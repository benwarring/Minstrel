"""Tests for the ``minstrel print`` outline in ``minstrel.render.text``.

The linearizer isn't written yet, so stories are built by hand from
``minstrel.ir``. Assertions check content and order, not column padding.
Control characters are built with ``chr()`` so none hide in this file.
"""

import unicodedata

import pytest

from minstrel.ir import (
    Arm,
    BranchPoint,
    CallSite,
    Chunk,
    Diagnostic,
    Page,
    PageCall,
    Program,
    Story,
)
from minstrel.render.text import escape_control_chars, render_text

MAIN = "app:main"
LOAD = "app:load"
ESC = chr(27)
# Expected escapes are built from this, not typed as backslash sequences,
# so no tool can decode one into the raw character it stands for.
BACKSLASH = chr(92)
# The categories docs/decisions.md says must never reach the terminal raw.
UNSAFE_CATEGORIES = {"Cc", "Cf", "Cs", "Zl", "Zp"}


def _site(chunk_id: str, number: int, callee: str) -> CallSite:
    return CallSite(f"{chunk_id}#call{number}", line=number, col=4, callee_text=callee)


def _line(text: str, first_word: str) -> str:
    """Return the one line of ``text`` whose first word is ``first_word``."""
    matches = [line for line in text.split("\n") if line.split()[:1] == [first_word]]
    assert len(matches) == 1, matches
    return matches[0]


def _assert_in_order(text: str, *parts: str) -> None:
    """Assert each of ``parts`` occurs in ``text``, each after the one before."""
    position = 0
    for part in parts:
        found = text.find(part, position)
        assert found != -1, f"{part!r} missing after position {position} in {text!r}"
        position = found + len(part)


@pytest.fixture
def program() -> Program:
    """main calls load, then cache.get (in a try body) and reload (in the
    handler); load calls main, obj.method and deep."""
    try_branch = BranchPoint(
        id=f"{MAIN}#branch1",
        kind="try",
        line=3,
        arms=[
            Arm(
                id=f"{MAIN}#branch1.arm1",
                label="except OSError",
                start_line=3,
                end_line=6,
                partial=[_site(MAIN, 2, "cache.get")],
                body=[_site(MAIN, 3, "reload")],
            )
        ],
        default_arm=f"{MAIN}#branch1.arm1",
    )
    main = Chunk(
        id=MAIN,
        kind="function",
        name="main",
        file="app.py",
        start_line=1,
        end_line=8,
        source="",
        body=[_site(MAIN, 1, "load"), try_branch],
    )
    load = Chunk(
        id=LOAD,
        kind="function",
        name="load",
        file="pkg/load.py",
        start_line=10,
        end_line=14,
        source="",
        body=[
            _site(LOAD, 1, "main"),
            _site(LOAD, 2, "obj.method"),
            _site(LOAD, 3, "deep"),
        ],
    )
    return Program(
        language="python", files=["app.py", "pkg/load.py"], chunks=[main, load]
    )


@pytest.fixture
def story() -> Story:
    """Two pages, main then load, with one call of each note between them."""
    main_calls = [
        PageCall(f"{MAIN}#call1", 2, "placed"),
        PageCall(f"{MAIN}#call2", None, "external", maybe_interrupted=True),
        PageCall(f"{MAIN}#call3", 2, "see_page", conditional=True),
    ]
    load_calls = [
        PageCall(f"{LOAD}#call1", 1, "recursive"),
        PageCall(f"{LOAD}#call2", None, "unresolved"),
        PageCall(f"{LOAD}#call3", None, "depth_limit"),
    ]
    pages = [
        Page(1, MAIN, 0, [MAIN], calls=main_calls),
        Page(2, LOAD, 1, [MAIN, LOAD], reached_from=f"{MAIN}#call1", calls=load_calls),
    ]
    return Story(MAIN, "follow", pages)


@pytest.fixture
def story_with_appendices(story: Story) -> Story:
    story.not_on_path = ["app:helper", "app:other"]
    story.never_reached = ["app:unused"]
    story.diagnostics = [
        Diagnostic("unresolved-call", "cannot resolve obj.method", "pkg/load.py", 11),
        Diagnostic("parse-error", "bad syntax", "pkg/bad.py"),
        Diagnostic("odd-line", "line only", line=7),
        Diagnostic("depth-limit", "stopped at depth 50"),
    ]
    return story


@pytest.fixture
def empty_story() -> Story:
    return Story(MAIN, "follow")


def test_render_text_header_names_entry(program, story):
    assert MAIN in render_text(story, program).split("\n")[0]


def test_render_text_lists_pages_then_their_calls_in_order(program, story):
    text = render_text(story, program)
    words = [line.split()[0] for line in text.split("\n") if line.strip()]
    assert words[: words.index("Not")] == [
        "Story",
        "1.",
        "load",
        "cache.get",
        "reload",
        "2.",
        "main",
        "obj.method",
        "deep",
    ]


@pytest.mark.parametrize(
    ("page", "name", "location"),
    [("1.", "main", "app.py:1"), ("2.", "load", "pkg/load.py:10")],
)
def test_render_text_page_line_shows_name_then_location(
    program, story, page, name, location
):
    _assert_in_order(_line(render_text(story, program), page), name, location)


def test_render_text_indents_deeper_pages_further(program, story):
    text = render_text(story, program)
    assert _line(text, "2.").index("load") > _line(text, "1.").index("main")


@pytest.mark.parametrize(
    ("page", "name", "callee"),
    [
        ("1.", "main", "load"),
        ("1.", "main", "reload"),
        ("2.", "load", "main"),
        ("2.", "load", "deep"),
    ],
)
def test_render_text_indents_calls_past_their_page_name(
    program, story, page, name, callee
):
    text = render_text(story, program)
    assert _line(text, callee).index(callee) > _line(text, page).index(name)


@pytest.mark.parametrize(
    ("callee", "badge"),
    [
        ("load", "-> page 2"),
        ("reload", "-> see page 2"),
        ("main", "-> recursive, see page 1"),
        ("cache.get", "[external]"),
        ("obj.method", "[unresolved]"),
        ("deep", "[depth limit]"),
    ],
    ids=["placed", "see_page", "recursive", "external", "unresolved", "depth_limit"],
)
def test_render_text_call_line_shows_note_badge(program, story, callee, badge):
    _assert_in_order(_line(render_text(story, program), callee), callee, badge)


@pytest.mark.parametrize(
    ("callee", "conditional", "interrupted"),
    [("load", False, False), ("reload", True, False), ("cache.get", False, True)],
)
def test_render_text_call_line_shows_conditional_and_interrupted_badges(
    program, story, callee, conditional, interrupted
):
    line = _line(render_text(story, program), callee)
    assert ("[conditional]" in line) is conditional
    assert ("[maybe interrupted]" in line) is interrupted


def test_render_text_all_mode_shows_via_arm(program, story):
    story.mode = "all"
    story.pages[1].via_arm = "if ready"
    text = render_text(story, program)
    assert "show everything" in text.split("\n")[0]
    _assert_in_order(_line(text, "2."), "load", "pkg/load.py:10", "if ready")
    assert "via" not in _line(text, "1.")


def test_render_text_lists_appendices_then_diagnostics(program, story_with_appendices):
    lines = [
        line.strip() for line in render_text(story_with_appendices, program).split("\n")
    ]
    start = lines.index("Not on this path:")
    assert lines[start : start + 6] == [
        "Not on this path:",
        "app:helper",
        "app:other",
        "Never reached:",
        "app:unused",
        "Diagnostics:",
    ]
    diagnostics = lines[start + 6 : -1]
    assert len(diagnostics) == 4
    _assert_in_order(
        diagnostics[0], "pkg/load.py:11", "unresolved-call", "cannot resolve obj.method"
    )
    _assert_in_order(diagnostics[1], "pkg/bad.py", "parse-error", "bad syntax")
    _assert_in_order(diagnostics[2], "7", "odd-line", "line only")
    _assert_in_order(diagnostics[3], "depth-limit", "stopped at depth 50")


def test_render_text_marks_empty_sections_none(program, empty_story):
    lines = render_text(empty_story, program).rstrip("\n").split("\n")
    titles = ["Not on this path", "Never reached", "Diagnostics"]
    for line, title in zip(lines[-3:], titles, strict=True):
        _assert_in_order(line, title, "(none)")


def test_render_text_raises_key_error_for_unknown_chunk(program, story):
    story.pages[1].chunk_id = "app:missing"
    with pytest.raises(KeyError):
        render_text(story, program)


def test_render_text_raises_key_error_for_call_site_not_in_page_chunk(program, story):
    story.pages[0].calls.append(PageCall(f"{LOAD}#call1", None, "external"))
    with pytest.raises(KeyError):
        render_text(story, program)


def test_render_text_escapes_code_derived_text(program, story_with_appendices):
    line_count = len(render_text(story_with_appendices, program).split("\n"))
    program.chunks[0].name = "main" + ESC + "[2J"
    program.chunks[1].body[1].callee_text = "obj" + chr(0x202E) + ".method"
    story_with_appendices.pages[1].via_arm = "if ok" + chr(0x2028) + "evil"
    story_with_appendices.never_reached = ["app:x" + chr(0x85)]
    story_with_appendices.diagnostics[0].message = (
        "two" + chr(10) + "lines" + chr(0xD800)
    )

    text = render_text(story_with_appendices, program)

    for escaped in (
        f"main{BACKSLASH}x1b[2J",
        f"obj{BACKSLASH}u202e.method",
        f"if ok{BACKSLASH}u2028evil",
        f"app:x{BACKSLASH}x85",
        f"two{BACKSLASH}nlines{BACKSLASH}ud800",
    ):
        assert escaped in text
    unsafe = [
        c for c in text if c != "\n" and unicodedata.category(c) in UNSAFE_CATEGORIES
    ]
    assert unsafe == []
    assert len(text.split("\n")) == line_count


@pytest.mark.parametrize(
    "story_name", ["story", "story_with_appendices", "empty_story"]
)
def test_render_text_has_no_trailing_whitespace_and_one_final_newline(
    request, program, story_name
):
    # Code-derived text can bring trailing spaces of its own.
    program.chunks[1].body[2].callee_text = "deep   "
    text = render_text(request.getfixturevalue(story_name), program)
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert [line for line in text.split("\n") if line != line.rstrip()] == []


@pytest.mark.parametrize(
    ("char", "escaped"),
    [
        (ESC, f"{BACKSLASH}x1b"),
        (chr(0x9B), f"{BACKSLASH}x9b"),
        (chr(0x202E), f"{BACKSLASH}u202e"),
        (chr(0x2028), f"{BACKSLASH}u2028"),
        (chr(0x2029), f"{BACKSLASH}u2029"),
        (chr(0xD800), f"{BACKSLASH}ud800"),
        (chr(0xE0001), f"{BACKSLASH}U000e0001"),
        (chr(10), f"{BACKSLASH}n"),
    ],
    ids=[
        "esc",
        "c1-csi",
        "rtl-override",
        "line-separator",
        "paragraph-separator",
        "lone-surrogate",
        "astral-format-char",
        "newline",
    ],
)
def test_escape_control_chars_makes_unsafe_char_visible(char, escaped):
    assert escape_control_chars("a" + char + "b") == "a" + escaped + "b"


@pytest.mark.parametrize("text", ["", "plain text", "café", "Ωμέγα", "日本語"])
def test_escape_control_chars_keeps_safe_text(text):
    assert escape_control_chars(text) == text
