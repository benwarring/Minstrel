"""Command-line interface for Minstrel.

Defines the Typer ``app`` behind the ``minstrel`` console script and
``load_program``, which turns a file or directory into a resolved ``Program``.
This is the one module outside ``adapters/`` allowed to import an adapter
(docs/decisions.md, "``load_program`` lives in ``cli.py``").

Behavior follows docs/decisions.md, "M2 CLI and golden fixtures": program
diagnostics go to stderr as warnings, every analyzed-code string printed here
goes through ``escape_control_chars``, and exit codes are 0 for success, 1 for
a failed run and 2 for usage errors. ``build`` stays a stub until M4.
"""

import json
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from minstrel.adapters.base import LanguageAdapter
from minstrel.adapters.python import PythonAdapter
from minstrel.core.linearize import linearize
from minstrel.ir import (
    SCHEMA_VERSION,
    Chunk,
    Diagnostic,
    Program,
    Selection,
    program_to_json,
)
from minstrel.render.text import escape_control_chars, render_text

# Run-wide caps on what load_program reads. The adapter caps each file on its
# own; these bound a whole run. A 10,000-line project is far below both.
MAX_FILES = 10_000
MAX_TOTAL_BYTES = 50_000_000
# Largest --path selection file accepted.
MAX_SELECTION_BYTES = 1_000_000

# A directory holding this file is a virtualenv: skipped, so a scan never
# reads installed packages.
_VENV_MARKER = "pyvenv.cfg"
_SKIPPED_DIR_NAMES = frozenset({"__pycache__"})

app = typer.Typer(
    help="Lay out Python functions as pages in execution order.",
    no_args_is_help=True,
)

SourcePath = Annotated[
    Path,
    typer.Argument(exists=True, help="Python file or directory to read."),
]
EntryOption = Annotated[
    str | None,
    typer.Option("--entry", help="Entry point to start from (default: top-ranked)."),
]


@dataclass
class _LoadState:
    """What ``load_program`` has gathered so far.

    Attributes:
        files: Root-relative paths of the files whose chunks were kept.
        chunks: Every kept chunk, in file then extraction order.
        diagnostics: Problems found while reading.
        owners: Chunk id -> the file that first defined it.
        file_count: Files counted against ``MAX_FILES``.
        byte_count: Bytes counted against ``MAX_TOTAL_BYTES``.
    """

    files: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    owners: dict[str, str] = field(default_factory=dict)
    file_count: int = 0
    byte_count: int = 0


def load_program(path: Path) -> Program:
    """Read, extract and resolve every source file under ``path``.

    A file is read alone, with its parent as the root. A directory is walked
    in sorted root-relative POSIX order, skipping symlinks, dot-directories,
    ``__pycache__``, virtualenvs and anything that resolves outside the root.
    Files past ``MAX_FILES`` or ``MAX_TOTAL_BYTES`` are skipped with one
    ``file_limit`` diagnostic; a file that can't be read or extracted gets
    ``file_skipped``; a file whose chunk ids clash with an earlier file's is
    skipped whole with ``duplicate_module``.

    Args:
        path: A source file or a project directory.

    Returns:
        The resolved program, with ``entry_candidates`` filled in. Its
        ``files`` list is empty when nothing could be read.

    Raises:
        UnicodeDecodeError: if the adapter's resolver meets undecodable
            source (``extract`` rejects such files first, so this should
            not happen).
    """
    adapter = PythonAdapter()
    state = _LoadState()
    files = _source_files(path, adapter.extensions, state.diagnostics)
    _read_files(adapter, files, state)
    program = Program(
        schema_version=SCHEMA_VERSION,
        language=adapter.language,
        files=state.files,
        chunks=state.chunks,
        diagnostics=state.diagnostics,
    )
    adapter.resolve(program)
    program.entry_candidates = adapter.entry_candidates(program)
    return program


def _source_files(
    path: Path, extensions: tuple[str, ...], diagnostics: list[Diagnostic]
) -> Iterator[tuple[Path, str]]:
    """Yield ``(file path, root-relative POSIX path)`` for each file to read.

    A single file is yielded as is, whatever its suffix: the user named it.
    A directory is walked depth-first without recursion, so a deeply nested
    tree can't exhaust Python's stack. Directories that can't be listed, and
    entries that resolve outside the root, get a ``file_skipped`` diagnostic.
    """
    if not path.is_dir():
        file_path = path.resolve()
        yield file_path, file_path.name
        return
    root = path.resolve()
    # Each item is (path, is_directory). Children are pushed in reverse so
    # they pop in sorted order.
    pending: list[tuple[Path, bool]] = [(root, True)]
    while pending:
        current, is_directory = pending.pop()
        relative = current.relative_to(root).as_posix()
        if not is_directory:
            yield current, relative
            continue
        children = _children(current, root, extensions, diagnostics)
        pending.extend(reversed(children))


def _children(
    directory: Path,
    root: Path,
    extensions: tuple[str, ...],
    diagnostics: list[Diagnostic],
) -> list[tuple[Path, bool]]:
    """List the subdirectories and source files of ``directory`` worth walking.

    Returns:
        ``(path, is_directory)`` pairs, sorted so that a depth-first walk
        yields files in sorted root-relative POSIX order.
    """
    try:
        with os.scandir(directory) as listing:
            entries = list(listing)
    except OSError as error:
        diagnostics.append(_skipped(directory, root, _os_reason(error)))
        return []
    kept: list[tuple[Path, bool]] = []
    for entry in entries:
        try:
            child = _walkable_child(entry, extensions)
        except OSError as error:
            diagnostics.append(_skipped(Path(entry.path), root, _os_reason(error)))
            continue
        if child is None:
            continue
        if not child[0].resolve().is_relative_to(root):
            # Symlinks are already skipped; this catches Windows junctions
            # and anything else that leads outside the project.
            diagnostics.append(_skipped(child[0], root, "outside the project root"))
            continue
        kept.append(child)
    # A directory sorts as its name plus "/", so its whole subtree lands
    # exactly where its paths fall in a sort of full POSIX path strings
    # ("a.py" < "a/b.py", because "." < "/").
    kept.sort(key=lambda item: (item[0].name + "/") if item[1] else item[0].name)
    return kept


def _walkable_child(
    entry: os.DirEntry[str], extensions: tuple[str, ...]
) -> tuple[Path, bool] | None:
    """Classify one directory entry: ``(path, is_directory)``, or None to skip.

    Raises:
        OSError: if the entry's type can't be read.
    """
    if entry.is_symlink():
        return None
    if entry.is_dir(follow_symlinks=False):
        if _is_skipped_directory(entry):
            return None
        return Path(entry.path), True
    if os.path.splitext(entry.name)[1] in extensions:
        # Non-regular files (FIFOs, devices) are kept here: the adapter's
        # parse rejects them with ValueError, so they get a diagnostic.
        return Path(entry.path), False
    return None


def _is_skipped_directory(entry: os.DirEntry[str]) -> bool:
    """Whether a directory is hidden, a bytecode cache or a virtualenv."""
    if entry.name.startswith(".") or entry.name in _SKIPPED_DIR_NAMES:
        return True
    # lexists never raises; an unreadable marker just doesn't count.
    return os.path.lexists(os.path.join(entry.path, _VENV_MARKER))


def _read_files(
    adapter: LanguageAdapter, files: Iterator[tuple[Path, str]], state: _LoadState
) -> None:
    """Parse and extract each file into ``state`` until a run-wide cap is hit."""
    for file_path, relative in files:
        size = _size_or_zero(file_path)
        if state.file_count >= MAX_FILES or state.byte_count + size > MAX_TOTAL_BYTES:
            state.diagnostics.append(_limit_reached(relative))
            return
        state.file_count += 1
        state.byte_count += size
        try:
            chunks = adapter.extract(adapter.parse(file_path), relative)
        except (OSError, ValueError) as error:
            # ValueError includes UnicodeDecodeError.
            reason = _skip_reason(error, file_path, relative)
            state.diagnostics.append(_file_diagnostic("file_skipped", reason, relative))
            continue
        _add_file(state, relative, chunks)


def _size_or_zero(file_path: Path) -> int:
    """Return the file's size in bytes, or 0 if it can't be read.

    An unreadable file counts as 0 bytes because the adapter's ``parse``
    then raises the same ``OSError`` and the file is skipped with a
    diagnostic. The size can change before the read; the adapter's own
    per-file cap still bounds each read.
    """
    try:
        return file_path.stat().st_size
    except OSError:
        return 0


def _add_file(state: _LoadState, relative: str, chunks: list[Chunk]) -> None:
    """Keep a file's chunks, or skip the whole file if any id is already taken."""
    clash = next((chunk.id for chunk in chunks if chunk.id in state.owners), None)
    if clash is not None:
        message = (
            f"chunk id {clash} is already defined in {state.owners[clash]}; "
            "file skipped"
        )
        state.diagnostics.append(
            _file_diagnostic("duplicate_module", message, relative)
        )
        return
    for chunk in chunks:
        state.owners[chunk.id] = relative
    state.files.append(relative)
    state.chunks.extend(chunks)


def _file_diagnostic(code: str, message: str, relative: str) -> Diagnostic:
    """A diagnostic about one file, named by its root-relative path."""
    return Diagnostic(code=code, message=message, file=relative)


def _skipped(path: Path, root: Path, reason: str) -> Diagnostic:
    """A ``file_skipped`` diagnostic for a walked path, named relative to root."""
    relative = path.relative_to(root).as_posix()
    return _file_diagnostic("file_skipped", f"skipped: {reason}", relative)


def _limit_reached(relative: str) -> Diagnostic:
    """The one ``file_limit`` diagnostic, at the first file over a cap."""
    message = (
        f"run limit reached ({MAX_FILES} files or {MAX_TOTAL_BYTES} bytes); "
        "this file and every later one skipped"
    )
    return _file_diagnostic("file_limit", message, relative)


def _skip_reason(error: Exception, file_path: Path, relative: str) -> str:
    """Explain a failed read without the absolute path, which ends up in the IR."""
    if isinstance(error, OSError):
        return f"skipped: {_os_reason(error)}"
    return f"skipped: {str(error).replace(str(file_path), relative)}"


def _os_reason(error: OSError) -> str:
    """The OS's reason text, without the filename ``str(error)`` would add."""
    return error.strerror or type(error).__name__


def _use_replacing_streams() -> None:
    """Make stdout and stderr replace characters their encoding can't write.

    Output may hold any printable character from the analyzed code (control
    and format characters are already escaped). On a cp1252 Windows console
    or redirect, writing one it can't encode would crash the run. Switching
    to UTF-8 instead would garble every non-ASCII letter on such a console;
    ``errors="replace"`` keeps the console's encoding and prints "?" for the
    few characters it lacks. Streams without ``reconfigure`` (some test
    capture objects) are left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def _echo_escaped(text: str, *, err: bool = False) -> None:
    """Print one line with control and format characters escaped."""
    typer.echo(escape_control_chars(text), err=err)


def _fail(message: str) -> NoReturn:
    """Report a failed run on stderr and exit with status 1.

    Args:
        message: What went wrong; may quote analyzed code or user input.

    Raises:
        typer.Exit: always, with exit code 1.
    """
    _echo_escaped(f"minstrel: {message}", err=True)
    raise typer.Exit(code=1)


def _not_implemented(command: str) -> NoReturn:
    """Report that ``command`` is a stub and exit with status 1.

    Args:
        command: CLI command name shown in the message.

    Raises:
        typer.Exit: always, with exit code 1.
    """
    _fail(f"{command}: not implemented yet")


def _warn_diagnostics(diagnostics: list[Diagnostic]) -> None:
    """Print each program diagnostic to stderr as a warning."""
    for diagnostic in diagnostics:
        location = diagnostic.file or ""
        if diagnostic.file is not None and diagnostic.line is not None:
            location += f":{diagnostic.line}"
        prefix = f"{location}: " if location else ""
        _echo_escaped(
            f"warning: {prefix}{diagnostic.code}: {diagnostic.message}", err=True
        )


def _load_or_exit(path: Path) -> Program:
    """Run ``load_program``, print its warnings, and exit 1 if nothing was read."""
    try:
        program = load_program(path)
    except UnicodeDecodeError as error:
        _fail(f"could not resolve calls: {error}")
    _warn_diagnostics(program.diagnostics)
    if not program.files:
        _fail(f"no source files read from {path}")
    return program


def _candidate_tiers(program: Program) -> list[list[str]]:
    """Return the entry tiers, or exit 1 if there are none."""
    if not program.entry_candidates:
        _fail("no entry candidates found; pass --entry to choose one")
    return program.entry_candidates


def _entry_lines(program: Program, tiers: list[list[str]]) -> list[str]:
    """One line per candidate: tier (the first marked default), id, file:line."""
    chunks = {chunk.id: chunk for chunk in program.chunks}
    lines: list[str] = []
    for number, tier in enumerate(tiers, start=1):
        label = f"tier {number} (default)" if number == 1 else f"tier {number}"
        for chunk_id in tier:
            chunk = chunks[chunk_id]
            lines.append(f"{label}  {chunk_id}  {chunk.file}:{chunk.start_line}")
    return lines


def _match_entry(program: Program, name: str) -> str:
    """Find the chunk ``--entry NAME`` means, or exit 1 listing the candidates.

    ``name`` matches a chunk id exactly, else a unique ``Chunk.name``.
    """
    if any(chunk.id == name for chunk in program.chunks):
        return name
    matches = [chunk.id for chunk in program.chunks if chunk.name == name]
    if len(matches) == 1:
        return matches[0]
    if matches:
        _fail(f"--entry {name} matches several chunks: {', '.join(matches)}")
    candidates = [chunk_id for tier in program.entry_candidates for chunk_id in tier]
    listed = ", ".join(candidates) or "(none)"
    _fail(f"--entry {name} matches no chunk; entry candidates: {listed}")


def _bad_selection(reason: str) -> typer.BadParameter:
    """A usage error (exit 2) about the ``--path`` file."""
    return typer.BadParameter(escape_control_chars(reason), param_hint="'--path'")


def _read_selection_bytes(path_file: Path) -> bytes:
    """Read the ``--path`` file, refusing non-regular or oversized files.

    Raises:
        typer.BadParameter: if the file can't be read, isn't a regular file
            or is larger than ``MAX_SELECTION_BYTES``.
    """
    try:
        # Checked before opening: a FIFO or device could block the read.
        if not stat.S_ISREG(path_file.stat().st_mode):
            raise _bad_selection("not a regular file")
        with path_file.open("rb") as handle:
            raw = handle.read(MAX_SELECTION_BYTES + 1)
    except OSError as error:
        raise _bad_selection(f"can't read it: {_os_reason(error)}") from error
    if len(raw) > MAX_SELECTION_BYTES:
        raise _bad_selection(f"larger than {MAX_SELECTION_BYTES} bytes")
    return raw


def _parse_selection(raw: bytes) -> Selection:
    """Decode a ``--path`` file as a JSON object of string to string.

    Raises:
        typer.BadParameter: if it isn't valid JSON of that shape.
    """
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as error:
        # ValueError covers bad JSON and undecodable bytes; RecursionError
        # comes from very deeply nested arrays or objects.
        raise _bad_selection(f"not valid JSON: {error}") from error
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise _bad_selection("must be a JSON object mapping strings to strings")
    return data


def _outline(program: Program, entry_id: str, selection: Selection | None) -> str:
    """Linearize one story in "follow" mode and render it as text."""
    try:
        story = linearize(program, entry_id, selection, "follow")
    except ValueError as error:
        _fail(str(error))
    return render_text(story, program)


@app.command()
def entries(path: SourcePath) -> None:
    """List entry-point candidates."""
    _use_replacing_streams()
    program = _load_or_exit(path)
    for line in _entry_lines(program, _candidate_tiers(program)):
        _echo_escaped(line)


@app.command()
def build(
    path: SourcePath,
    entry: EntryOption = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="HTML file to write.")
    ] = Path("story.html"),
) -> None:
    """Write the interactive HTML viewer."""
    _not_implemented("build")


@app.command("print")
def print_outline(
    path: SourcePath,
    entry: EntryOption = None,
    path_file: Annotated[
        Path | None,
        typer.Option(
            "--path",
            exists=True,
            dir_okay=False,
            help="JSON file mapping branch ids to selected arm ids.",
        ),
    ] = None,
) -> None:
    """Print the page outline to the terminal."""
    _use_replacing_streams()
    selection = None
    if path_file is not None:
        selection = _parse_selection(_read_selection_bytes(path_file))
    program = _load_or_exit(path)
    if entry is not None:
        entry_ids = [_match_entry(program, entry)]
    else:
        entry_ids = _candidate_tiers(program)[0]
    for number, entry_id in enumerate(entry_ids):
        if number > 0:
            typer.echo("")
        # render_text escapes its own output; escaping again would also turn
        # its newlines into literal "\n" text.
        typer.echo(_outline(program, entry_id, selection), nl=False)


@app.command()
def ir(
    path: SourcePath,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="JSON file to write (default: stdout)."),
    ] = None,
) -> None:
    """Dump the resolved IR as JSON."""
    _use_replacing_streams()
    program = _load_or_exit(path)
    # json.dumps escapes control characters and non-ASCII, so the text is
    # plain ASCII and needs no further escaping.
    text = program_to_json(program)
    if output is None:
        typer.echo(text)
        return
    try:
        output.write_text(text + "\n", encoding="utf-8")
    except OSError as error:
        _fail(f"could not write {output}: {_os_reason(error)}")
