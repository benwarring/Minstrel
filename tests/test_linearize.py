"""Tests for the canonical linearizer in ``minstrel.core.linearize``.

Every Program here is built by hand from ``minstrel.ir`` dataclasses, so a
failure points at the linearizer, not extraction. Expected pages, calls,
ranges and appendices are derived from docs/PLAN.md ("Branching and path
selection", "Linearization rules") and docs/decisions.md ("IR details (M1)",
"M2 contracts"), never copied from the linearizer's output.

All chunks live in module ``app``: the chunk named ``main`` has id
``app:main``. The builders below take bare names to keep each case short.
"""

from dataclasses import replace
from typing import Any

import pytest

from minstrel.core.linearize import linearize
from minstrel.ir import (
    Arm,
    BranchKind,
    BranchPoint,
    CallSite,
    CallStatus,
    Chunk,
    ChunkKind,
    Node,
    Page,
    PageCall,
    Program,
    Selection,
    Story,
    Terminator,
)

FILE = "app.py"
# Last line of every built chunk unless a case sets ``end``.
END = 20
# A chain one call longer than the default max_depth of 50 allows.
PAST_DEFAULT_DEPTH = 52
# Each call level costs the walk several Python frames, so this chain is well
# past the default recursion limit of 1000.
TOO_DEEP_CHAIN = 3_000


def _id(name: str) -> str:
    """Return the id of the chunk named ``name`` in module ``app``."""
    return f"app:{name}"


def _chunk(
    name: str,
    *body: Node,
    kind: ChunkKind = "function",
    file: str = FILE,
    end: int = END,
) -> Chunk:
    """Return a chunk spanning lines 1 to ``end`` whose body is ``body``."""
    return Chunk(
        id=_id(name),
        kind=kind,
        name=name,
        file=file,
        start_line=1,
        end_line=end,
        source="",
        body=list(body),
    )


def _call(
    owner: str,
    n: int,
    *targets: str,
    line: int = 2,
    status: CallStatus | None = None,
    conditional: bool = False,
) -> CallSite:
    """Return call site ``n`` of chunk ``owner``, calling the chunks ``targets``.

    The status defaults from the target count: none is unresolved, one is
    resolved, several are ambiguous.
    """
    if status is None:
        status = {0: "unresolved", 1: "resolved"}.get(len(targets), "ambiguous")
    return CallSite(
        id=f"{_id(owner)}#call{n}",
        line=line,
        col=4,
        callee_text=targets[0] if targets else "missing",
        targets=[_id(target) for target in targets],
        status=status,
        conditional=conditional,
    )


def _arm(
    label: str, start: int, end: int, *body: Node, partial: tuple[Node, ...] = ()
) -> Arm:
    """Return an arm spanning ``start`` to ``end``; ``_branch`` assigns its id."""
    return Arm(
        id="",
        label=label,
        start_line=start,
        end_line=end,
        body=list(body),
        partial=list(partial),
    )


def _branch_id(owner: str, n: int) -> str:
    return f"{_id(owner)}#branch{n}"


def _branch(
    owner: str, n: int, *arms: Arm, kind: BranchKind = "if", default: int = 1
) -> BranchPoint:
    """Return branch point ``n`` of ``owner`` with ``arms`` numbered from 1.

    ``default`` is the 1-based number of the default arm.
    """
    branch_id = _branch_id(owner, n)
    numbered = [
        replace(arm, id=f"{branch_id}.arm{index}")
        for index, arm in enumerate(arms, start=1)
    ]
    return BranchPoint(
        id=branch_id,
        kind=kind,
        line=numbered[0].start_line,
        arms=numbered,
        default_arm=numbered[default - 1].id,
    )


def _choose(owner: str, branch: int, arm: int) -> Selection:
    """Return a Selection picking arm ``arm`` of branch ``branch`` of ``owner``."""
    branch_id = _branch_id(owner, branch)
    return {branch_id: f"{branch_id}.arm{arm}"}


def _chain(length: int) -> list[Chunk]:
    """Return chunks ``c0`` to ``c<length - 1>``, each calling the next."""
    chunks = []
    for index in range(length):
        body = [_call(f"c{index}", 1, f"c{index + 1}")] if index + 1 < length else []
        chunks.append(_chunk(f"c{index}", *body))
    return chunks


def _program(*chunks: Chunk) -> Program:
    return Program(language="python", files=[FILE], chunks=list(chunks))


def _linearize(*chunks: Chunk, entry: str = "main", **options: Any) -> Story:
    """Linearize a program of ``chunks`` from the chunk named ``entry``."""
    return linearize(_program(*chunks), _id(entry), **options)


def _names(chunk_ids: list[str]) -> list[str]:
    return [chunk_id.removeprefix("app:") for chunk_id in chunk_ids]


def _order(story: Story) -> list[str]:
    """Return the chunk names of ``story``'s pages, in page order."""
    return _names([page.chunk_id for page in story.pages])


def _page(story: Story, name: str) -> Page:
    """Return the page of the chunk named ``name``."""
    return next(page for page in story.pages if page.chunk_id == _id(name))


def _calls(page: Page) -> list[tuple[str, int | None, str]]:
    """Return ``(callN, target_page, note)`` for each of ``page``'s calls."""
    return [
        (call.call_site_id.partition("#")[2], call.target_page, call.note)
        for call in page.calls
    ]


def _diagnostics(story: Story) -> list[tuple[str, str | None, int | None]]:
    """Return ``(code, file, line)`` per diagnostic; messages aren't compared."""
    return [(diag.code, diag.file, diag.line) for diag in story.diagnostics]


@pytest.fixture
def if_chunks() -> list[Chunk]:
    """main: check() before an if; a() in "if ok" (3-4), b() in "else" (5-6).

    c() follows the branch at line 8.
    """
    return [
        _chunk(
            "main",
            _call("main", 1, "check", line=2),
            _branch(
                "main",
                1,
                _arm("if ok", 3, 4, _call("main", 2, "a", line=4)),
                _arm("else", 5, 6, _call("main", 3, "b", line=6)),
            ),
            _call("main", 4, "c", line=8),
        ),
        _chunk("check"),
        _chunk("a"),
        _chunk("b"),
        _chunk("c"),
    ]


@pytest.fixture
def early_return_chunks() -> list[Chunk]:
    """main (1-10): "if done" (2-3) returns at 3; "else" (4-5) calls a().

    b() follows the branch at line 7.
    """
    return [
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm("if done", 2, 3, Terminator("return", 3)),
                _arm("else", 4, 5, _call("main", 1, "a", line=5)),
            ),
            _call("main", 2, "b", line=7),
            end=10,
        ),
        _chunk("a"),
        _chunk("b"),
    ]


@pytest.fixture
def try_chunks() -> list[Chunk]:
    """main (1-12): a try at line 2, then after() at line 9.

    The try body (3-5) calls risky() and an unresolved name, then returns at
    line 5. "no exception" (2-5) walks it; "except ValueError" (6-7) has it as
    ``partial`` and calls handle() at line 7. Both arms share the call ids.
    """
    try_body = (
        _call("main", 1, "risky", line=3),
        _call("main", 2, line=4),
        Terminator("return", 5),
    )
    return [
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm("no exception", 2, 5, *try_body),
                _arm(
                    "except ValueError",
                    6,
                    7,
                    _call("main", 3, "handle", line=7),
                    partial=try_body,
                ),
                kind="try",
            ),
            _call("main", 4, "after", line=9),
            end=12,
        ),
        _chunk("risky"),
        _chunk("handle"),
        _chunk("after"),
    ]


@pytest.fixture
def ambiguous_chunks() -> list[Chunk]:
    """main's only call may go to A.run or B.run, in that order."""
    return [
        _chunk("main", _call("main", 1, "A.run", "B.run")),
        _chunk("A.run"),
        _chunk("B.run"),
    ]


@pytest.fixture
def appendix_chunks() -> list[Chunk]:
    """main's "if fast" arm calls x(); its "else" arm calls z() then y().

    The program also has a module top level and two chunks nothing reaches,
    listed out of alphabetical order.
    """
    return [
        _chunk("<module>", _call("<module>", 1, "main"), kind="module_toplevel"),
        _chunk("zombie"),
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm("if fast", 2, 3, _call("main", 1, "x", line=3)),
                _arm(
                    "else",
                    4,
                    6,
                    _call("main", 2, "z", line=5),
                    _call("main", 3, "y", line=6),
                ),
            ),
        ),
        _chunk("x"),
        _chunk("y"),
        _chunk("z"),
        _chunk("alpha"),
    ]


# Rules 1-2: the entry is page 1, and each new callee is walked completely
# before the caller's next call.


def test_hello_world_places_entry_then_its_callee() -> None:
    story = _linearize(
        _chunk("helloWorld", _call("helloWorld", 1, status="external")),
        _chunk("main", _call("main", 1, "helloWorld")),
    )

    assert story == Story(
        entry="app:main",
        mode="follow",
        pages=[
            Page(
                index=1,
                chunk_id="app:main",
                depth=0,
                breadcrumb=["app:main"],
                calls=[PageCall("app:main#call1", 2, "placed")],
            ),
            Page(
                index=2,
                chunk_id="app:helloWorld",
                depth=1,
                breadcrumb=["app:main", "app:helloWorld"],
                reached_from="app:main#call1",
                calls=[PageCall("app:helloWorld#call1", None, "external")],
            ),
        ],
    )


def test_entry_without_calls_is_the_only_page() -> None:
    story = _linearize(_chunk("main"), _chunk("unused"))

    assert story.pages == [Page(1, "app:main", 0, ["app:main"])]
    assert story.not_on_path == []
    assert story.never_reached == ["app:unused"]
    assert story.diagnostics == []


def test_chain_is_walked_depth_first() -> None:
    story = _linearize(
        _chunk(
            "main",
            _call("main", 1, "load", line=2),
            _call("main", 2, "report", line=3),
        ),
        _chunk("load", _call("load", 1, "parse")),
        _chunk("parse", _call("parse", 1, "validate")),
        _chunk("validate"),
        _chunk("report"),
    )

    main, load, parse, validate = (
        _id(name) for name in ("main", "load", "parse", "validate")
    )
    assert [page.index for page in story.pages] == [1, 2, 3, 4, 5]
    assert [
        (page.chunk_id, page.depth, page.breadcrumb, page.reached_from)
        for page in story.pages
    ] == [
        (main, 0, [main], None),
        (load, 1, [main, load], "app:main#call1"),
        (parse, 2, [main, load, parse], "app:load#call1"),
        (validate, 3, [main, load, parse, validate], "app:parse#call1"),
        ("app:report", 1, [main, "app:report"], "app:main#call2"),
    ]
    assert _calls(_page(story, "main")) == [
        ("call1", 2, "placed"),
        ("call2", 5, "placed"),
    ]


def test_conditional_call_is_walked_and_badged() -> None:
    story = _linearize(
        _chunk("main", _call("main", 1, "a", conditional=True)), _chunk("a")
    )

    assert _order(story) == ["main", "a"]
    assert _page(story, "main").calls == [
        PageCall("app:main#call1", 2, "placed", conditional=True)
    ]


# Rules 4-5: calls to placed chunks link back instead of adding pages.


def test_repeated_helper_links_to_its_first_page() -> None:
    story = _linearize(
        _chunk(
            "main",
            _call("main", 1, "log", line=2),
            _call("main", 2, "work", line=3),
            _call("main", 3, "log", line=4),
        ),
        _chunk("work", _call("work", 1, "log")),
        _chunk("log", _call("log", 1, status="external")),
    )

    assert _order(story) == ["main", "log", "work"]
    assert _calls(_page(story, "main")) == [
        ("call1", 2, "placed"),
        ("call2", 3, "placed"),
        ("call3", 2, "see_page"),
    ]
    assert _calls(_page(story, "work")) == [("call1", 2, "see_page")]


def test_direct_recursion_links_back_as_recursive() -> None:
    story = _linearize(
        _chunk(
            "main",
            _call("main", 1, "factorial", line=2),
            _call("main", 2, line=3, status="external"),
        ),
        _chunk("factorial", _call("factorial", 1, "factorial")),
    )

    assert _order(story) == ["main", "factorial"]
    assert _calls(_page(story, "main")) == [
        ("call1", 2, "placed"),
        ("call2", None, "external"),
    ]
    assert _calls(_page(story, "factorial")) == [("call1", 2, "recursive")]


def test_mutual_recursion_is_recursive_on_the_stack_and_see_page_after() -> None:
    story = _linearize(
        _chunk(
            "main",
            _call("main", 1, "is_even", line=2),
            _call("main", 2, "is_odd", line=3),
        ),
        _chunk("is_even", _call("is_even", 1, "is_odd")),
        _chunk(
            "is_odd",
            _call("is_odd", 1, "is_even", line=2),
            _call("is_odd", 2, "main", line=3),
        ),
    )

    assert _order(story) == ["main", "is_even", "is_odd"]
    assert _calls(_page(story, "is_even")) == [("call1", 3, "placed")]
    assert _calls(_page(story, "is_odd")) == [
        ("call1", 2, "recursive"),
        ("call2", 1, "recursive"),
    ]
    # Once is_even and is_odd have returned, they are off the stack.
    assert _calls(_page(story, "main")) == [
        ("call1", 2, "placed"),
        ("call2", 3, "see_page"),
    ]


# Rules 6-7: external and unresolved calls add no page.


def test_external_and_unresolved_calls_add_no_page() -> None:
    story = _linearize(
        _chunk(
            "main",
            _call("main", 1, line=3, status="external"),
            _call("main", 2, "helper", line=5),
        ),
        _chunk("helper", _call("helper", 1, line=7), file="lib.py"),
    )

    assert _order(story) == ["main", "helper"]
    assert _calls(_page(story, "main")) == [
        ("call1", None, "external"),
        ("call2", 2, "placed"),
    ]
    assert _calls(_page(story, "helper")) == [("call1", None, "unresolved")]
    # The diagnostic is in the file of the chunk that makes the call.
    assert _diagnostics(story) == [("unresolved_call", "lib.py", 7)]


def test_target_missing_from_program_is_unresolved() -> None:
    story = _linearize(_chunk("main", _call("main", 1, "gone", line=4)))

    assert _order(story) == ["main"]
    assert _calls(_page(story, "main")) == [("call1", None, "unresolved")]
    assert _diagnostics(story) == [("unresolved_call", FILE, 4)]


# Rule 8: the depth limit.


def test_depth_limit_records_note_and_diagnostic() -> None:
    story = _linearize(
        _chunk("main", _call("main", 1, "a")),
        _chunk("a", _call("a", 1, "b", line=6), _call("a", 2, "main", line=7)),
        _chunk("b"),
        max_depth=1,
    )

    assert [(page.chunk_id, page.depth) for page in story.pages] == [
        ("app:main", 0),
        ("app:a", 1),
    ]
    # The call back to main places no page, so the limit doesn't apply to it.
    assert _calls(_page(story, "a")) == [
        ("call1", None, "depth_limit"),
        ("call2", 1, "recursive"),
    ]
    assert _diagnostics(story) == [("depth_limit", FILE, 6)]
    # The appendix walk uses the same max_depth, so it never places b either.
    assert story.not_on_path == []
    assert story.never_reached == ["app:b"]


@pytest.mark.parametrize(
    ("max_depth", "pages", "limit_hits"),
    [
        (0, ["c0"], 1),
        (1, ["c0", "c1"], 1),
        (2, ["c0", "c1", "c2"], 0),
        (3, ["c0", "c1", "c2"], 0),
    ],
)
def test_max_depth_is_the_deepest_depth_a_page_may_have(
    max_depth: int, pages: list[str], limit_hits: int
) -> None:
    story = _linearize(*_chain(3), entry="c0", max_depth=max_depth)

    assert _order(story) == pages
    assert _diagnostics(story) == [("depth_limit", FILE, 2)] * limit_hits


def test_default_max_depth_is_fifty() -> None:
    story = _linearize(*_chain(PAST_DEFAULT_DEPTH), entry="c0")

    assert len(story.pages) == 51
    assert story.pages[-1].chunk_id == "app:c50"
    assert story.pages[-1].depth == 50
    assert _calls(story.pages[-1]) == [("call1", None, "depth_limit")]
    assert _diagnostics(story) == [("depth_limit", FILE, 2)]


def test_calls_nested_past_the_recursion_limit_raise_value_error() -> None:
    with pytest.raises(ValueError):
        _linearize(*_chain(TOO_DEEP_CHAIN), entry="c0", max_depth=TOO_DEEP_CHAIN)


# Rule 9: the appendices.


@pytest.mark.parametrize(
    ("selection", "mode", "pages", "not_on_path"),
    [
        (None, "follow", ["main", "x"], ["z", "y"]),
        (_choose("main", 1, 2), "follow", ["main", "z", "y"], ["x"]),
        (None, "all", ["main", "x", "z", "y"], []),
    ],
    ids=["default_arm", "selected_arm", "all_mode"],
)
def test_appendices_list_chunks_off_this_path_and_never_reached(
    appendix_chunks: list[Chunk],
    selection: Selection | None,
    mode: str,
    pages: list[str],
    not_on_path: list[str],
) -> None:
    story = _linearize(*appendix_chunks, selection=selection, mode=mode)

    assert _order(story) == pages
    # Order of first reach in an all-arms walk, not program order.
    assert _names(story.not_on_path) == not_on_path
    # Program order; the module top level and the entry are left out.
    assert _names(story.never_reached) == ["zombie", "alpha"]


def test_diagnostics_come_only_from_the_requested_walk() -> None:
    main = _chunk(
        "main",
        _branch(
            "main", 1, _arm("if a", 2, 3), _arm("else", 4, 5, _call("main", 1, line=5))
        ),
    )

    assert _linearize(main).diagnostics == []
    assert _diagnostics(_linearize(main, mode="all")) == [("unresolved_call", FILE, 5)]


# Branch points in "follow" mode.


def test_branch_walks_default_arm_without_selection(if_chunks: list[Chunk]) -> None:
    story = _linearize(*if_chunks)
    main = _page(story, "main")

    assert _order(story) == ["main", "check", "a", "c"]
    assert _calls(main) == [
        ("call1", 2, "placed"),
        ("call2", 3, "placed"),
        ("call4", 4, "placed"),
    ]
    assert main.active_ranges == [(3, 4)]
    assert main.inactive_ranges == [(5, 6)]
    assert [page.via_arm for page in story.pages] == [None] * 4


def test_branch_walks_selected_arm(if_chunks: list[Chunk]) -> None:
    story = _linearize(*if_chunks, selection=_choose("main", 1, 2))
    main = _page(story, "main")

    assert _order(story) == ["main", "check", "b", "c"]
    assert _calls(main) == [
        ("call1", 2, "placed"),
        ("call3", 3, "placed"),
        ("call4", 4, "placed"),
    ]
    assert main.active_ranges == [(5, 6)]
    assert main.inactive_ranges == [(3, 4)]


@pytest.mark.parametrize("arm_id", ["app:main#branch1.arm9", ""])
def test_invalid_selection_falls_back_to_default_arm(
    if_chunks: list[Chunk], arm_id: str
) -> None:
    story = _linearize(*if_chunks, selection={_branch_id("main", 1): arm_id})

    assert _order(story) == ["main", "check", "a", "c"]
    assert _page(story, "main").active_ranges == [(3, 4)]


def test_branch_default_arm_need_not_be_the_first() -> None:
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm("if a", 2, 3, _call("main", 1, "a", line=3)),
                _arm("else", 4, 5, _call("main", 2, "b", line=5)),
                default=2,
            ),
        ),
        _chunk("a"),
        _chunk("b"),
    )

    assert _order(story) == ["main", "b"]
    assert _page(story, "main").active_ranges == [(4, 5)]


def test_nested_branch_records_ranges_of_every_selected_arm() -> None:
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm(
                    "if a",
                    2,
                    8,
                    _branch("main", 2, _arm("if b", 3, 4), _arm("else", 5, 6)),
                ),
                _arm("else", 9, 10),
            ),
        )
    )
    main = _page(story, "main")

    assert sorted(main.active_ranges) == [(2, 8), (3, 4)]
    assert sorted(main.inactive_ranges) == [(5, 6), (9, 10)]


@pytest.mark.parametrize("kind", ["return", "raise"])
def test_return_stops_the_chunk_and_dims_the_rest(kind: str) -> None:
    story = _linearize(
        _chunk(
            "main",
            _call("main", 1, "a", line=2),
            Terminator(kind, 4),
            _call("main", 2, "b", line=6),
            end=8,
        ),
        _chunk("a"),
        _chunk("b"),
    )
    main = _page(story, "main")

    assert _order(story) == ["main", "a"]
    assert _calls(main) == [("call1", 2, "placed")]
    assert main.active_ranges == []
    assert main.inactive_ranges == [(5, 8)]
    # b is after the return on every path.
    assert story.never_reached == ["app:b"]


def test_return_on_the_last_line_dims_nothing() -> None:
    story = _linearize(
        _chunk("main", _call("main", 1, "a"), Terminator("return", 8), end=8),
        _chunk("a"),
    )

    assert _page(story, "main").inactive_ranges == []


def test_return_in_selected_arm_stops_the_chunk(
    early_return_chunks: list[Chunk],
) -> None:
    story = _linearize(*early_return_chunks)
    main = _page(story, "main")

    assert _order(story) == ["main"]
    assert main.active_ranges == [(2, 3)]
    assert sorted(main.inactive_ranges) == [(4, 5), (4, 10)]
    assert _names(story.not_on_path) == ["a", "b"]


def test_selecting_the_other_arm_walks_past_the_return(
    early_return_chunks: list[Chunk],
) -> None:
    story = _linearize(*early_return_chunks, selection=_choose("main", 1, 2))
    main = _page(story, "main")

    assert _order(story) == ["main", "a", "b"]
    assert main.active_ranges == [(4, 5)]
    assert main.inactive_ranges == [(2, 3)]


def test_return_in_a_callee_does_not_stop_the_caller() -> None:
    story = _linearize(
        _chunk("main", _call("main", 1, "a", line=2), _call("main", 2, "c", line=3)),
        _chunk("a", Terminator("return", 3), _call("a", 1, "b", line=5)),
        _chunk("b"),
        _chunk("c"),
    )

    assert _order(story) == ["main", "a", "c"]
    assert _page(story, "a").inactive_ranges == [(4, END)]
    assert _page(story, "main").inactive_ranges == []


# Try arms: the except arm's partial block.


def test_no_exception_arm_returns_from_the_try_body(try_chunks: list[Chunk]) -> None:
    story = _linearize(*try_chunks)
    main = _page(story, "main")

    assert _order(story) == ["main", "risky"]
    assert main.calls == [
        PageCall("app:main#call1", 2, "placed"),
        PageCall("app:main#call2", None, "unresolved"),
    ]
    assert main.active_ranges == [(2, 5)]
    assert sorted(main.inactive_ranges) == [(6, 7), (6, 12)]
    assert _diagnostics(story) == [("unresolved_call", FILE, 4)]


def test_except_arm_walks_try_body_as_maybe_interrupted(
    try_chunks: list[Chunk],
) -> None:
    story = _linearize(*try_chunks, selection=_choose("main", 1, 2))
    main = _page(story, "main")

    # The return in the partial try body is ignored, so after() still runs.
    assert _order(story) == ["main", "risky", "handle", "after"]
    assert main.calls == [
        PageCall("app:main#call1", 2, "placed", maybe_interrupted=True),
        PageCall("app:main#call2", None, "unresolved", maybe_interrupted=True),
        PageCall("app:main#call3", 3, "placed"),
        PageCall("app:main#call4", 4, "placed"),
    ]
    assert main.active_ranges == [(6, 7)]
    assert main.inactive_ranges == [(2, 5)]


def test_terminators_nested_in_a_partial_block_are_ignored() -> None:
    inner_if = _branch(
        "main",
        2,
        _arm("if bad", 3, 4, _call("main", 1, "x", line=3), Terminator("raise", 4)),
        _arm("else", 5, 5),
    )
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm("no exception", 2, 5, inner_if),
                _arm(
                    "except",
                    6,
                    7,
                    _call("main", 2, "handle", line=7),
                    partial=(inner_if,),
                ),
                kind="try",
            ),
            _call("main", 3, "after", line=9),
            end=12,
        ),
        _chunk("x"),
        _chunk("handle"),
        _chunk("after"),
        selection=_choose("main", 1, 2),
    )

    assert _order(story) == ["main", "x", "handle", "after"]
    assert _page(story, "main").calls == [
        PageCall("app:main#call1", 2, "placed", maybe_interrupted=True),
        PageCall("app:main#call2", 3, "placed"),
        PageCall("app:main#call3", 4, "placed"),
    ]


def test_call_site_shared_by_two_arms_is_listed_once(try_chunks: list[Chunk]) -> None:
    story = _linearize(*try_chunks, mode="all")

    assert _order(story) == ["main", "risky", "handle", "after"]
    assert _calls(_page(story, "main")) == [
        ("call1", 2, "placed"),
        ("call2", None, "unresolved"),
        ("call3", 3, "placed"),
        ("call4", 4, "placed"),
    ]
    assert _diagnostics(story) == [("unresolved_call", FILE, 4)]


# Loop arms: break and continue end the loop arm, not the chunk.


@pytest.mark.parametrize("kind", ["break", "continue"])
def test_loop_arm_absorbs_break_and_the_walk_continues(kind: str) -> None:
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm(
                    "loop body runs",
                    2,
                    5,
                    _call("main", 1, "a", line=3),
                    Terminator(kind, 4),
                    _call("main", 2, "b", line=5),
                ),
                _arm("loop body skipped", 2, 2),
                kind="loop",
            ),
            _call("main", 3, "c", line=7),
            end=10,
        ),
        _chunk("a"),
        _chunk("b"),
        _chunk("c"),
    )

    assert _order(story) == ["main", "a", "c"]
    # Only the unselected arm is dimmed: a break doesn't dim the rest.
    assert _page(story, "main").inactive_ranges == [(2, 2)]
    assert story.never_reached == ["app:b"]


def test_break_inside_an_if_propagates_to_the_loop() -> None:
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm(
                    "loop body runs",
                    2,
                    6,
                    _branch(
                        "main",
                        2,
                        _arm("if done", 3, 4, Terminator("break", 4)),
                        _arm("else", 5, 5),
                    ),
                    _call("main", 1, "b", line=6),
                ),
                _arm("loop body skipped", 2, 2),
                kind="loop",
            ),
            _call("main", 2, "c", line=8),
            end=12,
        ),
        _chunk("b"),
        _chunk("c"),
    )

    assert _order(story) == ["main", "c"]
    assert sorted(_page(story, "main").inactive_ranges) == [(2, 2), (5, 5)]
    assert story.not_on_path == ["app:b"]


def test_return_inside_a_loop_stops_the_chunk() -> None:
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm("loop body runs", 2, 4, Terminator("return", 3)),
                _arm("loop body skipped", 2, 2),
                kind="loop",
            ),
            _call("main", 1, "c", line=6),
            end=10,
        ),
        _chunk("c"),
    )

    assert _order(story) == ["main"]
    assert (4, 10) in _page(story, "main").inactive_ranges
    assert story.not_on_path == ["app:c"]


# "all" mode ("show everything").


def test_all_mode_walks_every_arm_and_labels_pages() -> None:
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm(
                    "if a",
                    2,
                    5,
                    _call("main", 1, "x", line=3),
                    _branch(
                        "main",
                        2,
                        _arm("if b", 4, 4, _call("main", 2, "w", line=4)),
                        _arm("otherwise", 5, 5),
                    ),
                ),
                _arm("else", 6, 7, _call("main", 3, "y", line=7)),
            ),
            _call("main", 4, "z", line=9),
        ),
        _chunk("x", _call("x", 1, "q")),
        _chunk("y"),
        _chunk("z"),
        _chunk("w"),
        _chunk("q"),
        mode="all",
    )

    assert story.mode == "all"
    # via_arm is the innermost arm in the caller's own chunk: q is placed by
    # x, which has no branch, so it gets no label from main's arm.
    assert [(page.chunk_id, page.via_arm) for page in story.pages] == [
        ("app:main", None),
        ("app:x", "if a"),
        ("app:q", None),
        ("app:w", "if b"),
        ("app:y", "else"),
        ("app:z", None),
    ]
    for page in story.pages:
        assert page.active_ranges == []
        assert page.inactive_ranges == []
    assert story.not_on_path == []


def test_all_mode_continues_after_a_branch_where_one_arm_returns() -> None:
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm("if done", 2, 3, Terminator("return", 3)),
                _arm("else", 4, 5, _call("main", 1, "y", line=5)),
            ),
            _call("main", 2, "z", line=7),
            end=10,
        ),
        _chunk("y"),
        _chunk("z"),
        mode="all",
    )

    assert _order(story) == ["main", "y", "z"]
    assert _page(story, "main").inactive_ranges == []


def test_all_mode_stops_after_a_branch_where_every_arm_returns() -> None:
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm("if done", 2, 3, Terminator("return", 3)),
                _arm(
                    "else", 4, 5, _call("main", 1, "y", line=4), Terminator("raise", 5)
                ),
            ),
            _call("main", 2, "z", line=7),
            end=10,
        ),
        _chunk("y"),
        _chunk("z"),
        mode="all",
    )

    assert _order(story) == ["main", "y"]
    assert _page(story, "main").inactive_ranges == []
    assert story.never_reached == ["app:z"]


def test_all_mode_break_from_any_arm_is_absorbed_by_the_loop() -> None:
    # If the inner if reported "return" instead of "break", both loop arms
    # would stop the chunk and c() would never be walked.
    story = _linearize(
        _chunk(
            "main",
            _branch(
                "main",
                1,
                _arm(
                    "loop body runs",
                    2,
                    8,
                    _branch(
                        "main",
                        2,
                        _arm("if done", 3, 4, Terminator("break", 4)),
                        _arm("else", 5, 6, Terminator("return", 6)),
                    ),
                    Terminator("return", 8),
                ),
                _arm("loop body skipped", 7, 8, Terminator("return", 8)),
                kind="loop",
            ),
            _call("main", 1, "c", line=10),
            end=12,
        ),
        _chunk("c"),
        mode="all",
    )

    assert _order(story) == ["main", "c"]


# Ambiguous calls: a choice point keyed by the call site id.


@pytest.mark.parametrize(
    ("selection", "chosen", "other"),
    [
        (None, "A.run", "B.run"),
        ({"app:main#call1": "app:B.run"}, "B.run", "A.run"),
        ({"app:main#call1": "app:C.run"}, "A.run", "B.run"),
    ],
    ids=["default_first_target", "selected_target", "invalid_selection"],
)
def test_follow_mode_walks_one_ambiguous_target(
    ambiguous_chunks: list[Chunk],
    selection: Selection | None,
    chosen: str,
    other: str,
) -> None:
    story = _linearize(*ambiguous_chunks, selection=selection)

    assert _order(story) == ["main", chosen]
    assert _calls(_page(story, "main")) == [("call1", 2, "placed")]
    assert _names(story.not_on_path) == [other]


def test_all_mode_walks_every_ambiguous_target(ambiguous_chunks: list[Chunk]) -> None:
    story = _linearize(*ambiguous_chunks, mode="all")

    assert _order(story) == ["main", "A.run", "B.run"]
    assert _calls(_page(story, "main")) == [
        ("call1", 2, "placed"),
        ("call1", 3, "placed"),
    ]
    # Candidates aren't arms, so they set no via_arm.
    assert [page.via_arm for page in story.pages] == [None, None, None]


# Errors.


def test_unknown_entry_raises_value_error() -> None:
    with pytest.raises(ValueError):
        _linearize(_chunk("main"), entry="nope")


def test_unknown_mode_raises_value_error() -> None:
    with pytest.raises(ValueError):
        _linearize(_chunk("main"), mode="everything")
