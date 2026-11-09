"""Python extraction: source bytes -> tree-sitter tree -> IR chunks.

Every ``def`` becomes a chunk; code outside any def (module statements and
class bodies) goes to the module's top-level chunk. A chunk's body lists its
call sites in evaluation order. In M1, compound statements (``if``, ``for``,
``try``, ``with``, ``match``) are flattened in source order; M3 turns them
into branch points and terminators in ``_Extractor._walk``.

Decorators, default arguments and annotations on a ``def`` are not walked:
they run when the ``def`` statement runs, not when the function is called.
"""

from __future__ import annotations

import codecs
import io
import re
import stat
import tokenize
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace
from itertools import count
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Node, Parser, Tree

from minstrel.ir import Block, CallSite, Chunk, ChunkKind, iter_call_sites

_PARSER = Parser(Language(tree_sitter_python.language()))

# parse() refuses bigger files, so one file can't exhaust memory.
MAX_FILE_BYTES = 1_000_000

# Characters of chunk source, ids, callee text and class prefixes that one
# file's extraction may build: _OUTPUT_CHARS_PER_BYTE times the file size, but
# at least _MIN_OUTPUT_CHARS. Crafted code (deeply nested defs, long names
# called often, long call chains) could otherwise build thousands of times its
# own size; normal code stays far below this.
_OUTPUT_CHARS_PER_BYTE = 50
_MIN_OUTPUT_CHARS = 1_000_000

_COMPREHENSIONS = frozenset(
    {
        "list_comprehension",
        "set_comprehension",
        "dictionary_comprehension",
        "generator_expression",
    }
)
_COMPREHENSION_CLAUSES = frozenset({"for_in_clause", "if_clause"})

# A carriage return not followed by a newline: Python ends a line there.
_LONE_CR = re.compile(rb"\r(?!\n)")


def parse_bytes(source: bytes) -> Tree:
    """Parse Python source into a tree-sitter tree.

    Syntax errors don't raise: they show up as ``ERROR`` nodes in the tree.
    Each lone ``\\r`` becomes ``\\n`` first: Python ends a line there but
    tree-sitter doesn't, so in ``# note\\rrun()`` the call would hide in the
    comment. The swap keeps the length, so byte offsets still fit the file.

    Args:
        source: The file's raw bytes.

    Returns:
        The syntax tree. It keeps the source it parsed (lone ``\\r`` already
        swapped), which ``extract`` reads back.

    Raises:
        ValueError: if Python wouldn't read the source as UTF-8: it declares
            another encoding (``# coding: ...``), or ``tokenize`` rejects its
            first lines (for example an unknown encoding, or bytes that
            aren't UTF-8).
    """
    source = _LONE_CR.sub(b"\n", source)
    # After the swap, so the coding line is found where Python finds it.
    _check_utf8(source)
    return _PARSER.parse(source)


def parse(path: Path) -> Tree:
    """Read a Python file and parse it with ``parse_bytes``.

    Args:
        path: The file to read.

    Returns:
        The file's syntax tree.

    Raises:
        OSError: if the file can't be read.
        ValueError: if ``path`` is not a regular file (after following
            symlinks), is larger than ``MAX_FILE_BYTES``, or isn't UTF-8 by
            Python's rules (see ``parse_bytes``).
    """
    # Checked before opening: in an untrusted repo x.py can be a symlink to a
    # FIFO or a device such as /dev/stdin, and opening or reading one can hang.
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"{path}: not a regular file")
    # Read one byte past the cap instead of trusting the size stat() reports.
    with path.open("rb") as handle:
        source = handle.read(MAX_FILE_BYTES + 1)
    if len(source) > MAX_FILE_BYTES:
        raise ValueError(f"{path}: larger than {MAX_FILE_BYTES} bytes")
    return parse_bytes(source)


def module_name(file: str) -> str:
    """Return the dotted module name for a root-relative POSIX path.

    ``app.py`` -> ``app``, ``pkg/mod.py`` -> ``pkg.mod``,
    ``pkg/__init__.py`` -> ``pkg``.

    Args:
        file: Path relative to the project root, with ``/`` separators.

    Returns:
        The dotted module name.
    """
    parts = file.removesuffix(".py").split("/")
    if parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return ".".join(parts)


def extract(tree: Tree, file: str) -> list[Chunk]:
    """Turn a parsed file into chunks with their call sites.

    Every ``def`` becomes a chunk: ``method`` in a class body, else
    ``function``. Calls outside any def go to a ``module_toplevel`` chunk,
    which exists only when there are such calls. Syntax errors are
    tolerated: ``ERROR`` nodes are walked like any other node.

    Args:
        tree: The file's tree from ``parse`` or ``parse_bytes``.
        file: The file's root-relative POSIX path, e.g. ``pkg/mod.py``.

    Returns:
        The module top level first (if present), then every def in source
        order, outer defs before the defs nested in them.

    Raises:
        UnicodeDecodeError: if the source is not valid UTF-8.
        ValueError: if the code is nested too deeply to walk, or the chunks
            built from it would be far larger than the file (see
            ``_OUTPUT_CHARS_PER_BYTE``).
    """
    try:
        return _Extractor(tree, file).run()
    except RecursionError as error:
        # The walk recurses once per tree level, so pathologically deep code
        # (say, a 1000-term ``a + b + ...``) exceeds Python's recursion limit.
        raise ValueError(f"{file}: code nested too deeply to extract") from error


@dataclass(frozen=True)
class _Context:
    """Where a walk is: the chunk receiving call sites and the scope around it.

    Attributes:
        chunk_id: Id of the chunk the call sites belong to.
        calls: Numbers that chunk's call sites; shared by all its contexts.
        prefix: Qualname prefix for defs found here, e.g. ``outer.C.``.
        in_class: True in a class body (not inside a def in it), so defs
            found here are methods.
        conditional: True when the code walked may not run even though its
            statement does.
    """

    chunk_id: str
    calls: Iterator[int]
    prefix: str = ""
    in_class: bool = False
    conditional: bool = False


class _Extractor:
    """Builds one file's chunks in a single walk of its tree."""

    def __init__(self, tree: Tree, file: str) -> None:
        self.root = tree.root_node
        self.file = file
        self.module = module_name(file)
        self.lines = _source_lines(self.root)
        self.root_start = self.root.start_point
        # Per row with a call site on it: _multibyte_chars of its line.
        self.multibyte_chars: dict[int, tuple[list[int], list[int]]] = {}
        self.defs: list[Chunk] = []
        self.qualname_counts: Counter[str] = Counter()
        # The root's end_byte is the file size, give or take trailing whitespace.
        file_size = self.root.end_byte
        self.output_budget = max(_OUTPUT_CHARS_PER_BYTE * file_size, _MIN_OUTPUT_CHARS)
        self.output_chars = 0

    def run(self) -> list[Chunk]:
        """Walk the tree and return the file's chunks in program order."""
        top = self._new_chunk("<module>", "module_toplevel", 1, len(self.lines))
        self._walk(self.root, top.body, _Context(top.id, count(1)))
        return [top, *self.defs] if any(iter_call_sites(top.body)) else self.defs

    def _walk(self, node: Node, out: Block, ctx: _Context) -> None:
        """Append the call sites under ``node`` to ``out`` in evaluation order.

        A call's site goes after the calls in its callee and arguments
        (post-order). Defs become their own chunks; class bodies stay in
        ``ctx``'s chunk.
        """
        if node.type == "function_definition":
            self._function(node, ctx)
        elif node.type == "class_definition":
            self._class(node, out, ctx)
        else:
            for child, may_skip in _evaluation_order(node):
                child_ctx = replace(ctx, conditional=True) if may_skip else ctx
                self._walk(child, out, child_ctx)
            if node.type == "call":
                site = self._call_site(node, ctx)
                self._charge(len(site.id) + len(site.callee_text))
                out.append(site)

    def _function(self, node: Node, ctx: _Context) -> None:
        """Add a chunk for a ``def`` and walk only its body into that chunk."""
        parent = node.parent
        decorated = parent is not None and parent.type == "decorated_definition"
        first_line = (parent if decorated else node).start_point.row + 1
        qualname = ctx.prefix + _text(node.child_by_field_name("name"))
        kind: ChunkKind = "method" if ctx.in_class else "function"
        chunk = self._new_chunk(qualname, kind, first_line, _last_line(node))
        self.defs.append(chunk)
        body = node.child_by_field_name("body")
        if body is not None:
            inner = _Context(chunk.id, count(1), prefix=qualname + ".")
            self._walk(body, chunk.body, inner)

    def _class(self, node: Node, out: Block, ctx: _Context) -> None:
        """Walk a class's superclasses and body into the enclosing chunk.

        A class body runs when the ``class`` statement runs, so its calls
        belong to the enclosing chunk; defs in it become methods.
        """
        superclasses = node.child_by_field_name("superclasses")
        if superclasses is not None:
            self._walk(superclasses, out, ctx)
        body = node.child_by_field_name("body")
        if body is not None:
            name = _text(node.child_by_field_name("name"))
            class_ctx = replace(ctx, prefix=f"{ctx.prefix}{name}.", in_class=True)
            # Each nested class level keeps its own copy of the prefix.
            self._charge(len(class_ctx.prefix))
            self._walk(body, out, class_ctx)

    def _call_site(self, node: Node, ctx: _Context) -> CallSite:
        """Build the CallSite for a ``call`` node, numbered next in its chunk.

        The position is the callee's name token (``k`` in ``h(1).k()``, ``m``
        in ``self.m()``), so each call in a chain gets its own spot for the
        viewer's link. A callee with no name, like ``x[0]()``, uses its own
        start. The column counts characters of the line, not bytes.
        """
        callee = node.child_by_field_name("function")
        anchor = callee if callee is not None else node
        if anchor.type == "attribute":
            name = anchor.child_by_field_name("attribute")
            anchor = name if name is not None else anchor
        return CallSite(
            id=f"{ctx.chunk_id}#call{next(ctx.calls)}",
            line=anchor.start_point.row + 1,
            col=self._char_column(anchor),
            callee_text=_text(callee),
            conditional=ctx.conditional,
        )

    def _char_column(self, node: Node) -> int:
        """Return where ``node`` starts, in characters of ``self.lines[row]``.

        tree-sitter's column counts bytes. A non-ASCII line's multi-byte
        characters are indexed once, so each lookup is a bisect; decoding
        the line up to the column instead would take quadratic time on one
        long line full of calls.
        """
        row, column = node.start_point
        if row == self.root_start.row:
            # This line's text starts where the root does (after any BOM or
            # leading whitespace), so count from there.
            column -= self.root_start.column
        # A zero-width node tree-sitter adds for a syntax error can sit at the
        # end of the file, past the last line.
        line = self.lines[row] if row < len(self.lines) else ""
        if line.isascii():
            return column
        if row not in self.multibyte_chars:
            self.multibyte_chars[row] = _multibyte_chars(line)
        starts, extras = self.multibyte_chars[row]
        return column - extras[bisect_left(starts, column)]

    def _new_chunk(self, qualname: str, kind: ChunkKind, start: int, end: int) -> Chunk:
        """Create a chunk with a unique id and the source of lines start..end.

        A qualname repeated in one module gets ``~2``, ``~3``... on its id.
        The id and source count against the output budget.
        """
        self.qualname_counts[qualname] += 1
        repeat = self.qualname_counts[qualname]
        suffix = f"~{repeat}" if repeat > 1 else ""
        chunk = Chunk(
            id=f"{self.module}:{qualname}{suffix}",
            kind=kind,
            name=qualname,
            file=self.file,
            start_line=start,
            end_line=end,
            source="\n".join(self.lines[start - 1 : end]),
        )
        self._charge(len(chunk.id) + len(chunk.source))
        return chunk

    def _charge(self, chars: int) -> None:
        """Add ``chars`` to the characters built so far.

        Raises:
            ValueError: once the total passes ``output_budget``.
        """
        self.output_chars += chars
        if self.output_chars > self.output_budget:
            raise ValueError(
                f"{self.file}: extracted IR too large "
                f"(over {self.output_budget} characters)"
            )


def _evaluation_order(node: Node) -> list[tuple[Node, bool]]:
    """Return ``node``'s children in the order Python evaluates them.

    Each child comes with ``may_skip``: True when it may not run even though
    ``node`` runs, which makes the calls inside it conditional. Decorators
    and lambda parameters are left out (they run at definition time), and
    children missing after a syntax error are dropped.
    """
    kind = node.type
    by_field = node.child_by_field_name
    pairs: list[tuple[Node | None, bool]]
    if kind == "decorated_definition":
        pairs = [(by_field("definition"), False)]
    elif kind == "lambda":
        pairs = [(by_field("body"), True)]
    elif kind == "boolean_operator":  # the right operand short-circuits
        pairs = [(by_field("left"), False), (by_field("right"), True)]
    elif kind == "assignment":
        pairs = _assignment_order(node)
    elif kind == "comparison_operator":
        pairs = _comparison_order(node)
    elif kind == "conditional_expression":
        pairs = _conditional_expression_order(node)
    elif kind in _COMPREHENSIONS:
        pairs = _comprehension_order(node)
    else:
        pairs = [(child, False) for child in node.children]
    return [(child, may_skip) for child, may_skip in pairs if child is not None]


def _assignment_order(node: Node) -> list[tuple[Node | None, bool]]:
    """Order an assignment: the value first, then the targets left to right.

    tree-sitter nests ``a = b = value`` as ``a = (b = value)``, so the value
    is the innermost ``right``. Each level's other parts (its target, and its
    annotation if any) follow it, outermost level first.
    """
    targets: list[tuple[Node | None, bool]] = []
    level: Node | None = node
    while level is not None and level.type == "assignment":
        value = level.child_by_field_name("right")
        targets += [(child, False) for child in level.children if child != value]
        level = value
    return [(level, False), *targets]


def _comparison_order(node: Node) -> list[tuple[Node | None, bool]]:
    """Order a comparison in source order; operands after the second may skip.

    ``a() < b() < c()`` runs like ``a() < b() and b() < c()``, so each operand
    after the second runs only if the comparisons before it held.
    """
    pairs: list[tuple[Node | None, bool]] = []
    operands = 0
    for child in node.children:
        # Operands are the named children; operators are anonymous and
        # comments are extras.
        if child.is_named and not child.is_extra:
            operands += 1
        pairs.append((child, operands > 2))
    return pairs


def _conditional_expression_order(node: Node) -> list[tuple[Node | None, bool]]:
    """Order ``a if cond else b``: the condition first, then the two values."""
    # Named children come as true value, condition, false value. Comments
    # are named too, so drop extras before relying on positions.
    parts = [child for child in node.named_children if not child.is_extra]
    if len(parts) == 3:
        parts = [parts[1], parts[0], parts[2]]
    # With a syntax error the count can differ; keep source order then.
    return [(part, index > 0) for index, part in enumerate(parts)]


def _comprehension_order(node: Node) -> list[tuple[Node | None, bool]]:
    """Order a comprehension: its ``for``/``if`` clauses, then its body.

    Only the first iterable is sure to run (it is evaluated before the first
    item); everything else runs once per item, possibly never.
    """
    clauses = [c for c in node.named_children if c.type in _COMPREHENSION_CLAUSES]
    first_iterable = clauses[0].child_by_field_name("right") if clauses else None
    pairs: list[tuple[Node | None, bool]] = [(first_iterable, False)]
    for clause in clauses:
        pairs += [(part, True) for part in clause.children if part != first_iterable]
    return [*pairs, (node.child_by_field_name("body"), True)]


def _check_utf8(source: bytes) -> None:
    """Raise ValueError unless Python would read ``source`` as UTF-8.

    tree-sitter always reads UTF-8, but Python honors a ``# coding:`` line,
    and under another encoding the same bytes can be different code (in
    UTF-7, ``+AAo-`` inside a comment is a newline).
    """
    try:
        declared, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    except SyntaxError as error:  # unknown encoding, or first lines not UTF-8
        raise ValueError(f"cannot read source encoding: {error.msg}") from error
    # The codec's own name, so aliases like "utf8" count as UTF-8.
    encoding = codecs.lookup(declared).name
    if encoding not in ("utf-8", "utf-8-sig"):
        raise ValueError(f"declares encoding {encoding!r}; only UTF-8 is supported")


def _multibyte_chars(line: str) -> tuple[list[int], list[int]]:
    """Index the characters of ``line`` that take more than one UTF-8 byte.

    Returns:
        ``starts``, the byte offset of each such character in order, and
        ``extras``, where ``extras[k]`` counts the bytes the first ``k`` of
        them take beyond one per character (``extras[0]`` is 0).
    """
    starts: list[int] = []
    extras = [0]
    offset = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if width > 1:
            starts.append(offset)
            extras.append(extras[-1] + width - 1)
        offset += width
    return starts, extras


def _source_lines(root: Node) -> list[str]:
    """Return the file's lines without line endings; ``lines[row]`` is row ``row``.

    tree-sitter's root node skips leading whitespace, so its text lacks any
    blank lines (or BOM) before the first token; empty lines stand in for
    them. Lines split on ``\\n`` only, matching tree-sitter's row count
    (``str.splitlines`` also splits on form feeds and other separators).

    Raises:
        UnicodeDecodeError: if the source is not valid UTF-8.
    """
    text = root.text.decode("utf-8").replace("\r\n", "\n")
    return [""] * root.start_point.row + text.removesuffix("\n").split("\n")


def _last_line(node: Node) -> int:
    """Return the 1-based last line of ``node``.

    A node ending at column 0 stopped just after a newline, so its last line
    is the one before.
    """
    end = node.end_point
    if end.column == 0 and end.row > node.start_point.row:
        return end.row
    return end.row + 1


def _text(node: Node | None) -> str:
    """Return ``node``'s source text, or "" when a syntax error left it out."""
    return node.text.decode("utf-8") if node is not None else ""
