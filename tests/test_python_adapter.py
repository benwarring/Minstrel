"""Tests for ``PythonAdapter`` in ``minstrel.adapters.python``.

Expected values are written by hand from docs/decisions.md ("Python adapter
(M2)", "Python adapter entry tiers", "Python adapter resolve", "Default
entries", "``Program.entry_candidates``" and the "M2 contracts" resolver
lines), docs/PLAN.md ("Entry detection"), the ``LanguageAdapter`` protocol
and the ``PythonAdapter`` entry in .pipeline/helpers.md, never copied from
the adapter's output.

Every case is one module ``app`` (file ``app.py``) unless it says otherwise.
"""

from collections.abc import Callable
from pathlib import Path
from textwrap import dedent

import pytest
from tree_sitter import Tree

import minstrel.adapters.python.resolve as resolve_module
from minstrel.adapters.base import LanguageAdapter
from minstrel.adapters.python import PythonAdapter
from minstrel.adapters.python.extract import extract, parse_bytes
from minstrel.ir import SCHEMA_VERSION, Diagnostic, Program, iter_call_sites

FIXTURES = Path(__file__).parent / "fixtures" / "python"
FILE = "app.py"
# Built with chr() rather than typed as an escape sequence.
NEWLINE = chr(10)
FULLWIDTH_LOW_LINE = chr(0xFF3F)
INVALID_START_BYTE = bytes([0xFF])

# A call's (status, targets).
Outcome = tuple[str, list[str]]

UNRESOLVED: Outcome = ("unresolved", [])
EXTERNAL: Outcome = ("external", [])

PROTOCOL_MEMBERS = (
    "language",
    "extensions",
    "parse",
    "extract",
    "resolve",
    "entry_candidates",
)

# A module-level ``main`` and a top-level ``if {condition}:`` that calls it.
# When the condition is a main guard, the top level is tier 1 and ``main``
# tier 2; otherwise ``main`` is tier 2 and the top level falls to tier 3.
MAIN_UNDER_CONDITION = """\
def main():
    pass


if {condition}:
    main()
"""
GUARD_COUNTED = [["app:<module>"], ["app:main"]]
GUARD_NOT_COUNTED = [["app:main"], ["app:<module>"]]

# Python reads identifiers after NFKC, which maps U+FF3F FULLWIDTH LOW LINE
# to "_" and fullwidth letters to ASCII. U+FF3F is not XID_Start, so Python
# rejects it as an identifier's first character: the first underscore of a
# spelling Python accepts has to stay ASCII.
FULLWIDTH_UNDERSCORE_NAME = (
    "_" + FULLWIDTH_LOW_LINE + "name" + FULLWIDTH_LOW_LINE + FULLWIDTH_LOW_LINE
)
FULLWIDTH_LETTER_NAME = "__" + "".join(chr(ord(c) + 0xFEE0) for c in "name") + "__"
# Every underscore fullwidth: Python raises SyntaxError ("invalid character
# U+FF3F"), so the file has a syntax error and its guard can't count.
ALL_FULLWIDTH_UNDERSCORE_NAME = FULLWIDTH_LOW_LINE * 2 + "name" + FULLWIDTH_LOW_LINE * 2

# A guard that only runs a builtin, so its top level has a call.
GUARDED_PRINT = """\
if __name__ == "__main__":
    print("go")
"""

# Top-level calls with no guard.
TOPLEVEL_PRINT = """\
print("go")
"""

PLAIN_MAIN = """\
def main():
    pass
"""

GUARDED_MAIN = MAIN_UNDER_CONDITION.format(condition='__name__ == "__main__"')

# One local def called from ``main``, and a call to a def that only ``b.py``
# has.
CROSS_FILE_A = """\
def local():
    pass


def main():
    local()
    helper()
"""
CROSS_FILE_B = """\
def helper():
    print("b")
"""

# A class name this long, with this many private names in its body, builds
# mangled ``_<name>__a`` names far past resolve's budget of
# max(50 chars per byte, 1,000,000) (as in tests/test_resolve.py).
LONG_CLASS_NAME = 20_000
PRIVATE_NAMES = 200

# Defs and a call that would resolve, to follow the over-budget class.
HELPER_AND_MAIN = """\
def helper():
    pass


def main():
    helper()
"""


@pytest.fixture
def adapter() -> PythonAdapter:
    return PythonAdapter()


def _program(adapter: PythonAdapter, sources: dict[str, str]) -> Program:
    """Parse and extract every ``file: source`` with ``adapter``, in order."""
    chunks = [
        chunk
        for file, source in sources.items()
        for chunk in adapter.extract(parse_bytes(source.encode()), file)
    ]
    return Program(
        schema_version=SCHEMA_VERSION,
        language=adapter.language,
        files=list(sources),
        chunks=chunks,
    )


def _code_program(adapter: PythonAdapter, code: str) -> Program:
    """Return the Program for dedented ``code`` as ``app.py``."""
    return _program(adapter, {FILE: dedent(code)})


def _condition_program(adapter: PythonAdapter, condition: str) -> Program:
    """Return the Program for ``MAIN_UNDER_CONDITION`` with ``condition``."""
    return _program(adapter, {FILE: MAIN_UNDER_CONDITION.format(condition=condition)})


def _outcomes(program: Program) -> dict[str, Outcome]:
    """Return every call site's id with its (status, targets)."""
    return {
        site.id: (site.status, site.targets)
        for chunk in program.chunks
        for site in iter_call_sites(chunk.body)
    }


def _fixture_program(adapter: PythonAdapter, case: str) -> Program:
    """Parse and extract every file of fixture ``case`` with ``adapter``."""
    paths = sorted((FIXTURES / case).glob("*.py"))
    chunks = [
        chunk
        for path in paths
        for chunk in adapter.extract(adapter.parse(path), path.name)
    ]
    return Program(
        schema_version=SCHEMA_VERSION,
        language=adapter.language,
        files=[path.name for path in paths],
        chunks=chunks,
    )


def _over_budget_source() -> str:
    """Return a file whose mangled names blow resolve's budget, then defs."""
    return (
        f"class {'K' * LONG_CLASS_NAME}:"
        + NEWLINE
        + ("    __a = 0" + NEWLINE) * PRIVATE_NAMES
        + NEWLINE
        + NEWLINE
        + HELPER_AND_MAIN
    )


# --- Protocol members ------------------------------------------------------------


def test_language_is_python(adapter: PythonAdapter) -> None:
    assert PythonAdapter.language == "python"
    assert adapter.language == "python"


def test_extensions_are_py_only(adapter: PythonAdapter) -> None:
    assert PythonAdapter.extensions == (".py",)
    assert adapter.extensions == (".py",)


@pytest.mark.parametrize("member", PROTOCOL_MEMBERS)
def test_adapter_has_every_protocol_member(adapter: PythonAdapter, member: str) -> None:
    as_protocol: LanguageAdapter = adapter

    assert hasattr(as_protocol, member)


@pytest.mark.parametrize("method", PROTOCOL_MEMBERS[2:])
def test_protocol_methods_are_callable(adapter: PythonAdapter, method: str) -> None:
    assert callable(getattr(adapter, method))


# --- parse and extract -----------------------------------------------------------


def test_parse_and_extract_a_real_file(adapter: PythonAdapter, tmp_path: Path) -> None:
    path = tmp_path / "pkg" / "mod.py"
    path.parent.mkdir()
    path.write_text(GUARDED_MAIN, encoding="utf-8")

    tree = adapter.parse(path)
    chunks = adapter.extract(tree, "pkg/mod.py")

    assert isinstance(tree, Tree)
    assert [chunk.id for chunk in chunks] == ["pkg.mod:<module>", "pkg.mod:main"]
    assert [chunk.kind for chunk in chunks] == ["module_toplevel", "function"]
    assert {chunk.file for chunk in chunks} == {"pkg/mod.py"}
    assert [site.callee_text for site in iter_call_sites(chunks[0].body)] == ["main"]


def test_real_file_resolves_and_ranks_after_extract(
    adapter: PythonAdapter, tmp_path: Path
) -> None:
    path = tmp_path / "pkg" / "mod.py"
    path.parent.mkdir()
    path.write_text(GUARDED_MAIN, encoding="utf-8")
    chunks = adapter.extract(adapter.parse(path), "pkg/mod.py")
    program = Program(language="python", files=["pkg/mod.py"], chunks=chunks)

    adapter.resolve(program)

    assert _outcomes(program) == {
        "pkg.mod:<module>#call1": ("resolved", ["pkg.mod:main"]),
    }
    assert adapter.entry_candidates(program) == [
        ["pkg.mod:<module>"],
        ["pkg.mod:main"],
    ]


def test_extract_accepts_a_tree_from_parse_bytes(adapter: PythonAdapter) -> None:
    tree = parse_bytes(dedent(CROSS_FILE_A).encode())

    chunks = adapter.extract(tree, FILE)

    assert [chunk.id for chunk in chunks] == ["app:local", "app:main"]
    assert [site.callee_text for site in iter_call_sites(chunks[1].body)] == [
        "local",
        "helper",
    ]
    statuses = {site.status for chunk in chunks for site in iter_call_sites(chunk.body)}
    assert statuses == {"unresolved"}


def test_parse_raises_os_error_for_a_missing_file(
    adapter: PythonAdapter, tmp_path: Path
) -> None:
    with pytest.raises(OSError):
        adapter.parse(tmp_path / "missing.py")


def test_parse_raises_value_error_for_a_directory(
    adapter: PythonAdapter, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        adapter.parse(tmp_path)


def test_extract_raises_type_error_for_a_non_tree(adapter: PythonAdapter) -> None:
    with pytest.raises(TypeError):
        adapter.extract(GUARDED_MAIN, FILE)


# --- resolve ---------------------------------------------------------------------


def test_resolve_fills_statuses_across_files(adapter: PythonAdapter) -> None:
    program = _program(
        adapter, {"a.py": CROSS_FILE_A, "b.py": CROSS_FILE_B}
    )

    adapter.resolve(program)

    # M2 resolves each file on its own, so a.py's helper() can't reach b.py.
    assert _outcomes(program) == {
        "a:main#call1": ("resolved", ["a:local"]),
        "a:main#call2": UNRESOLVED,
        "b:helper#call1": EXTERNAL,
    }
    assert program.diagnostics == []


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (
            "hello_world",
            {
                "app:helloWorld#call1": EXTERNAL,
                "app:main#call1": ("resolved", ["app:helloWorld"]),
            },
        ),
        (
            "multiple_entries",
            {
                "backup:<module>#call1": ("resolved", ["backup:run"]),
                "backup:run#call1": EXTERNAL,
                "restore:<module>#call1": ("resolved", ["restore:run"]),
                "restore:run#call1": EXTERNAL,
            },
        ),
    ],
)
def test_resolve_fixture_calls(
    adapter: PythonAdapter, case: str, expected: dict[str, Outcome]
) -> None:
    program = _fixture_program(adapter, case)

    adapter.resolve(program)

    assert _outcomes(program) == expected


def test_resolve_passes_each_file_alone_with_the_same_chunks(
    adapter: PythonAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = _program(adapter, {"a.py": CROSS_FILE_A, "b.py": CROSS_FILE_B})
    seen: list[tuple[list[str], list[str], list[int]]] = []

    def record(single: Program, trees: dict[str, Tree]) -> None:
        seen.append(
            (single.files, list(trees), [id(chunk) for chunk in single.chunks])
        )

    monkeypatch.setattr(resolve_module, "resolve", record)

    adapter.resolve(program)

    chunk_ids = [id(chunk) for chunk in program.chunks]
    assert seen == [
        (["a.py"], ["a.py"], chunk_ids[:2]),
        (["b.py"], ["b.py"], chunk_ids[2:]),
    ]


def test_over_budget_file_is_skipped_and_others_still_resolve(
    adapter: PythonAdapter,
) -> None:
    program = _program(
        adapter, {"bad.py": _over_budget_source(), "good.py": HELPER_AND_MAIN}
    )

    adapter.resolve(program)

    assert _outcomes(program) == {
        "bad:main#call1": UNRESOLVED,
        "good:main#call1": ("resolved", ["good:helper"]),
    }
    assert [(d.code, d.file) for d in program.diagnostics] == [
        ("resolve_skipped", "bad.py")
    ]
    assert program.diagnostics[0].message


def _fail_after_writing(
    bad_file: str, real: Callable[[Program, dict[str, Tree]], None]
) -> Callable[[Program, dict[str, Tree]], None]:
    """Return a resolve that links ``bad_file``'s calls, then raises ValueError.

    Other files go to ``real``, so the test can tell they still resolve.
    """

    def fake(single: Program, trees: dict[str, Tree]) -> None:
        if single.files != [bad_file]:
            real(single, trees)
            return
        for chunk in single.chunks:
            for site in iter_call_sites(chunk.body):
                site.status = "resolved"
                site.targets = ["bad:helper"]
        raise ValueError("over the name budget")

    return fake


def test_value_error_mid_pass_resets_that_files_calls(
    adapter: PythonAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = _program(
        adapter,
        {"good.py": HELPER_AND_MAIN, "bad.py": HELPER_AND_MAIN, "c.py": PLAIN_MAIN},
    )
    earlier = Diagnostic(code="earlier", message="kept")
    program.diagnostics.append(earlier)
    monkeypatch.setattr(
        resolve_module, "resolve", _fail_after_writing("bad.py", resolve_module.resolve)
    )

    adapter.resolve(program)

    assert _outcomes(program) == {
        "good:main#call1": ("resolved", ["good:helper"]),
        "bad:main#call1": UNRESOLVED,
    }
    assert program.diagnostics[0] is earlier
    assert [(d.code, d.file) for d in program.diagnostics[1:]] == [
        ("resolve_skipped", "bad.py")
    ]


def test_resolve_lets_unicode_decode_error_propagate(
    adapter: PythonAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = _code_program(adapter, HELPER_AND_MAIN)

    def undecodable(single: Program, trees: dict[str, Tree]) -> None:
        raise UnicodeDecodeError("utf-8", INVALID_START_BYTE, 0, 1, "bad start byte")

    monkeypatch.setattr(resolve_module, "resolve", undecodable)

    with pytest.raises(UnicodeDecodeError):
        adapter.resolve(program)
    assert program.diagnostics == []


def test_resolve_raises_key_error_for_a_file_never_extracted(
    adapter: PythonAdapter,
) -> None:
    chunks = extract(parse_bytes(HELPER_AND_MAIN.encode()), FILE)
    program = Program(language="python", files=[FILE], chunks=chunks)

    with pytest.raises(KeyError):
        adapter.resolve(program)


def test_resolve_raises_key_error_for_a_file_another_adapter_extracted(
    adapter: PythonAdapter,
) -> None:
    program = _code_program(PythonAdapter(), HELPER_AND_MAIN)

    with pytest.raises(KeyError):
        adapter.resolve(program)


# --- entry_candidates --------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("multiple_entries", [["backup:<module>", "restore:<module>"]]),
        ("hello_world", [["app:main"]]),
    ],
)
def test_fixture_entry_candidates(
    adapter: PythonAdapter, case: str, expected: list[list[str]]
) -> None:
    program = _fixture_program(adapter, case)
    adapter.resolve(program)

    assert adapter.entry_candidates(program) == expected


@pytest.mark.parametrize(
    "condition",
    [
        '__name__ == "__main__"',
        "__name__ == '__main__'",
        '"__main__" == __name__',
        "'__main__' == __name__",
        f'{FULLWIDTH_UNDERSCORE_NAME} == "__main__"',
        f'{FULLWIDTH_LETTER_NAME} == "__main__"',
        f'"__main__" == {FULLWIDTH_UNDERSCORE_NAME}',
    ],
    ids=[
        "double_quotes",
        "single_quotes",
        "reversed",
        "reversed_single_quotes",
        "fullwidth_underscores",
        "fullwidth_letters",
        "reversed_fullwidth_underscores",
    ],
)
def test_main_guard_puts_top_level_in_tier_one(
    adapter: PythonAdapter, condition: str
) -> None:
    program = _condition_program(adapter, condition)

    assert adapter.entry_candidates(program) == GUARD_COUNTED


@pytest.mark.parametrize(
    "condition",
    [
        '(__name__ == "__main__")',
        '__name__ == u"__main__"',
        '__name__ == r"__main__"',
        '__name__ != "__main__"',
        '__name__ == "__main__" == __name__',
        '__name__ == "__main__" and True',
        '__name__ == "main"',
        'name == "__main__"',
        f'{ALL_FULLWIDTH_UNDERSCORE_NAME} == "__main__"',
    ],
    ids=[
        "parenthesized",
        "u_prefix",
        "r_prefix",
        "not_equal",
        "chained",
        "boolean_and",
        "wrong_string",
        "wrong_name",
        "all_fullwidth_underscores_syntax_error",
    ],
)
def test_non_guard_condition_leaves_top_level_in_tier_three(
    adapter: PythonAdapter, condition: str
) -> None:
    program = _condition_program(adapter, condition)

    assert adapter.entry_candidates(program) == GUARD_NOT_COUNTED


def test_guard_nested_in_another_block_is_not_tier_one(adapter: PythonAdapter) -> None:
    program = _code_program(
        adapter,
        """\
        def main():
            pass


        if True:
            if __name__ == "__main__":
                main()
        """,
    )

    assert adapter.entry_candidates(program) == GUARD_NOT_COUNTED


def test_guard_in_a_file_with_a_syntax_error_is_not_tier_one(
    adapter: PythonAdapter,
) -> None:
    program = _program(adapter, {FILE: GUARDED_MAIN + NEWLINE + "x = )" + NEWLINE})

    assert adapter.entry_candidates(program) == GUARD_NOT_COUNTED


def test_main_and_guard_in_one_file_give_two_tiers(adapter: PythonAdapter) -> None:
    program = _program(adapter, {FILE: GUARDED_MAIN})

    assert adapter.entry_candidates(program) == [["app:<module>"], ["app:main"]]


def test_top_level_calls_alone_are_tier_three(adapter: PythonAdapter) -> None:
    program = _program(adapter, {"app.py": PLAIN_MAIN, "script.py": TOPLEVEL_PRINT})

    assert adapter.entry_candidates(program) == [["app:main"], ["script:<module>"]]


def test_top_level_calls_alone_are_the_only_tier(adapter: PythonAdapter) -> None:
    program = _program(adapter, {"script.py": TOPLEVEL_PRINT})

    assert adapter.entry_candidates(program) == [["script:<module>"]]


def test_empty_middle_tier_is_dropped(adapter: PythonAdapter) -> None:
    program = _program(adapter, {"g.py": GUARDED_PRINT, "t.py": TOPLEVEL_PRINT})

    assert adapter.entry_candidates(program) == [["g:<module>"], ["t:<module>"]]


def test_all_three_tiers_in_order(adapter: PythonAdapter) -> None:
    program = _program(
        adapter,
        {"t.py": TOPLEVEL_PRINT, "m.py": PLAIN_MAIN, "g.py": GUARDED_PRINT},
    )

    assert adapter.entry_candidates(program) == [
        ["g:<module>"],
        ["m:main"],
        ["t:<module>"],
    ]


def test_order_within_a_tier_follows_program_chunks(adapter: PythonAdapter) -> None:
    program = _program(adapter, {"a.py": GUARDED_MAIN, "b.py": GUARDED_MAIN})
    a_chunks, b_chunks = program.chunks[:2], program.chunks[2:]
    program.chunks = b_chunks + a_chunks

    assert adapter.entry_candidates(program) == [
        ["b:<module>", "a:<module>"],
        ["b:main", "a:main"],
    ]


def test_repeated_main_counts_only_the_first(adapter: PythonAdapter) -> None:
    program = _program(adapter, {FILE: PLAIN_MAIN + NEWLINE + NEWLINE + PLAIN_MAIN})

    assert [chunk.id for chunk in program.chunks] == ["app:main", "app:main~2"]
    assert adapter.entry_candidates(program) == [["app:main"]]


def test_package_init_main_is_a_candidate(adapter: PythonAdapter) -> None:
    program = _program(adapter, {"pkg/__init__.py": PLAIN_MAIN})

    assert adapter.entry_candidates(program) == [["pkg:main"]]


def test_method_and_nested_main_are_not_candidates(adapter: PythonAdapter) -> None:
    program = _code_program(
        adapter,
        """\
        class Tool:
            def main(self):
                pass


        def outer():
            def main():
                pass
        """,
    )

    assert {chunk.id for chunk in program.chunks} >= {"app:Tool.main", "app:outer.main"}
    assert adapter.entry_candidates(program) == []


def test_no_candidates_gives_an_empty_list(adapter: PythonAdapter) -> None:
    program = _code_program(
        adapter,
        """\
        def helper():
            print("no main and no top-level calls")
        """,
    )

    assert adapter.entry_candidates(program) == []


def test_empty_program_gives_an_empty_list(adapter: PythonAdapter) -> None:
    assert adapter.entry_candidates(Program(language="python")) == []


def test_entry_candidates_raises_key_error_for_a_file_never_extracted(
    adapter: PythonAdapter,
) -> None:
    chunks = extract(parse_bytes(GUARDED_PRINT.encode()), FILE)
    program = Program(language="python", files=[FILE], chunks=chunks)

    with pytest.raises(KeyError):
        adapter.entry_candidates(program)
