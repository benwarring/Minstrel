"""Canonical linearizer: a resolved Program and an entry chunk -> a Story.

The walk is depth-first from the entry, which mirrors execution: a callee and
everything it calls get pages before the caller's next call. Rules come from
docs/PLAN.md ("Branching and path selection", "Linearization rules"); the
binding details are in docs/decisions.md ("IR details (M1)", "M2 contracts").

The viewer's ``linearize.js`` is a line-for-line port of this module and must
agree with it on every fixture, so the algorithm stays plain and explicit.
Change both files together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from minstrel.ir import (
    Arm,
    Block,
    BranchPoint,
    CallNote,
    CallSite,
    Chunk,
    Diagnostic,
    Page,
    PageCall,
    Program,
    Selection,
    Story,
    StoryMode,
    Terminator,
)

DEFAULT_MAX_DEPTH = 50

_MODES = ("follow", "all")

# How a block's walk ended: "return" (a return or raise ends the rest of the
# chunk) or "break" (a break or continue ends the rest of the nearest loop
# arm). None means the block ran to its end.
_Status = Literal["return", "break"] | None


def linearize(
    program: Program,
    entry: str,
    selection: Selection | None = None,
    mode: StoryMode = "follow",
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Story:
    """Lay out the chunks reached from ``entry`` as a Story of ordered pages.

    Args:
        program: A resolved IR program (call targets and statuses filled in).
        entry: Chunk id of page 1.
        selection: The reader's choices: BranchPoint id -> Arm id, and
            ambiguous CallSite id -> chunk id. Missing or invalid entries
            mean the default (the branch's default arm, the call's first
            target).
        mode: ``follow`` walks only the selected arm of each branch point and
            the selected target of each ambiguous call, and records line
            ranges. ``all`` walks every arm and every target, sets
            ``via_arm`` on pages placed inside an arm, and leaves the ranges
            empty.
        max_depth: Deepest call depth a page may have (the entry is 0). A
            call that would place a page deeper records ``depth_limit`` and a
            diagnostic instead.

    Returns:
        The Story. Its ``not_on_path`` and ``never_reached`` appendices come
        from an ``all``-mode walk from the same entry with the same
        ``max_depth``.

    Raises:
        ValueError: if ``entry`` is not a chunk id in ``program``, ``mode`` is
            not ``follow`` or ``all``, or the walk nests deeper than Python's
            recursion limit allows.
    """
    chunks = {chunk.id: chunk for chunk in program.chunks}
    if entry not in chunks:
        raise ValueError(f"entry {entry!r} is not a chunk id in this program")
    if mode not in _MODES:
        raise ValueError(f"unknown story mode {mode!r}; expected 'follow' or 'all'")
    try:
        walk = _walk(chunks, entry, selection or {}, mode, max_depth)
        if mode == "all":
            overview = walk
        else:
            overview = _walk(chunks, entry, {}, "all", max_depth)
    except RecursionError as error:
        # As in ir.py and extract.py, Python's recursion limit is the cap. The
        # walk recurses a few frames per call level and per nested branch
        # point, so a huge max_depth on a deep call chain can exceed it.
        raise ValueError(
            f"calls from {entry!r} nest too deeply to linearize; try a lower max_depth"
        ) from error
    return Story(
        entry=entry,
        mode=mode,
        pages=walk.pages,
        not_on_path=_not_on_path(walk, overview),
        never_reached=_never_reached(program, overview),
        diagnostics=walk.diagnostics,
    )


def _walk(
    chunks: dict[str, Chunk],
    entry: str,
    selection: Selection,
    mode: StoryMode,
    max_depth: int,
) -> _Walker:
    """Run one depth-first walk from ``entry`` and return its finished state."""
    walker = _Walker(chunks, selection, mode, max_depth)
    walker.place(entry, None, None)
    return walker


def _not_on_path(walk: _Walker, overview: _Walker) -> list[str]:
    """Chunks the all-arms walk placed but ``walk`` didn't, in overview order.

    In "all" mode ``overview`` is ``walk`` itself, so the list is empty.
    """
    missing = []
    for page in overview.pages:
        if page.chunk_id not in walk.placed:
            missing.append(page.chunk_id)
    return missing


def _never_reached(program: Program, overview: _Walker) -> list[str]:
    """Chunks the all-arms walk never placed, in ``program.chunks`` order.

    Module top levels are left out: they run on import, not by call. The
    entry is page 1 of ``overview``, so it is never listed.
    """
    unreached = []
    for chunk in program.chunks:
        if chunk.kind != "module_toplevel" and chunk.id not in overview.placed:
            unreached.append(chunk.id)
    return unreached


def _selected_arm(branch: BranchPoint, selection: Selection) -> Arm | None:
    """Return the arm ``selection`` names for ``branch``, else its default arm.

    If ``default_arm`` names no arm either (malformed IR), the first arm is
    used, so its calls still show. None only for a branch with no arms.
    """
    for wanted in (selection.get(branch.id), branch.default_arm):
        for arm in branch.arms:
            if arm.id == wanted:
                return arm
    if branch.arms:
        return branch.arms[0]
    return None


@dataclass
class _Frame:
    """The page being walked, its chunk, and the call sites already handled."""

    page: Page
    chunk: Chunk
    seen_calls: set[str] = field(default_factory=set)


@dataclass
class _Walker:
    """State of one depth-first walk.

    Attributes:
        chunks: Every chunk in the program, by id.
        selection: Arm and target choices (used in "follow" mode only).
        mode: "follow" or "all".
        max_depth: Deepest depth a placed page may have.
        pages: Pages placed so far, in page order.
        placed: Chunk id -> index of its page.
        stack: Chunk ids from the entry down to the page being walked.
        diagnostics: Unresolved calls and depth-limit hits, in walk order.
    """

    chunks: dict[str, Chunk]
    selection: Selection
    mode: StoryMode
    max_depth: int
    pages: list[Page] = field(default_factory=list)
    placed: dict[str, int] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def place(
        self, chunk_id: str, reached_from: str | None, arm_label: str | None
    ) -> int:
        """Give ``chunk_id`` the next page, walk it completely, return its index.

        ``arm_label`` becomes the page's ``via_arm``: the innermost arm
        around the placing call site in the caller's chunk. It is always
        None in "follow" mode.
        """
        chunk = self.chunks[chunk_id]
        page = Page(
            index=len(self.pages) + 1,
            chunk_id=chunk_id,
            depth=len(self.stack),
            breadcrumb=self.stack + [chunk_id],
            reached_from=reached_from,
            via_arm=arm_label,
        )
        self.pages.append(page)
        self.placed[chunk_id] = page.index
        self.stack.append(chunk_id)
        # Any early stop just ends this chunk's walk, so the status is unused.
        self.walk_block(_Frame(page=page, chunk=chunk), chunk.body, False, None)
        self.stack.pop()
        return page.index

    def walk_block(
        self,
        frame: _Frame,
        block: Block,
        interrupted: bool,
        arm_label: str | None,
    ) -> _Status:
        """Walk ``block``'s nodes in order and return how the walk ended.

        ``interrupted`` is True inside an arm's ``partial`` block: its calls
        are marked ``maybe_interrupted`` and its terminators are ignored,
        since the block may stop anywhere.
        """
        for node in block:
            if isinstance(node, CallSite):
                self.walk_call(frame, node, interrupted, arm_label)
            elif isinstance(node, BranchPoint):
                if self.mode == "all":
                    status = self.walk_every_arm(frame, node, interrupted)
                else:
                    status = self.walk_selected_arm(frame, node, interrupted)
                if status is not None:
                    return status
            elif isinstance(node, Terminator) and not interrupted:
                return self.stop_at(frame, node)
        return None

    def walk_selected_arm(
        self, frame: _Frame, branch: BranchPoint, interrupted: bool
    ) -> _Status:
        """Walk only the selected arm, recording active and inactive ranges."""
        arm = _selected_arm(branch, self.selection)
        if arm is None:
            return None
        for other in branch.arms:
            span = (other.start_line, other.end_line)
            if other is arm:
                frame.page.active_ranges.append(span)
            else:
                frame.page.inactive_ranges.append(span)
        return self.walk_arm(frame, branch, arm, interrupted, None)

    def walk_every_arm(
        self, frame: _Frame, branch: BranchPoint, interrupted: bool
    ) -> _Status:
        """Walk every arm in order; stop only if every arm stopped.

        The result is "break" if any arm broke out, else "return".
        """
        if not branch.arms:
            return None
        statuses = []
        for arm in branch.arms:
            statuses.append(self.walk_arm(frame, branch, arm, interrupted, arm.label))
        if None in statuses:
            return None
        if "break" in statuses:
            return "break"
        return "return"

    def walk_arm(
        self,
        frame: _Frame,
        branch: BranchPoint,
        arm: Arm,
        interrupted: bool,
        arm_label: str | None,
    ) -> _Status:
        """Walk ``arm.partial`` (interrupted) then ``arm.body``.

        A loop branch point absorbs "break": break and continue end only the
        loop arm, and the walk goes on after the loop.
        """
        self.walk_block(frame, arm.partial, True, arm_label)
        status = self.walk_block(frame, arm.body, interrupted, arm_label)
        if status == "break" and branch.kind == "loop":
            return None
        return status

    def stop_at(self, frame: _Frame, terminator: Terminator) -> _Status:
        """Return the status ``terminator`` ends the walk with.

        In "follow" mode a return or raise also dims the rest of the chunk.
        """
        if terminator.kind in ("break", "continue"):
            return "break"
        # A return on the chunk's last line leaves nothing after it to dim.
        if self.mode == "follow" and terminator.line < frame.chunk.end_line:
            rest = (terminator.line + 1, frame.chunk.end_line)
            frame.page.inactive_ranges.append(rest)
        return "return"

    def walk_call(
        self,
        frame: _Frame,
        call: CallSite,
        interrupted: bool,
        arm_label: str | None,
    ) -> None:
        """Record a PageCall per target walked, placing new targets as pages."""
        # A try body sits in several arms under the same ids: handle it once.
        if call.id in frame.seen_calls:
            return
        frame.seen_calls.add(call.id)
        if call.status == "external":
            self.add_call(frame, call, None, "external", interrupted)
        elif call.status == "unresolved" or not call.targets:
            self.add_unresolved(frame, call, interrupted)
        else:
            for target in self.targets_to_walk(call):
                self.walk_target(frame, call, target, interrupted, arm_label)

    def targets_to_walk(self, call: CallSite) -> list[str]:
        """Every target in "all" mode, else the selected one (default first)."""
        if self.mode == "all":
            return call.targets
        chosen = self.selection.get(call.id)
        if chosen in call.targets:
            return [chosen]
        return [call.targets[0]]

    def walk_target(
        self,
        frame: _Frame,
        call: CallSite,
        target: str,
        interrupted: bool,
        arm_label: str | None,
    ) -> None:
        """Link ``call`` to one target chunk, placing it as a page if new."""
        if target not in self.chunks:
            # The resolver named a chunk the program doesn't have.
            self.add_unresolved(frame, call, interrupted)
        elif target in self.stack:
            self.add_call(frame, call, self.placed[target], "recursive", interrupted)
        elif target in self.placed:
            self.add_call(frame, call, self.placed[target], "see_page", interrupted)
        elif frame.page.depth + 1 > self.max_depth:
            message = (
                f"call depth limit {self.max_depth} reached; {target!r} not placed"
            )
            self.diagnose(frame, call, "depth_limit", message)
            self.add_call(frame, call, None, "depth_limit", interrupted)
        else:
            index = self.place(target, call.id, arm_label)
            self.add_call(frame, call, index, "placed", interrupted)

    def add_unresolved(self, frame: _Frame, call: CallSite, interrupted: bool) -> None:
        """Record ``call`` as unresolved, with a diagnostic at its line."""
        message = f"cannot resolve call to {call.callee_text!r}"
        self.diagnose(frame, call, "unresolved_call", message)
        self.add_call(frame, call, None, "unresolved", interrupted)

    def add_call(
        self,
        frame: _Frame,
        call: CallSite,
        target_page: int | None,
        note: CallNote,
        interrupted: bool,
    ) -> None:
        """Append one PageCall for ``call`` to the page being walked."""
        frame.page.calls.append(
            PageCall(
                call_site_id=call.id,
                target_page=target_page,
                note=note,
                conditional=call.conditional,
                maybe_interrupted=interrupted,
            )
        )

    def diagnose(self, frame: _Frame, call: CallSite, code: str, message: str) -> None:
        """Add a story diagnostic located at ``call`` in the page's file."""
        self.diagnostics.append(
            Diagnostic(
                code=code, message=message, file=frame.chunk.file, line=call.line
            )
        )
