"""Minstrel's language-neutral IR: the contract between adapters and the core.

An adapter builds a ``Program`` of ``Chunk``s, the resolver fills each
``CallSite``'s ``targets`` and ``status`` in place, and the linearizer turns
the program into ``Story`` objects. Nothing language-specific belongs here:
anything language-flavored (like the word ``elif``) arrives as a display
string from the adapter.

Conventions: lines are 1-based file line numbers, columns are 0-based, line
ranges are inclusive, file paths are root-relative POSIX paths, and ids follow
docs/decisions.md (``pkg.mod:Class.method``, ``<chunk>#callN``,
``<chunk>#branchN``, ``<branch>.armN``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal

# Bump on any IR change, in the same commit as the fixtures it affects.
SCHEMA_VERSION = 1

_JSON_INDENT = 2

ChunkKind = Literal["function", "method", "module_toplevel"]
CallStatus = Literal["resolved", "ambiguous", "external", "unresolved"]
BranchKind = Literal["if", "match", "try", "loop"]
TerminatorKind = Literal["return", "raise", "break", "continue"]
CallNote = Literal[
    "placed", "see_page", "recursive", "external", "unresolved", "depth_limit"
]
StoryMode = Literal["follow", "all"]

# The reader's choices ("Path" in PLAN.md): BranchPoint id -> Arm id, and
# ambiguous CallSite id -> chosen chunk id. Missing entries mean the default.
Selection = dict[str, str]


@dataclass
class Diagnostic:
    """A problem worth showing the reader, such as an unresolved call.

    Attributes:
        code: Stable short identifier for the kind of problem. Golden
            fixtures compare this and the location, not the message.
        message: Human-readable explanation.
        file: Path the problem is in, if it has one.
        line: Line the problem is on, if it has one.
    """

    code: str
    message: str
    file: str | None = None
    line: int | None = None


@dataclass
class CallSite:
    """One call expression in a chunk's body.

    Attributes:
        id: ``<chunk id>#callN``, N counting from 1 in source order.
        callee_text: The callee as written, e.g. ``self.load`` (display only).
        targets: Chunk ids the call may go to, filled by the resolver. More
            than one makes the call a choice point keyed by ``id``.
        status: Resolution outcome, filled by the resolver.
        conditional: True when the call may be skipped by a short-circuit,
            ternary, comprehension or lambda.
    """

    id: str
    line: int
    col: int
    callee_text: str
    targets: list[str] = field(default_factory=list)
    status: CallStatus = "unresolved"
    conditional: bool = False


@dataclass
class Terminator:
    """A statement that ends the walk on its path.

    ``return`` and ``raise`` end the rest of the chunk; ``break`` and
    ``continue`` end the rest of the nearest enclosing loop arm.
    """

    kind: TerminatorKind
    line: int


@dataclass
class Arm:
    """One way through a BranchPoint.

    Attributes:
        id: ``<branch id>.armN``.
        label: Display text, e.g. ``if user.is_admin`` or ``loop body skipped``.
        start_line: First line of the arm's span.
        end_line: Last line of the arm's span.
        body: Nodes walked when this arm is taken.
        partial: Nodes that run before ``body`` and may be interrupted (the
            try body in an ``except`` arm). Their calls are marked as maybe
            interrupted and their terminators are ignored.
        terminates: True when the arm ends in ``return`` or ``raise``, so the
            rest of the chunk does not run on this arm.
    """

    id: str
    label: str
    start_line: int
    end_line: int
    body: Block = field(default_factory=list)
    partial: Block = field(default_factory=list)
    terminates: bool = False


@dataclass
class BranchPoint:
    """A place where the walk takes one of several arms.

    Calls in the condition are not part of the branch point. They sit in the
    enclosing block just before it, so every path walks them.

    Attributes:
        id: ``<chunk id>#branchN``, N counting from 1 in source order.
        line: Line of the branching statement.
        arms: The arms, in source order.
        default_arm: Id of the arm taken when the Selection has no entry.
    """

    id: str
    kind: BranchKind
    line: int
    arms: list[Arm]
    default_arm: str


# A Block is a chunk's or arm's body. Its order is source order, which is
# execution order within a chunk.
Node = CallSite | BranchPoint | Terminator
Block = list[Node]


@dataclass
class Chunk:
    """A unit that becomes one page: a function, a method, or module top level.

    Attributes:
        id: ``<module>:<qualname>``; module top level is ``<module>:<module>``.
        name: Display name.
        file: Path of the source file.
        start_line: First line of the chunk in ``file``.
        end_line: Last line of the chunk in ``file``.
        source: Source text of the chunk's lines.
        body: The chunk's call sites, branch points and terminators.
    """

    id: str
    kind: ChunkKind
    name: str
    file: str
    start_line: int
    end_line: int
    source: str
    body: Block = field(default_factory=list)


# kw_only so schema_version can lead the JSON while keeping its default.
@dataclass(kw_only=True)
class Program:
    """The top-level IR document an adapter produces.

    Attributes:
        schema_version: IR version this document follows.
        language: Source language name, e.g. ``python``.
        files: Paths of the files read.
        chunks: Every chunk, in the order the adapter extracted them.
        entry_candidates: Chunk ids that could start a story, best first.
        diagnostics: Problems found while reading or resolving.
    """

    schema_version: int = SCHEMA_VERSION
    language: str
    files: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    entry_candidates: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


@dataclass
class PageCall:
    """How one call site walked on a page is shown.

    Attributes:
        call_site_id: The CallSite this entry describes.
        target_page: Index of the page the call links to; None when there is
            none (external, unresolved, depth limit).
        note: ``placed`` (the target became a new page here), ``see_page``
            (the target was already placed), ``recursive`` (the target is on
            the current call stack), ``external``, ``unresolved``, or
            ``depth_limit`` (the walk stopped at the depth limit).
        conditional: Copied from the CallSite.
        maybe_interrupted: True when the call is in an arm's ``partial`` block.
    """

    call_site_id: str
    target_page: int | None
    note: CallNote
    conditional: bool = False
    maybe_interrupted: bool = False


@dataclass
class Page:
    """One chunk placed in a story.

    Attributes:
        index: 1-based page number.
        depth: Call depth; the entry page is 0.
        breadcrumb: Chunk ids from the entry up to and including this page.
        reached_from: Id of the call site that placed this page; None for
            the entry page.
        active_ranges: ``(start_line, end_line)`` spans of the selected arms.
        inactive_ranges: Spans to dim: unselected arms, plus the rest of the
            chunk after a ``return`` or ``raise``. Both range lists are empty
            in ``all`` mode.
        via_arm: In ``all`` mode, the label of the arm this page was reached
            through; otherwise None.
        calls: One entry per call site walked on this page, in walk order.
    """

    index: int
    chunk_id: str
    depth: int
    breadcrumb: list[str]
    reached_from: str | None = None
    active_ranges: list[tuple[int, int]] = field(default_factory=list)
    inactive_ranges: list[tuple[int, int]] = field(default_factory=list)
    via_arm: str | None = None
    calls: list[PageCall] = field(default_factory=list)


@dataclass
class Story:
    """The linearizer's output for one entry and one Selection.

    Attributes:
        entry: Chunk id of the entry (page 1).
        mode: ``follow`` walks only selected arms; ``all`` walks every arm.
        pages: Pages in reading order.
        not_on_path: Chunk ids reachable on some path but not this one, in
            the order an all-arms walk first reaches them.
        never_reached: Chunk ids unreachable on every path, in program chunk
            order. Module top levels are excluded (they run on import).
        diagnostics: One per unresolved call site walked, plus depth-limit hits.
    """

    entry: str
    mode: StoryMode
    pages: list[Page] = field(default_factory=list)
    not_on_path: list[str] = field(default_factory=list)
    never_reached: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def iter_call_sites(block: Block) -> Iterator[CallSite]:
    """Yield every call site in ``block``, including those inside branch arms.

    Arms are walked in order, each ``partial`` first and then ``body``. A call
    site shared by several arms (such as a try body) is yielded once, at its
    first occurrence.

    Args:
        block: The block to walk, usually a ``Chunk.body``.

    Yields:
        Each distinct CallSite, in walk order.
    """
    seen: set[str] = set()
    for call in _walk_calls(block):
        if call.id not in seen:
            seen.add(call.id)
            yield call


def _walk_calls(block: Block) -> Iterator[CallSite]:
    """Yield call sites depth-first, including repeats shared between arms."""
    for node in block:
        if isinstance(node, CallSite):
            yield node
        elif isinstance(node, BranchPoint):
            for arm in node.arms:
                yield from _walk_calls(arm.partial)
                yield from _walk_calls(arm.body)


# The "node" discriminator written on each block node, so JSON decodes
# without guessing which dataclass a dict was.
_NODE_TYPES: dict[str, type[Node]] = {
    "call": CallSite,
    "branch": BranchPoint,
    "terminator": Terminator,
}
_NODE_TAGS = {node_type: tag for tag, node_type in _NODE_TYPES.items()}


def program_to_json(program: Program) -> str:
    """Serialize a Program to indented JSON that ``program_from_json`` reads back.

    Args:
        program: The IR document to serialize.

    Returns:
        The JSON text. Block nodes carry a ``"node"`` key (``call``,
        ``branch`` or ``terminator``).
    """
    return json.dumps(_to_plain(program), indent=_JSON_INDENT)


def story_to_json(story: Story) -> str:
    """Serialize a Story to indented JSON.

    Args:
        story: The linearizer output to serialize.

    Returns:
        The JSON text. Line ranges become two-element arrays.
    """
    return json.dumps(_to_plain(story), indent=_JSON_INDENT)


def program_from_json(text: str) -> Program:
    """Rebuild a Program, nested blocks included, from ``program_to_json`` output.

    Args:
        text: JSON text of an IR document.

    Returns:
        The decoded Program.

    Raises:
        ValueError: if ``text`` is not JSON, its ``schema_version`` is not
            ``SCHEMA_VERSION``, or it doesn't have the IR's shape.
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("IR document must be a JSON object")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported IR schema_version {version!r}; "
            f"this Minstrel reads schema_version {SCHEMA_VERSION}"
        )
    try:
        return _program_from_dict(data)
    except (KeyError, TypeError) as error:
        # Missing keys, unknown keys and wrong container types all surface as
        # one ValueError, so callers only need to catch one exception type.
        raise ValueError(f"malformed IR document: {error!r}") from error


def _to_plain(value: Any) -> Any:
    """Convert dataclasses to JSON-ready dicts, tagging block nodes."""
    if is_dataclass(value):
        plain = {f.name: _to_plain(getattr(value, f.name)) for f in fields(value)}
        tag = _NODE_TAGS.get(type(value))
        return {"node": tag, **plain} if tag else plain
    if isinstance(value, list | tuple):
        return [_to_plain(item) for item in value]
    return value


def _program_from_dict(data: dict[str, Any]) -> Program:
    return Program(
        **{
            **data,
            "chunks": [_chunk_from_dict(chunk) for chunk in data["chunks"]],
            "diagnostics": [Diagnostic(**diag) for diag in data["diagnostics"]],
        }
    )


def _chunk_from_dict(data: dict[str, Any]) -> Chunk:
    return Chunk(**{**data, "body": _block_from_list(data["body"])})


def _block_from_list(items: list[dict[str, Any]]) -> Block:
    return [_node_from_dict(item) for item in items]


def _node_from_dict(data: dict[str, Any]) -> Node:
    """Rebuild one block node from its tagged dict; ValueError on unknown tags."""
    attrs = dict(data)
    tag = attrs.pop("node")
    node_type = _NODE_TYPES.get(tag)
    if node_type is None:
        raise ValueError(f"unknown block node type {tag!r}")
    if node_type is BranchPoint:
        attrs["arms"] = [_arm_from_dict(arm) for arm in attrs["arms"]]
    return node_type(**attrs)


def _arm_from_dict(data: dict[str, Any]) -> Arm:
    return Arm(
        **{
            **data,
            "body": _block_from_list(data["body"]),
            "partial": _block_from_list(data["partial"]),
        }
    )
