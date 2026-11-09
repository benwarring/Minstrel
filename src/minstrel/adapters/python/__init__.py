"""The Python language adapter: parsing, extraction, resolution and entries.

``PythonAdapter`` satisfies ``LanguageAdapter``. It keeps each file's parse
tree after ``extract``, so ``resolve`` and ``entry_candidates`` can read Python
details (bindings, the ``__main__`` guard) without language-specific IR fields
(docs/decisions.md, "The Python adapter keeps its parsed trees").

Entry candidates come in tiers, best first (PLAN.md "Entry detection"): module
top levels with an ``if __name__ == "__main__":`` block (in a file with no
syntax errors), then module-level functions named ``main``, then every other
module top level.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from tree_sitter import Node, Tree

from minstrel.adapters.python import extract, resolve
from minstrel.ir import Chunk, Diagnostic, Program, iter_call_sites

# The operands of a main guard; either may come first. The name is compared
# after NFKC normalization, as Python reads identifiers. The string must be a
# plain literal in either quote style, with no prefix, escape or triple quotes.
_GUARD_NAME = "__name__"
_MAIN_LITERALS = frozenset({b'"__main__"', b"'__main__'"})


class PythonAdapter:
    """Turns Python source files into Minstrel's IR.

    One instance reads one project: the trees it keeps from ``extract`` are
    what ``resolve`` and ``entry_candidates`` read later.

    Attributes:
        language: ``"python"``, copied into ``Program.language``.
        extensions: The file suffixes this adapter reads, ``(".py",)``.
    """

    language: str = "python"
    extensions: tuple[str, ...] = (".py",)

    def __init__(self) -> None:
        self._trees: dict[str, Tree] = {}

    def parse(self, path: Path) -> Tree:
        """Read and parse one Python file (see ``extract.parse``).

        Args:
            path: The file to read.

        Returns:
            The file's tree-sitter tree.

        Raises:
            OSError: if the file can't be read.
            ValueError: if the file isn't a regular file, is too large, or
                isn't UTF-8 by Python's rules.
        """
        return extract.parse(path)

    def extract(self, tree: object, file: str) -> list[Chunk]:
        """Turn one parsed file into chunks, keeping its tree for later steps.

        Args:
            tree: A tree returned by ``parse``.
            file: The file's root-relative POSIX path, e.g. ``pkg/mod.py``.

        Returns:
            The file's chunks, module top level (if any) first, then its defs
            in source order.

        Raises:
            TypeError: if ``tree`` isn't a tree-sitter ``Tree``.
            UnicodeDecodeError: if the tree's source isn't valid UTF-8.
            ValueError: if the code is nested too deeply or its chunks would be
                far larger than the file (see ``extract.extract``).
        """
        if not isinstance(tree, Tree):
            raise TypeError(f"expected a tree-sitter Tree, got {type(tree).__name__}")
        chunks = extract.extract(tree, file)
        # Kept only on success: a file that fails to extract has no chunks to
        # resolve or rank.
        self._trees[file] = tree
        return chunks

    def resolve(self, program: Program) -> None:
        """Fill every call site's ``targets`` and ``status`` in place.

        Each file is resolved on its own. A file over its name budget keeps
        all its calls ``unresolved`` and adds a ``resolve_skipped`` diagnostic
        to ``program.diagnostics``; the other files still resolve.

        Args:
            program: A program built from this adapter's ``extract`` output.

        Raises:
            KeyError: if a chunk's file wasn't extracted by this adapter.
            UnicodeDecodeError: if a kept tree's source isn't valid UTF-8
                (``extract`` rejects such files first).
        """
        # M2 resolution is single-file, and resolve.resolve raises ValueError
        # for the whole call when any one file is over its name budget, so
        # resolving files one at a time keeps one bad file from blocking the
        # rest. M5 revisits this when imports link files to each other.
        for file, chunks in _chunks_by_file(program.chunks).items():
            single_file = Program(
                language=program.language, files=[file], chunks=chunks
            )
            try:
                resolve.resolve(single_file, {file: self._trees[file]})
            except UnicodeDecodeError:
                # A ValueError subclass, but not a name-budget overrun: let it
                # propagate, as documented above.
                raise
            except ValueError:
                # Over the name budget. Reset this file's calls so the
                # diagnostic holds even if resolve.resolve raised after it
                # had already written some of them.
                _mark_unresolved(chunks)
                program.diagnostics.append(
                    Diagnostic(
                        code="resolve_skipped",
                        message="too many names to resolve; calls left unresolved",
                        file=file,
                    )
                )

    def entry_candidates(self, program: Program) -> list[list[str]]:
        """Rank the program's possible entry points.

        Tiers, best first: module top levels whose file has a main guard and
        no syntax errors, then module-level functions named ``main``, then
        every other module top level (one exists only when its module makes
        calls). Empty tiers
        are left out; ids within a tier keep ``program.chunks`` order.

        Args:
            program: A program built from this adapter's ``extract`` output.

        Returns:
            Tiers of chunk ids, best tier first; empty if there are none.

        Raises:
            KeyError: if a module top level's file wasn't extracted by this
                adapter.
        """
        guarded: list[str] = []
        mains: list[str] = []
        toplevels: list[str] = []
        for chunk in program.chunks:
            if chunk.kind == "module_toplevel":
                has_guard = _has_main_guard(self._trees[chunk.file])
                (guarded if has_guard else toplevels).append(chunk.id)
            elif chunk.kind == "function" and _is_module_main(chunk):
                mains.append(chunk.id)
        return [tier for tier in (guarded, mains, toplevels) if tier]


def _chunks_by_file(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    """Group chunks by ``Chunk.file``, keeping their order."""
    grouped: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.file, []).append(chunk)
    return grouped


def _mark_unresolved(chunks: list[Chunk]) -> None:
    """Set every call site in ``chunks`` to ``unresolved`` with no targets."""
    for chunk in chunks:
        for site in iter_call_sites(chunk.body):
            site.status = "unresolved"
            site.targets = []


def _is_module_main(chunk: Chunk) -> bool:
    """Whether ``chunk`` is the module-level ``main``, id ``<module>:main``.

    A repeated ``def main`` gets ``<module>:main~2`` and isn't counted.
    """
    return chunk.id == f"{extract.module_name(chunk.file)}:main"


def _has_main_guard(tree: Tree) -> bool:
    """Whether any module-scope statement is an ``if __name__ == "__main__":``.

    A file with syntax errors never counts, so a broken file can't claim the
    top entry tier with a decoy guard.
    """
    if tree.root_node.has_error:
        return False
    return any(_is_main_guard(statement) for statement in tree.root_node.children)


def _is_main_guard(statement: Node) -> bool:
    """Whether ``statement`` is an ``if`` comparing ``__name__`` to ``"__main__"``.

    Only a single ``==`` with the two operands in either order counts; a
    parenthesized or chained condition doesn't. The name matches after NFKC
    normalization (so ``__name__`` spelled with fullwidth letters counts); the
    string must be a plain ``"__main__"`` or ``'__main__'`` literal.
    """
    if statement.type != "if_statement":
        return False
    condition = statement.child_by_field_name("condition")
    if condition is None or condition.type != "comparison_operator":
        return False
    parts = condition.children
    if len(parts) != 3 or parts[1].type != "==":
        return False
    left, right = parts[0], parts[2]
    return (_is_guard_name(left) and _is_main_literal(right)) or (
        _is_guard_name(right) and _is_main_literal(left)
    )


def _is_guard_name(node: Node) -> bool:
    """Whether ``node`` is the identifier ``__name__``, NFKC-normalized as Python."""
    if node.type != "identifier":
        return False
    # "replace" keeps this from raising: extract already rejected undecodable
    # files, and a replacement character can never normalize to "__name__".
    raw = (node.text or b"").decode("utf-8", errors="replace")
    return unicodedata.normalize("NFKC", raw) == _GUARD_NAME


def _is_main_literal(node: Node) -> bool:
    """Whether ``node`` is a plain ``"__main__"`` or ``'__main__'`` string."""
    return node.type == "string" and node.text in _MAIN_LITERALS
