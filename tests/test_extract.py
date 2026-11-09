"""Tests for Python extraction in ``minstrel.adapters.python.extract``.

Expected chunk ids and call lists are written by hand from docs/PLAN.md and
docs/decisions.md (ids, evaluation-order numbering, callee-name positions,
conditional rules, chunk order), never copied from the extractor's output.
"""

import codecs
from pathlib import Path
from textwrap import dedent

import pytest

from minstrel.adapters.python.extract import (
    MAX_FILE_BYTES,
    extract,
    module_name,
    parse,
    parse_bytes,
)
from minstrel.ir import (
    CallSite,
    Chunk,
    Program,
    iter_call_sites,
    program_from_json,
    program_to_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "python"

# Per fixture file (``<case>/<file>``): chunk ids in program order, each with
# its call sites' callee_text in evaluation order.
FIXTURE_CALLS = {
    "hello_world/app.py": [
        ("app:helloWorld", ["print"]),
        ("app:main", ["helloWorld"]),
    ],
    "chain/app.py": [
        ("app:main", ["load"]),
        ("app:load", ["parse"]),
        ("app:parse", ["validate"]),
        ("app:validate", []),
    ],
    "repeated_helper/app.py": [
        ("app:main", ["log", "work", "log"]),
        ("app:work", ["log"]),
        ("app:log", ["print"]),
    ],
    "recursion/app.py": [
        ("app:main", ["factorial", "print"]),
        ("app:factorial", ["factorial"]),
    ],
    "mutual_recursion/app.py": [
        ("app:main", ["is_even", "print"]),
        ("app:is_even", ["is_odd"]),
        ("app:is_odd", ["is_even"]),
    ],
    "nested_function/app.py": [
        ("app:main", ["outer"]),
        ("app:outer", ["inner"]),
        ("app:outer.inner", ["print"]),
    ],
    "external_calls/app.py": [
        ("app:main", ["os.path.join", "json.dumps", "print"]),
    ],
    "unresolved_call/app.py": [
        ("app:main", ["setup", "load_plugins"]),
        ("app:setup", ["print"]),
    ],
    "dead_code/app.py": [
        ("app:main", ["used"]),
        ("app:used", ["print"]),
        ("app:unused", ["also_unused"]),
        ("app:also_unused", ["print"]),
    ],
    "multiple_entries/backup.py": [
        ("backup:<module>", ["run"]),
        ("backup:run", ["print"]),
    ],
    "multiple_entries/restore.py": [
        ("restore:<module>", ["run"]),
        ("restore:run", ["print"]),
    ],
}

# Each link of a call chain repeats the whole chain before it in its
# callee_text, so the text built grows with the square of the chain's length.
# 250 links stays well inside Python's recursion limit.
CHAIN_LINKS = 250
SHORT_LINK = ".m()"
# With 250 of these links, callee_text alone passes the 1,000,000-character
# minimum budget.
LONG_LINK = ".m" + "x" * 50 + "()"
# A left-nested ``a + a + ...`` this long is far deeper than the recursion limit.
TOO_DEEP_TERMS = 5_000


def _extract_fixture(relative: str) -> list[Chunk]:
    """Parse and extract one fixture file, given as ``<case>/<file>``."""
    file = relative.split("/", 1)[1]
    return extract(parse(FIXTURES / relative), file)


def _extract(code: str, file: str = "app.py") -> list[Chunk]:
    """Extract the chunks of dedented source ``code``."""
    return extract(parse_bytes(dedent(code).encode()), file)


def _calls(chunk: Chunk) -> list[CallSite]:
    """Return every call site in ``chunk``'s body, in order."""
    return list(iter_call_sites(chunk.body))


def _callees(chunks: list[Chunk]) -> list[tuple[str, list[str]]]:
    """Return each chunk's id with its call sites' callee_text, in order."""
    return [
        (chunk.id, [site.callee_text for site in _calls(chunk)]) for chunk in chunks
    ]


def _top_level_calls(code: str) -> list[CallSite]:
    """Extract ``code``, which must hold only top-level calls, and return them."""
    chunks = _extract(code)
    assert [chunk.id for chunk in chunks] == ["app:<module>"]
    return _calls(chunks[0])


def _write(path: Path, data: bytes) -> Path:
    """Write ``data`` to ``path`` and return the path."""
    path.write_bytes(data)
    return path


# --- Golden fixture inputs ---------------------------------------------------


@pytest.mark.parametrize("relative", FIXTURE_CALLS)
def test_fixture_extracts_expected_chunks_and_calls(relative: str) -> None:
    assert _callees(_extract_fixture(relative)) == FIXTURE_CALLS[relative]


def test_hello_world_extracts_spec_chunks_exactly() -> None:
    expected = [
        Chunk(
            id="app:helloWorld",
            kind="function",
            name="helloWorld",
            file="app.py",
            start_line=1,
            end_line=2,
            source='def helloWorld():\n    print("Hello, World")',
            body=[CallSite("app:helloWorld#call1", 2, 4, "print")],
        ),
        Chunk(
            id="app:main",
            kind="function",
            name="main",
            file="app.py",
            start_line=5,
            end_line=6,
            source="def main():\n    helloWorld()",
            body=[CallSite("app:main#call1", 6, 4, "helloWorld")],
        ),
    ]

    assert _extract_fixture("hello_world/app.py") == expected


@pytest.mark.parametrize("relative", FIXTURE_CALLS)
def test_fixture_extraction_round_trips_through_json(relative: str) -> None:
    file = relative.split("/", 1)[1]
    program = Program(
        language="python", files=[file], chunks=_extract_fixture(relative)
    )
    text = program_to_json(program)

    decoded = program_from_json(text)

    assert decoded == program
    assert program_to_json(decoded) == text


# --- Evaluation order and positions ------------------------------------------


EVALUATION_ORDER = {
    "arguments_before_call": ("f(g())", ["g", "f"]),
    "receiver_before_method": ("h(1).k()", ["h", "h(1).k"]),
    "value_before_target": ("x[f()] = g()", ["g", "f"]),
    "callee_then_arguments": ("f(g())(h())", ["g", "f", "h", "f(g())"]),
    "arguments_left_to_right": ("f(a(), key=b())", ["a", "b", "f"]),
    "augmented_target_before_value": ("x[f()] += g()", ["f", "g"]),
    "chained_assignment_targets_left_to_right": (
        "a[f()] = b[g()] = h()",
        ["h", "f", "g"],
    ),
    "f_string_interpolation": ('print(f"{name()}")', ["name", "print"]),
}


@pytest.mark.parametrize(
    ("code", "expected"), EVALUATION_ORDER.values(), ids=EVALUATION_ORDER.keys()
)
def test_calls_are_listed_in_evaluation_order(code: str, expected: list[str]) -> None:
    calls = _top_level_calls(code)

    assert [site.callee_text for site in calls] == expected


def test_call_ids_count_from_one_in_evaluation_order() -> None:
    calls = _top_level_calls("f(g())\nh(1).k()\n")

    assert [(site.id, site.callee_text) for site in calls] == [
        ("app:<module>#call1", "g"),
        ("app:<module>#call2", "f"),
        ("app:<module>#call3", "h"),
        ("app:<module>#call4", "h(1).k"),
    ]


def test_call_numbering_restarts_per_chunk_and_skips_nested_defs() -> None:
    chunks = _extract(
        """\
        def outer():
            first()

            def inner():
                nested()

            second()
        """
    )

    assert [[(site.id, site.callee_text) for site in _calls(c)] for c in chunks] == [
        [("app:outer#call1", "first"), ("app:outer#call2", "second")],
        [("app:outer.inner#call1", "nested")],
    ]


def test_call_position_is_the_callee_name_token() -> None:
    chunks = _extract(
        """\
        def run(self):
            self.m()
            h(1).k()
            x[0]()
            value = (
                obj
                .method()
            )
            f(
                g()
            )
        """
    )

    # self.m -> m; h(1).k() -> h, then k; x[0]() has no name, so x[0]'s start;
    # a chain split over lines -> method on its own line; f(g()) -> g, then f.
    assert [(site.line, site.col) for site in _calls(chunks[0])] == [
        (2, 9),
        (3, 4),
        (3, 9),
        (4, 4),
        (7, 9),
        (10, 8),
        (9, 4),
    ]


# Columns count characters (code points), not UTF-8 bytes: é takes 2 bytes,
# 日 and 本 take 3, 😀 takes 4.
CHARACTER_COLUMNS = {
    "two_byte_char_before_call": ('print("café", g())', [("g", 14), ("print", 0)]),
    "three_and_four_byte_chars": (
        'x = "日本" + h() + "😀" + k()',
        [("h", 11), ("k", 23)],
    ),
    "callee_name_starts_with_multibyte_char": ('"é" + été()', [("été", 6)]),
}


@pytest.mark.parametrize(
    ("code", "expected"), CHARACTER_COLUMNS.values(), ids=CHARACTER_COLUMNS.keys()
)
def test_call_columns_count_characters_not_bytes(
    code: str, expected: list[tuple[str, int]]
) -> None:
    calls = _top_level_calls(code)

    assert [(site.callee_text, site.col) for site in calls] == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ('print("café", g())\n', [("g", 1, 14), ("print", 1, 0)]),
        ("if x:\n    g()\n", [("g", 2, 4)]),
    ],
    ids=["call_on_first_row", "call_on_later_row"],
)
def test_call_columns_in_a_bom_file_count_from_the_visible_text(
    code: str, expected: list[tuple[str, int, int]]
) -> None:
    [top] = extract(parse_bytes(codecs.BOM_UTF8 + code.encode()), "app.py")

    assert [(s.callee_text, s.line, s.col) for s in _calls(top)] == expected


def test_crlf_file_gives_line_numbers_and_character_columns() -> None:
    source = 'def main():\r\n    first()\r\n\r\n    print("café", g())\r\n'

    [chunk] = extract(parse_bytes(source.encode()), "app.py")

    assert (chunk.start_line, chunk.end_line) == (1, 4)
    assert [(s.callee_text, s.line, s.col) for s in _calls(chunk)] == [
        ("first", 2, 4),
        ("g", 4, 18),
        ("print", 4, 4),
    ]


# --- Conditional flags --------------------------------------------------------


CONDITIONAL = {
    "plain_calls": ("f(g())", [("g", False), ("f", False)]),
    "and_right_side": ("a() and b()", [("a", False), ("b", True)]),
    "or_right_side": ("a() or b()", [("a", False), ("b", True)]),
    "boolean_chain": (
        "a() and b() or c()",
        [("a", False), ("b", True), ("c", True)],
    ),
    "call_nested_in_right_side": (
        "a() or f(g())",
        [("a", False), ("g", True), ("f", True)],
    ),
    "ternary_values_not_condition": (
        "x() if c() else y()",
        [("c", False), ("x", True), ("y", True)],
    ),
    "ternary_inside_right_side": (
        "a() and (x() if c() else y())",
        [("a", False), ("c", True), ("x", True), ("y", True)],
    ),
    "list_comprehension": (
        "[f(v) for v in items() if g(v)]",
        [("items", False), ("g", True), ("f", True)],
    ),
    "comprehension_second_iterable": (
        "[f(a, b) for a in xs() for b in ys(a)]",
        [("xs", False), ("ys", True), ("f", True)],
    ),
    "set_comprehension": (
        "{f(v) for v in items()}",
        [("items", False), ("f", True)],
    ),
    "dict_comprehension": (
        "{k(v): w(v) for v in items()}",
        [("items", False), ("k", True), ("w", True)],
    ),
    "generator_expression": (
        "total = sum(f(v) for v in items())",
        [("items", False), ("f", True), ("sum", False)],
    ),
    "short_circuit_in_first_iterable": (
        "[v for v in (a() or b())]",
        [("a", False), ("b", True)],
    ),
    "lambda_body": (
        "callback = lambda: f(g())",
        [("g", True), ("f", True)],
    ),
    "plain_comparison": ("a() < b()", [("a", False), ("b", False)]),
    "chained_comparison": (
        "a() < b() < c()",
        [("a", False), ("b", False), ("c", True)],
    ),
    "chained_comparison_word_operators": (
        "a() in b() is not c() not in d()",
        [("a", False), ("b", False), ("c", True), ("d", True)],
    ),
    "chained_comparison_comment_is_not_an_operand": (
        "x = (a() <  # note\n     b() < c())",
        [("a", False), ("b", False), ("c", True)],
    ),
    "chained_comparison_inside_right_side": (
        "x() or a() < b() < c()",
        [("x", False), ("a", True), ("b", True), ("c", True)],
    ),
}


@pytest.mark.parametrize(
    ("code", "expected"), CONDITIONAL.values(), ids=CONDITIONAL.keys()
)
def test_conditional_flags_follow_short_circuits(
    code: str, expected: list[tuple[str, bool]]
) -> None:
    calls = _top_level_calls(code)

    assert [(site.callee_text, site.conditional) for site in calls] == expected


# --- Chunks: kinds, qualnames, ids, order --------------------------------------


def test_nested_defs_methods_and_classes_get_dotted_qualnames() -> None:
    chunks = _extract(
        """\
        class Outer:
            class Inner:
                def method(self):
                    pass

            def method(self):
                def local():
                    pass


        def func():
            class Local:
                def meth(self):
                    pass

            def helper():
                pass
        """
    )

    assert [(c.id, c.kind, c.name) for c in chunks] == [
        ("app:Outer.Inner.method", "method", "Outer.Inner.method"),
        ("app:Outer.method", "method", "Outer.method"),
        ("app:Outer.method.local", "function", "Outer.method.local"),
        ("app:func", "function", "func"),
        ("app:func.Local.meth", "method", "func.Local.meth"),
        ("app:func.helper", "function", "func.helper"),
    ]


def test_repeated_qualnames_get_numbered_suffixes() -> None:
    chunks = _extract(
        """\
        def f():
            a()


        def f():
            b()


        class C:
            @property
            def x(self):
                return get()

            @x.setter
            def x(self, value):
                put(value)


        def f():
            c()
        """
    )

    assert [(c.id, c.name, [s.id for s in _calls(c)]) for c in chunks] == [
        ("app:f", "f", ["app:f#call1"]),
        ("app:f~2", "f", ["app:f~2#call1"]),
        ("app:C.x", "C.x", ["app:C.x#call1"]),
        ("app:C.x~2", "C.x", ["app:C.x~2#call1"]),
        ("app:f~3", "f", ["app:f~3#call1"]),
    ]


def test_chunks_are_module_toplevel_then_defs_in_pre_order() -> None:
    chunks = _extract(
        """\
        def a():
            def b():
                def c():
                    pass

            def d():
                pass


        async def e():
            await get()


        a()
        """
    )

    assert _callees(chunks) == [
        ("app:<module>", ["a"]),
        ("app:a", []),
        ("app:a.b", []),
        ("app:a.b.c", []),
        ("app:a.d", []),
        ("app:e", ["get"]),
    ]


def test_decorator_default_and_annotation_calls_are_not_call_sites() -> None:
    chunks = _extract(
        """\
        @decorate(config())
        def handler(x=default(), *, y: hint() = other()) -> returns():
            body()


        @register()
        class Plugin:
            def run(self, z=fallback()):
                work()
        """
    )

    assert _callees(chunks) == [
        ("app:handler", ["body"]),
        ("app:Plugin.run", ["work"]),
    ]
    # A decorated def starts at its first decorator, so the source shows it.
    assert chunks[0].start_line == 1
    assert chunks[0].source.startswith("@decorate(config())\ndef handler(")


def test_lambda_default_argument_calls_are_not_call_sites() -> None:
    assert _extract("callback = lambda x=default(): x\n") == []


def test_class_body_calls_belong_to_the_enclosing_chunk() -> None:
    chunks = _extract(
        """\
        class Config(base()):
            value = compute()

            def method(self):
                inner()


        def build():
            first()

            class Local(mixin()):
                setting = local_compute()

            last()
        """
    )

    assert _callees(chunks) == [
        ("app:<module>", ["base", "compute"]),
        ("app:Config.method", ["inner"]),
        ("app:build", ["first", "mixin", "local_compute", "last"]),
    ]
    assert [site.id for site in _calls(chunks[2])] == [
        f"app:build#call{number}" for number in range(1, 5)
    ]


def test_module_toplevel_spans_the_whole_file_when_it_has_calls() -> None:
    code = dedent(
        """\
        def main():
            pass


        if __name__ == "__main__":
            main()
        """
    )

    top = extract(parse_bytes(code.encode()), "app.py")[0]

    assert (top.id, top.kind, top.name, top.file) == (
        "app:<module>",
        "module_toplevel",
        "<module>",
        "app.py",
    )
    assert (top.start_line, top.end_line) == (1, 6)
    assert top.source == code.removesuffix("\n")
    assert [site.callee_text for site in _calls(top)] == ["main"]


@pytest.mark.parametrize(
    ("code", "expected_ids"),
    [
        ("", []),
        ("import os\n\nLIMIT = 1\n", []),
        ("class C:\n    x = 1\n\n\ndef f():\n    g()\n", ["app:f"]),
    ],
    ids=["empty_file", "imports_and_assignments", "class_and_def"],
)
def test_module_toplevel_is_absent_without_top_level_calls(
    code: str, expected_ids: list[str]
) -> None:
    assert [chunk.id for chunk in _extract(code)] == expected_ids


def test_chunk_source_normalizes_crlf_without_trailing_newline() -> None:
    chunks = extract(parse_bytes(b"def f():\r\n    g()\r\n\r\n\r\nf()\r\n"), "app.py")

    assert [(c.id, c.start_line, c.end_line, c.source) for c in chunks] == [
        ("app:<module>", 1, 5, "def f():\n    g()\n\n\nf()"),
        ("app:f", 1, 2, "def f():\n    g()"),
    ]


@pytest.mark.parametrize(
    ("source", "start"),
    [
        (b"\n\n# comment\ndef f():\n    g()\n", 4),
        (b"\xef\xbb\xbfdef f():\n    g()\n", 1),
    ],
    ids=["leading_blank_lines", "utf8_bom"],
)
def test_chunk_lines_and_source_survive_leading_blank_lines_and_bom(
    source: bytes, start: int
) -> None:
    [chunk] = extract(parse_bytes(source), "app.py")

    assert (chunk.start_line, chunk.end_line) == (start, start + 1)
    assert chunk.source == "def f():\n    g()"
    assert [site.line for site in _calls(chunk)] == [start + 1]


# --- module_name -------------------------------------------------------------


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        ("app.py", "app"),
        ("pkg/mod.py", "pkg.mod"),
        ("pkg/__init__.py", "pkg"),
        ("pkg/sub/__init__.py", "pkg.sub"),
        ("a/b/c.py", "a.b.c"),
    ],
)
def test_module_name_is_dotted_path(file: str, expected: str) -> None:
    assert module_name(file) == expected


def test_extract_ids_use_module_name_and_keep_file_path() -> None:
    [chunk] = _extract("def f():\n    g()\n", file="pkg/__init__.py")

    assert (chunk.id, chunk.file) == ("pkg:f", "pkg/__init__.py")
    assert [site.id for site in _calls(chunk)] == ["pkg:f#call1"]


# --- parse / parse_bytes -------------------------------------------------------


def test_parse_reads_a_python_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "app.py", b"def f():\n    g()\n")

    assert _callees(extract(parse(path), "app.py")) == [("app:f", ["g"])]


def test_parse_accepts_a_file_of_exactly_max_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path / "big.py", b"#" * MAX_FILE_BYTES)

    assert parse(path).root_node.type == "module"


def test_parse_rejects_a_file_over_max_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path / "big.py", b"#" * (MAX_FILE_BYTES + 1))

    with pytest.raises(ValueError):
        parse(path)


def test_parse_rejects_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        parse(tmp_path)


def test_parse_raises_os_error_for_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        parse(tmp_path / "missing.py")


def test_parse_bytes_reports_syntax_errors_in_the_tree() -> None:
    assert parse_bytes(b"def (:\n").root_node.has_error


def test_lone_cr_ends_a_line_so_the_next_call_is_not_hidden_in_a_comment() -> None:
    [chunk] = extract(parse_bytes(b"def main():\n    # note\r    run()\n"), "app.py")

    assert (chunk.id, chunk.end_line) == ("app:main", 3)
    assert chunk.source == "def main():\n    # note\n    run()"
    assert [(s.callee_text, s.line, s.col) for s in _calls(chunk)] == [("run", 3, 4)]


@pytest.mark.parametrize(
    "source",
    [
        b"a()\nb()\nc()\n",
        b"a()\r\nb()\r\nc()\r\n",
        b"a()\rb()\rc()\r",
        b"a()  # note\rb()\r\nc()\n",
    ],
    ids=["lf", "crlf", "lone_cr", "mixed_with_comment"],
)
def test_every_line_ending_python_accepts_gives_python_line_numbers(
    source: bytes,
) -> None:
    [top] = extract(parse_bytes(source), "app.py")

    assert [(s.callee_text, s.line, s.col) for s in _calls(top)] == [
        ("a", 1, 0),
        ("b", 2, 0),
        ("c", 3, 0),
    ]


@pytest.mark.parametrize(
    "source",
    [
        b"# coding: latin-1\nf()\n",
        b"#!/usr/bin/env python\n# -*- coding: latin-1 -*-\nf()\n",
        b"#!python\r# coding: latin-1\nf()\n",
        b"# coding: no-such-codec\nf()\n",
        codecs.BOM_UTF8 + b"# coding: latin-1\nf()\n",
        b"# caf\xe9\nf()\n",
    ],
    ids=[
        "latin1",
        "latin1_on_second_line",
        "latin1_on_second_line_after_lone_cr",
        "unknown_encoding",
        "bom_with_latin1",
        "first_line_not_utf8",
    ],
)
def test_parse_bytes_rejects_source_python_would_not_read_as_utf8(
    source: bytes,
) -> None:
    with pytest.raises(ValueError):
        parse_bytes(source)


@pytest.mark.parametrize(
    "source",
    [
        b"# coding: utf8\nf()\n",
        b"# coding: u8\nf()\n",
        b"# -*- coding: UTF-8 -*-\nf()\n",
        codecs.BOM_UTF8 + b"f()\n",
        codecs.BOM_UTF8 + b"# coding: utf-8\nf()\n",
        # Python reads only lines 1 and 2 for the declaration.
        b"#!python\r#\r# coding: latin-1\nf()\n",
    ],
    ids=[
        "utf8_alias",
        "u8_alias",
        "utf8_upper_case_emacs_style",
        "bom",
        "bom_with_utf8_declaration",
        "declaration_on_third_line_after_lone_crs",
    ],
)
def test_parse_bytes_accepts_utf8_aliases_and_bom(source: bytes) -> None:
    [top] = extract(parse_bytes(source), "app.py")

    assert [site.callee_text for site in _calls(top)] == ["f"]


def test_parse_rejects_a_file_declaring_another_encoding(tmp_path: Path) -> None:
    path = _write(tmp_path / "app.py", b"# coding: latin-1\nf()\n")

    with pytest.raises(ValueError):
        parse(path)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("def broken(:\n    g()\n", [("app:broken", ["g"])]),
        ("x = (f() +\ny = 2\n", [("app:<module>", ["f"])]),
    ],
    ids=["missing_paren_in_def", "call_inside_error_node"],
)
def test_extract_tolerates_syntax_errors(
    code: str, expected: list[tuple[str, list[str]]]
) -> None:
    assert _callees(_extract(code)) == expected


# --- Limits and errors ---------------------------------------------------------


def test_extract_handles_a_long_chain_of_short_calls() -> None:
    calls = _top_level_calls("start()" + SHORT_LINK * CHAIN_LINKS)

    assert len(calls) == CHAIN_LINKS + 1


def test_extract_rejects_a_long_callee_chain_over_the_output_budget() -> None:
    # Same depth as the short chain above, so only the budget can fail it.
    with pytest.raises(ValueError):
        _extract("start()" + LONG_LINK * CHAIN_LINKS)


def test_extract_rejects_code_nested_too_deeply() -> None:
    with pytest.raises(ValueError):
        _extract("x = " + " + ".join(["a"] * TOO_DEEP_TERMS))


def test_extract_rejects_source_that_is_not_utf8() -> None:
    with pytest.raises(UnicodeDecodeError):
        extract(parse_bytes(b"f()\nname = '\xff'\n"), "app.py")
