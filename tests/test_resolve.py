"""Tests for Python call resolution in ``minstrel.adapters.python.resolve``.

Expected statuses and targets are written by hand from docs/decisions.md
("M2 contracts": every Resolver line, fix rounds 2-4, the threat model and
the known gaps) and the ``resolve`` docstring, never copied from the
resolver's output.

Every case is one module ``app`` (file ``app.py``) unless it says otherwise,
so a def ``helper`` has chunk id ``app:helper`` and the first call in
``main`` has id ``app:main#call1``.
"""

import time
from pathlib import Path
from textwrap import dedent, indent

import pytest
from tree_sitter import Tree

from minstrel.adapters.python.extract import extract, parse, parse_bytes
from minstrel.adapters.python.resolve import MAX_TARGETS, resolve
from minstrel.ir import SCHEMA_VERSION, Program, iter_call_sites

FIXTURES = Path(__file__).parent / "fixtures" / "python"
FILE = "app.py"
# Built with chr() rather than typed as an escape sequence.
NEWLINE = chr(10)
FULLWIDTH_F = chr(0xFF46)
FULLWIDTH_H = chr(0xFF48)

# A call's (status, targets).
Outcome = tuple[str, list[str]]

UNRESOLVED: Outcome = ("unresolved", [])
EXTERNAL: Outcome = ("external", [])


def _resolved(chunk_id: str) -> Outcome:
    """Return the outcome of a call resolved to the one chunk ``chunk_id``."""
    return ("resolved", [chunk_id])


RESOLVED_HELPER = _resolved("app:helper")

# A module-level ``helper`` and a ``main`` whose first call is ``helper()``;
# ``{body}`` is the rest of ``main``, indented to fit.
MAIN_CALLS_HELPER = """\
def helper():
    pass


def main():
    helper()
{body}"""

# A class whose method ``m`` calls ``self.b()``; the call is ``app:C.m#call1``.
SELF_CALL = """\
class C:
    def b(self):
        pass

    def m(self):
        self.b()


"""

# A class with its own ``__init__``, called from ``main``.
CLASS_WITH_INIT = """\
class Foo:
    def __init__(self):
        pass


def main():
    Foo()


"""

# A class with no ``__init__`` of its own, called from ``main``.
CLASS_WITHOUT_INIT = """\
class Foo:
    pass


def main():
    Foo()


"""

# A module-level def; ``{name}`` is its name.
NAMED_DEF = """\
def {name}():
    pass


"""
REPEATED_DEF = NAMED_DEF.format(name="f")

MAIN_CALLS_F = """\
def main():
    f()
"""

# Fixture case -> {call id: outcome} for every call site in the case's files.
FIXTURE_OUTCOMES: dict[str, dict[str, Outcome]] = {
    "hello_world": {
        "app:helloWorld#call1": EXTERNAL,
        "app:main#call1": _resolved("app:helloWorld"),
    },
    "chain": {
        "app:main#call1": _resolved("app:load"),
        "app:load#call1": _resolved("app:parse"),
        "app:parse#call1": _resolved("app:validate"),
    },
    "repeated_helper": {
        "app:main#call1": _resolved("app:log"),
        "app:main#call2": _resolved("app:work"),
        "app:main#call3": _resolved("app:log"),
        "app:work#call1": _resolved("app:log"),
        "app:log#call1": EXTERNAL,
    },
    "recursion": {
        "app:main#call1": _resolved("app:factorial"),
        "app:main#call2": EXTERNAL,
        "app:factorial#call1": _resolved("app:factorial"),
    },
    "mutual_recursion": {
        "app:main#call1": _resolved("app:is_even"),
        "app:main#call2": EXTERNAL,
        "app:is_even#call1": _resolved("app:is_odd"),
        "app:is_odd#call1": _resolved("app:is_even"),
    },
    "nested_function": {
        "app:main#call1": _resolved("app:outer"),
        "app:outer#call1": _resolved("app:outer.inner"),
        "app:outer.inner#call1": EXTERNAL,
    },
    "external_calls": {
        "app:main#call1": EXTERNAL,
        "app:main#call2": EXTERNAL,
        "app:main#call3": EXTERNAL,
    },
    "unresolved_call": {
        "app:main#call1": _resolved("app:setup"),
        "app:main#call2": UNRESOLVED,
        "app:setup#call1": EXTERNAL,
    },
    "dead_code": {
        "app:main#call1": _resolved("app:used"),
        "app:used#call1": EXTERNAL,
        "app:unused#call1": _resolved("app:also_unused"),
        "app:also_unused#call1": EXTERNAL,
    },
    "multiple_entries": {
        "backup:<module>#call1": _resolved("backup:run"),
        "backup:run#call1": EXTERNAL,
        "restore:<module>#call1": _resolved("restore:run"),
        "restore:run#call1": EXTERNAL,
    },
}

# Performance guard sizes, from decisions.md "M2 contracts" fix round 4.
MANY_DEFS = 2_000
MANY_CALLS = 20_000
NEST_DEPTH = 99
METHODS_PER_CLASS = 20
# Generous: the spec's crafted 1 MB file resolves in about 1.8 s.
NEST_SECONDS = 10.0

# A class name this long, with this many private names in its body, builds
# mangled ``_<name>__a`` names far past the walk's budget of
# max(50 chars per byte, 1,000,000).
LONG_CLASS_NAME = 20_000
PRIVATE_NAMES = 200


def _program(trees: dict[str, Tree]) -> Program:
    """Extract every file in ``trees`` into one unresolved Program."""
    chunks = [chunk for file, tree in trees.items() for chunk in extract(tree, file)]
    return Program(
        schema_version=SCHEMA_VERSION,
        language="python",
        files=list(trees),
        chunks=chunks,
    )


def _outcomes(program: Program) -> dict[str, Outcome]:
    """Return every call site's id with its (status, targets)."""
    return {
        site.id: (site.status, site.targets)
        for chunk in program.chunks
        for site in iter_call_sites(chunk.body)
    }


def _resolve_trees(trees: dict[str, Tree]) -> dict[str, Outcome]:
    """Extract and resolve the files in ``trees``; return each call's outcome."""
    program = _program(trees)
    resolve(program, trees)
    return _outcomes(program)


def _resolve_source(source: str) -> dict[str, Outcome]:
    """Resolve ``source`` as ``app.py``, as written (not dedented)."""
    return _resolve_trees({FILE: parse_bytes(source.encode())})


def _resolve_code(code: str) -> dict[str, Outcome]:
    """Resolve dedented ``code`` as ``app.py``; return each call's outcome."""
    return _resolve_source(dedent(code))


def _main_calls_helper(body: str) -> str:
    """Return ``MAIN_CALLS_HELPER`` with ``body`` as the rest of ``main``."""
    return MAIN_CALLS_HELPER.format(body=indent(dedent(body), "    "))


def _repeated_defs(count: int) -> str:
    """Return ``count`` module-level ``def f()`` and a ``main`` calling ``f()``."""
    return REPEATED_DEF * count + MAIN_CALLS_F


def _repeated_ids(count: int) -> list[str]:
    """Return the ids of ``count`` repeated ``f`` defs, latest first."""
    return [f"app:f~{number}" for number in range(count, 1, -1)] + ["app:f"]


# --- Golden fixture inputs ---------------------------------------------------


@pytest.mark.parametrize("case", FIXTURE_OUTCOMES)
def test_fixture_calls_resolve_as_expected(case: str) -> None:
    trees = {path.name: parse(path) for path in sorted((FIXTURES / case).glob("*.py"))}

    assert _resolve_trees(trees) == FIXTURE_OUTCOMES[case]


# --- Python scoping ------------------------------------------------------------


def test_own_nested_def_shadows_module_def() -> None:
    outcomes = _resolve_code(
        """\
        def helper():
            pass


        def outer():
            def helper():
                pass

            helper()
        """
    )

    assert outcomes["app:outer#call1"] == _resolved("app:outer.helper")


def test_enclosing_function_scope_is_searched_before_module() -> None:
    outcomes = _resolve_code(
        """\
        def helper():
            pass


        def outer():
            def helper():
                pass

            def inner():
                def deeper():
                    helper()

                helper()
        """
    )

    assert outcomes["app:outer.inner#call1"] == _resolved("app:outer.helper")
    assert outcomes["app:outer.inner.deeper#call1"] == _resolved("app:outer.helper")


def test_class_body_names_are_skipped_by_method_lookups() -> None:
    outcomes = _resolve_code(
        """\
        def helper():
            pass


        class C:
            def helper(self):
                pass

            other = 1

            def m(self):
                helper()
                other()
        """
    )

    # ``other`` is bound only in the class body, which ``m`` doesn't see.
    assert outcomes == {
        "app:C.m#call1": RESOLVED_HELPER,
        "app:C.m#call2": UNRESOLVED,
    }


def test_class_body_inside_a_function_is_skipped_too() -> None:
    outcomes = _resolve_code(
        """\
        def outer():
            def helper():
                pass

            class Local:
                def helper(self):
                    pass

                def m(self):
                    helper()
        """
    )

    assert outcomes["app:outer.Local.m#call1"] == _resolved("app:outer.helper")


def test_enclosing_function_assignment_stops_the_lookup() -> None:
    outcomes = _resolve_code(
        """\
        def helper():
            pass


        def outer():
            helper = make()

            def inner():
                helper()
        """
    )

    assert outcomes["app:outer.inner#call1"] == UNRESOLVED


def test_module_def_import_builtin_and_unknown_names() -> None:
    outcomes = _resolve_code(
        """\
        import json
        import os.path as osp
        from collections import OrderedDict as Ordered


        def helper():
            pass


        def main():
            helper()
            json()
            osp()
            Ordered()
            len()
            missing()
            json.dumps()
            osp.join()
            len.mystery()
            missing.attr()
        """
    )

    assert outcomes == {
        "app:main#call1": RESOLVED_HELPER,
        "app:main#call2": EXTERNAL,
        "app:main#call3": EXTERNAL,
        "app:main#call4": EXTERNAL,
        "app:main#call5": EXTERNAL,
        "app:main#call6": UNRESOLVED,
        "app:main#call7": EXTERNAL,
        "app:main#call8": EXTERNAL,
        "app:main#call9": EXTERNAL,
        "app:main#call10": UNRESOLVED,
    }


def test_function_scope_import_is_external() -> None:
    outcomes = _resolve_code(
        """\
        def helper():
            pass


        def main():
            from tools import helper

            helper()
        """
    )

    assert outcomes["app:main#call1"] == EXTERNAL


def test_def_named_like_a_builtin_at_module_scope_is_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        def print():
            pass


        class list:
            pass


        def main():
            print()
            list()

            def len():
                pass

            len()
        """
    )

    # Only module scope is affected: a local ``len`` def is the only binding.
    assert outcomes == {
        "app:main#call1": UNRESOLVED,
        "app:main#call2": UNRESOLVED,
        "app:main#call3": _resolved("app:main.len"),
    }


def test_call_in_class_body_to_a_name_the_class_binds_is_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        def helper():
            pass


        def other():
            pass


        class C:
            def helper(self):
                pass

            first = helper()
            second = other()
        """
    )

    assert outcomes == {
        "app:<module>#call1": UNRESOLVED,
        "app:<module>#call2": _resolved("app:other"),
    }


# --- self.m() and cls.m() ----------------------------------------------------


def test_self_and_cls_calls_resolve_in_the_same_class() -> None:
    outcomes = _resolve_code(
        """\
        class C:
            def b(self):
                pass

            def m(self):
                self.b()

            @classmethod
            def k(cls):
                cls.b()


        class D:
            def b(self):
                pass

            class Inner:
                def b(self):
                    pass

                def m(self):
                    self.b()
        """
    )

    assert outcomes == {
        "app:C.m#call1": _resolved("app:C.b"),
        "app:C.k#call1": _resolved("app:C.b"),
        "app:D.Inner.m#call1": _resolved("app:D.Inner.b"),
    }


def test_self_call_to_a_method_the_class_lacks_is_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        class C:
            def m(self):
                self.missing()
        """
    )

    assert outcomes["app:C.m#call1"] == UNRESOLVED


@pytest.mark.parametrize(
    ("code", "call_id"),
    [
        pytest.param(
            """\
            class C:
                def b(self):
                    pass

                def m(self):
                    def inner():
                        self.b()
            """,
            "app:C.m.inner#call1",
            id="self-from-enclosing-method",
        ),
        pytest.param(
            """\
            class C:
                def b(self):
                    pass

                @staticmethod
                def m():
                    self.b()


            self = C()
            """,
            "app:C.m#call1",
            id="self-is-a-module-name",
        ),
        pytest.param(
            """\
            class C:
                def b(self):
                    pass

                def m(this):
                    self.b()
            """,
            "app:C.m#call1",
            id="self-is-not-bound",
        ),
        pytest.param(
            """\
            class C:
                def b(self):
                    pass

                def m(self):
                    self = other()
                    self.b()
            """,
            "app:C.m#call2",
            id="self-rebound-in-method",
        ),
        pytest.param(
            """\
            def b():
                pass


            def m(self):
                self.b()
            """,
            "app:m#call1",
            id="not-a-method",
        ),
    ],
)
def test_self_call_needs_self_as_the_methods_own_parameter(
    code: str, call_id: str
) -> None:
    assert _resolve_code(code)[call_id] == UNRESOLVED


# --- Calling a class ---------------------------------------------------------


def test_calling_a_class_resolves_to_its_init() -> None:
    outcomes = _resolve_source(CLASS_WITH_INIT)

    assert outcomes == {"app:main#call1": _resolved("app:Foo.__init__")}


def test_calling_a_class_without_init_is_external() -> None:
    outcomes = _resolve_source(CLASS_WITHOUT_INIT)

    assert outcomes == {"app:main#call1": EXTERNAL}


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(
            """\
            class Base:
                def __init__(self):
                    pass
            """,
            id="other-class-defines-init",
        ),
        pytest.param(
            """\
            class Base:
                __init__ = make_init()
            """,
            id="other-class-assigns-init",
        ),
        pytest.param(
            """\
            class Data:
                def __post_init__(self):
                    pass
            """,
            id="other-class-defines-post-init",
        ),
        pytest.param("helper.__code__ = other.__code__\n", id="code-swapped"),
    ],
)
def test_calling_a_class_without_init_is_unresolved_when_one_may_run(
    extra: str,
) -> None:
    outcomes = _resolve_source(CLASS_WITHOUT_INIT + dedent(extra))

    assert outcomes["app:main#call1"] == UNRESOLVED


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(
            """\
            class Other:
                def __new__(cls):
                    pass
            """,
            id="a-class-defines-new",
        ),
        pytest.param(
            """\
            class Meta(type):
                def __call__(cls):
                    pass
            """,
            id="a-class-defines-call",
        ),
        pytest.param(
            """\
            class Other(metaclass=Meta):
                pass
            """,
            id="metaclass-keyword",
        ),
        pytest.param(
            """\
            class Other(**options):
                pass
            """,
            id="class-keyword-splat",
        ),
        pytest.param("Foo.__init__ = other\n", id="init-attribute-written"),
        pytest.param("Foo.__new__ = other\n", id="new-attribute-written"),
        pytest.param("Foo.__call__ = other\n", id="call-attribute-written"),
        pytest.param("obj.__class__ = Other\n", id="class-attribute-written"),
        pytest.param("del Foo.__init__\n", id="init-attribute-deleted"),
        pytest.param("helper.__code__ = other.__code__\n", id="code-swapped"),
    ],
)
def test_calling_a_class_is_unresolved_when_other_code_may_run_first(
    extra: str,
) -> None:
    outcomes = _resolve_source(CLASS_WITH_INIT + dedent(extra))

    assert outcomes["app:main#call1"] == UNRESOLVED


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(
            """\
            class Foo:
                def __init__(self):
                    pass

                def __new__(cls):
                    pass


            def main():
                Foo()
            """,
            id="own-new",
        ),
        pytest.param(
            """\
            class Foo(Base, metaclass=Meta):
                def __init__(self):
                    pass


            def main():
                Foo()
            """,
            id="own-metaclass",
        ),
        pytest.param(
            """\
            class Foo:
                __init__ = make_init()


            def main():
                Foo()
            """,
            id="init-bound-not-by-def",
        ),
    ],
)
def test_calling_a_class_with_its_own_constructor_hook_is_unresolved(
    code: str,
) -> None:
    assert _resolve_code(code)["app:main#call1"] == UNRESOLVED


# --- Ambiguous calls -----------------------------------------------------------


def test_repeated_defs_are_ambiguous_latest_first() -> None:
    outcomes = _resolve_source(_repeated_defs(3))

    assert outcomes["app:main#call1"] == ("ambiguous", _repeated_ids(3))


def test_nfkc_equal_def_names_are_the_same_name() -> None:
    code = REPEATED_DEF + NAMED_DEF.format(name=FULLWIDTH_F) + MAIN_CALLS_F

    outcomes = _resolve_source(code)

    assert outcomes["app:main#call1"] == ("ambiguous", [f"app:{FULLWIDTH_F}", "app:f"])


def test_nfkc_equal_call_name_finds_the_def() -> None:
    code = _main_calls_helper("").replace("    helper()", f"    {FULLWIDTH_H}elper()")

    assert _resolve_source(code)["app:main#call1"] == RESOLVED_HELPER


def test_exactly_max_targets_repeated_defs_are_ambiguous() -> None:
    outcomes = _resolve_source(_repeated_defs(MAX_TARGETS))

    assert outcomes["app:main#call1"] == ("ambiguous", _repeated_ids(MAX_TARGETS))


def test_more_than_max_targets_repeated_defs_are_unresolved() -> None:
    outcomes = _resolve_source(_repeated_defs(MAX_TARGETS + 1))

    assert outcomes["app:main#call1"] == UNRESOLVED


def test_max_targets_is_sixteen() -> None:
    assert MAX_TARGETS == 16


def test_call_sites_never_share_a_target_list() -> None:
    tree = parse_bytes((_repeated_defs(2) + "    f()" + NEWLINE).encode())
    program = _program({FILE: tree})
    resolve(program, {FILE: tree})
    first, second = iter_call_sites(program.chunks[-1].body)

    assert first.targets == second.targets == _repeated_ids(2)
    assert first.targets is not second.targets


# --- Binding kinds that stop a lookup --------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("helper = other\n", id="assignment"),
        pytest.param("helper: Callable = other\n", id="annotated-assignment"),
        pytest.param("first, *helper = items\n", id="unpacking"),
        pytest.param("helper += other\n", id="augmented-assignment"),
        pytest.param(
            """\
            for helper in items:
                pass
            """,
            id="for-target",
        ),
        pytest.param(
            """\
            with open("f") as helper:
                pass
            """,
            id="with-target",
        ),
        pytest.param(
            """\
            try:
                pass
            except ValueError as helper:
                pass
            """,
            id="except-target",
        ),
        pytest.param("value = (helper := other)\n", id="walrus"),
        pytest.param(
            """\
            match command:
                case [helper]:
                    pass
            """,
            id="match-capture",
        ),
        pytest.param(
            """\
            match command:
                case str() as helper:
                    pass
            """,
            id="match-as",
        ),
        pytest.param("del helper\n", id="del"),
        pytest.param("run(lambda helper: helper)\n", id="lambda-parameter"),
        pytest.param("values = [0 for helper in items]\n", id="comprehension"),
        pytest.param(
            """\
            def helper():
                pass

            helper = wrap(helper)
            """,
            id="def-and-assignment",
        ),
        pytest.param(
            """\
            def helper():
                pass

            from tools import helper
            """,
            id="def-and-import",
        ),
    ],
)
def test_other_binding_in_the_calling_scope_makes_the_call_unresolved(
    body: str,
) -> None:
    # ``main`` calls ``helper()`` first; the binding later in ``main`` still
    # puts the name in ``main``'s scope, as in Python.
    outcomes = _resolve_source(_main_calls_helper(body))

    assert outcomes["app:main#call1"] == UNRESOLVED


def test_parameter_named_like_a_def_makes_the_call_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        def helper():
            pass


        def main(helper):
            helper()
        """
    )

    assert outcomes["app:main#call1"] == UNRESOLVED


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param("helper = wrap(helper)\n", id="assignment"),
        pytest.param("import helper\n", id="import"),
        pytest.param("from tools import helper\n", id="from-import"),
        pytest.param(
            """\
            class helper:
                pass
            """,
            id="class",
        ),
        pytest.param(
            """\
            for helper in items:
                pass
            """,
            id="for-target",
        ),
        pytest.param(
            """\
            def rebind():
                global helper
                helper = other
            """,
            id="global-in-another-function",
        ),
    ],
)
def test_module_def_mixed_with_another_binding_is_unresolved(extra: str) -> None:
    outcomes = _resolve_source(_main_calls_helper("") + NEWLINE + dedent(extra))

    assert outcomes["app:main#call1"] == UNRESOLVED


def test_nonlocal_declaration_makes_the_enclosing_def_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        def outer():
            def helper():
                pass

            def rebind():
                nonlocal helper
                helper = other

            helper()
        """
    )

    assert outcomes["app:outer#call1"] == UNRESOLVED


def test_wildcard_import_makes_defs_unresolved_and_unknown_names_external() -> None:
    outcomes = _resolve_code(
        """\
        from tools import *


        def helper():
            pass


        class Foo:
            pass


        def main():
            helper()
            Foo()
            unknown()
        """
    )

    assert outcomes == {
        "app:main#call1": UNRESOLVED,
        "app:main#call2": UNRESOLVED,
        "app:main#call3": EXTERNAL,
    }


# --- Name mangling -------------------------------------------------------------


def test_bare_private_name_inside_a_class_is_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        def __helper():
            pass


        class C:
            first = __helper()

            def m(self):
                __helper()

                def inner():
                    __helper()


        def main():
            __helper()
        """
    )

    assert outcomes == {
        "app:<module>#call1": UNRESOLVED,
        "app:C.m#call1": UNRESOLVED,
        "app:C.m.inner#call1": UNRESOLVED,
        "app:main#call1": _resolved("app:__helper"),
    }


def test_private_binding_in_a_class_also_binds_the_mangled_name() -> None:
    outcomes = _resolve_code(
        """\
        class C:
            def _C__b(self):
                pass

            __b = None

            def m(self):
                self._C__b()
        """
    )

    # In class C, ``__b = None`` rebinds ``_C__b``, replacing the def.
    assert outcomes["app:C.m#call1"] == UNRESOLVED


def test_private_global_in_a_class_binds_the_mangled_module_name() -> None:
    outcomes = _resolve_code(
        """\
        def _C__helper():
            pass


        class C:
            def m(self):
                global __helper
                __helper = None


        def main():
            _C__helper()
        """
    )

    assert outcomes["app:main#call1"] == UNRESOLVED


# --- Syntax errors -------------------------------------------------------------


def test_every_call_in_a_file_with_syntax_errors_is_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        import os


        def helper():
            pass


        def main():
            helper()
            print("x")
            os.getcwd()
            ", ".join([])


        broken = (
        """
    )

    assert outcomes == {
        "app:main#call1": UNRESOLVED,
        "app:main#call2": UNRESOLVED,
        "app:main#call3": UNRESOLVED,
        "app:main#call4": UNRESOLVED,
    }


# --- Attribute-target guards ---------------------------------------------------


def test_self_call_resolves_when_no_attribute_target_interferes() -> None:
    outcomes = _resolve_source(SELF_CALL + "X.c = None\n")

    assert outcomes["app:C.m#call1"] == _resolved("app:C.b")


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param("X.b = None\n", id="same-name-attribute"),
        pytest.param(
            """\
            def patch(obj):
                obj.b = None
            """,
            id="attribute-in-a-function",
        ),
        pytest.param("del X.b\n", id="attribute-deleted"),
        pytest.param(
            """\
            for X.b in items:
                pass
            """,
            id="attribute-for-target",
        ),
        pytest.param("obj.__class__ = Other\n", id="class-written"),
        pytest.param("obj.__dict__ = {}\n", id="dict-written"),
        pytest.param("obj.__getattribute__ = hook\n", id="getattribute-written"),
        pytest.param('obj.__dict__["b"] = hook\n', id="dict-item-written"),
        pytest.param('del obj.__dict__["b"]\n', id="dict-item-deleted"),
        pytest.param("helper.__code__ = other.__code__\n", id="code-swapped"),
    ],
)
def test_attribute_target_blocks_self_call(extra: str) -> None:
    outcomes = _resolve_source(SELF_CALL + dedent(extra))

    assert outcomes["app:C.m#call1"] == UNRESOLVED


def test_code_assignment_blocks_links_and_external() -> None:
    code = """\
        import os


        def helper():
            pass


        def main():
            helper()
            os.getcwd()
            len("")
            ", ".join([])
        """
    plain = _resolve_code(code)
    swapped = _resolve_code(code + "\n        helper.__code__ = main.__code__\n")

    assert plain == {
        "app:main#call1": RESOLVED_HELPER,
        "app:main#call2": EXTERNAL,
        "app:main#call3": EXTERNAL,
        "app:main#call4": EXTERNAL,
    }
    assert swapped == {
        "app:main#call1": UNRESOLVED,
        "app:main#call2": UNRESOLVED,
        "app:main#call3": UNRESOLVED,
        "app:main#call4": UNRESOLVED,
    }


def test_builtins_attribute_write_blocks_that_builtin_only() -> None:
    outcomes = _resolve_code(
        """\
        import builtins

        builtins.len = fake


        def main():
            len("")
            print("")
        """
    )

    assert outcomes == {
        "app:main#call1": UNRESOLVED,
        "app:main#call2": EXTERNAL,
    }


def test_imported_module_attribute_write_blocks_that_attribute_only() -> None:
    outcomes = _resolve_code(
        """\
        import os

        os.system = fake


        def main():
            os.system("ls")
            os.getcwd()
        """
    )

    assert outcomes == {
        "app:main#call1": UNRESOLVED,
        "app:main#call2": EXTERNAL,
    }


def test_module_alias_attribute_write_blocks_that_module_def() -> None:
    outcomes = _resolve_code(
        """\
        import app as me


        def helper():
            pass


        def other():
            pass


        me.helper = fake


        def main():
            helper()
            other()
        """
    )

    assert outcomes == {
        "app:main#call1": UNRESOLVED,
        "app:main#call2": _resolved("app:other"),
    }


def test_attribute_write_does_not_block_a_function_scope_root() -> None:
    outcomes = _resolve_code(
        """\
        def outer():
            def helper():
                pass

            helper()


        obj.helper = fake
        """
    )

    assert outcomes["app:outer#call1"] == _resolved("app:outer.helper")


# ``outer`` calls a def in its own scope (call1) and a module-level def (call2).
CLOSURE_CALLS = """\
def helper():
    pass


def outer():
    def local():
        pass

    local()
    helper()


"""


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        pytest.param(
            "outer.__closure__[0].cell_contents = fake\n",
            {"app:outer#call1": UNRESOLVED, "app:outer#call2": RESOLVED_HELPER},
            id="cell-contents",
        ),
        pytest.param(
            'frame.f_locals["local"] = fake\n',
            {"app:outer#call1": UNRESOLVED, "app:outer#call2": UNRESOLVED},
            id="f-locals-item",
        ),
        pytest.param(
            'obj.__dict__["helper"] = fake\n',
            {
                "app:outer#call1": _resolved("app:outer.local"),
                "app:outer#call2": UNRESOLVED,
            },
            id="dict-item",
        ),
        pytest.param(
            'del obj.__dict__["helper"]\n',
            {
                "app:outer#call1": _resolved("app:outer.local"),
                "app:outer#call2": UNRESOLVED,
            },
            id="dict-item-deleted",
        ),
        pytest.param(
            'outer.__globals__["helper"] = fake\n',
            {
                "app:outer#call1": _resolved("app:outer.local"),
                "app:outer#call2": UNRESOLVED,
            },
            id="globals-item",
        ),
    ],
)
def test_namespace_and_cell_writes_block_the_calls_they_can_reach(
    extra: str, expected: dict[str, Outcome]
) -> None:
    assert _resolve_source(CLOSURE_CALLS + extra) == expected


def test_namespace_item_write_blocks_builtin_and_imported_calls() -> None:
    outcomes = _resolve_code(
        """\
        import os


        def main():
            len("")
            os.getcwd()


        self.__dict__["k"] = v
        """
    )

    assert outcomes == {
        "app:main#call1": UNRESOLVED,
        "app:main#call2": UNRESOLVED,
    }


# --- Repeated qualnames, conditional defs, __getattribute__ --------------------


def test_repeated_function_qualname_makes_bindings_inside_it_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        def outer():
            def helper():
                pass

            helper()


        def outer():
            pass
        """
    )

    assert outcomes["app:outer#call1"] == UNRESOLVED


def test_repeated_class_qualname_makes_self_calls_unresolved() -> None:
    outcomes = _resolve_source(SELF_CALL + "class C:\n    pass\n")

    assert outcomes["app:C.m#call1"] == UNRESOLVED


@pytest.mark.parametrize(
    "guarded_def",
    [
        pytest.param(
            """\
            if FLAG:
                def b(self):
                    pass
            """,
            id="if",
        ),
        pytest.param(
            """\
            try:
                def b(self):
                    pass
            except ImportError:
                pass
            """,
            id="try",
        ),
        pytest.param(
            """\
            for _ in range(1):
                def b(self):
                    pass
            """,
            id="for",
        ),
    ],
)
def test_def_not_directly_in_class_body_makes_self_call_unresolved(
    guarded_def: str,
) -> None:
    code = (
        "class C:"
        + NEWLINE
        + indent(dedent(guarded_def), "    ")
        + NEWLINE
        + "    def m(self):"
        + NEWLINE
        + "        self.b()"
        + NEWLINE
    )

    assert _resolve_source(code)["app:C.m#call1"] == UNRESOLVED


def test_class_binding_getattribute_makes_self_call_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        class C:
            def __getattribute__(self, name):
                pass

            def b(self):
                pass

            def m(self):
                self.b()
        """
    )

    assert outcomes["app:C.m#call1"] == UNRESOLVED


# --- Other callees -------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        pytest.param('", ".join(parts)', EXTERNAL, id="string-literal"),
        pytest.param('b"ab".hex()', EXTERNAL, id="bytes-literal"),
        pytest.param("1 .bit_length()", EXTERNAL, id="number-literal"),
        pytest.param('"".__class__.mro()', UNRESOLVED, id="longer-literal-chain"),
        pytest.param("[helper][0]()", UNRESOLVED, id="list-display"),
        pytest.param('{"k": helper}["k"]()', UNRESOLVED, id="dict-display"),
        pytest.param("{helper}.pop()", UNRESOLVED, id="set-display"),
        pytest.param("(helper,)[0]()", UNRESOLVED, id="tuple-display"),
    ],
)
def test_calls_on_literals_and_displays(statement: str, expected: Outcome) -> None:
    code = NAMED_DEF.format(name="helper") + "def main():" + NEWLINE
    code += "    " + statement + NEWLINE

    assert _resolve_source(code)["app:main#call1"] == expected


def test_attribute_subscript_and_call_results_are_unresolved() -> None:
    outcomes = _resolve_code(
        """\
        def helper():
            pass


        class Foo:
            def m(self):
                super().m()


        def main(obj, items):
            obj.m()
            items[0]()
            helper()()
            Foo.m()
        """
    )

    assert outcomes == {
        "app:Foo.m#call1": EXTERNAL,
        "app:Foo.m#call2": UNRESOLVED,
        "app:main#call1": UNRESOLVED,
        "app:main#call2": UNRESOLVED,
        "app:main#call3": RESOLVED_HELPER,
        "app:main#call4": UNRESOLVED,
        "app:main#call5": UNRESOLVED,
    }


# --- Errors and budgets ----------------------------------------------------------


def test_resolve_raises_key_error_when_a_chunk_file_has_no_tree() -> None:
    tree = parse_bytes(_main_calls_helper("").encode())
    program = _program({FILE: tree})

    with pytest.raises(KeyError):
        resolve(program, {})


def test_resolve_raises_value_error_when_mangled_names_pass_the_budget() -> None:
    name = "K" * LONG_CLASS_NAME
    code = f"class {name}:" + NEWLINE + ("    __a = 0" + NEWLINE) * PRIVATE_NAMES
    tree = parse_bytes(code.encode())
    program = _program({FILE: tree})

    with pytest.raises(ValueError):
        resolve(program, {FILE: tree})


# --- Performance guards ----------------------------------------------------------


def test_many_repeated_defs_called_many_times_build_no_targets() -> None:
    code = ("def f():0" + NEWLINE) * MANY_DEFS + ("f()" + NEWLINE) * MANY_CALLS

    outcomes = _resolve_source(code)

    assert len(outcomes) == MANY_CALLS
    assert {status for status, _ in outcomes.values()} == {"unresolved"}
    assert sum(len(targets) for _, targets in outcomes.values()) == 0


def _nested_classes(depth: int, methods: int) -> tuple[str, dict[str, Outcome]]:
    """Return ``depth`` nested classes of ``methods`` methods, and the outcomes.

    Each method ``mJ`` calls ``self.m<J+1>()`` (wrapping to ``m0``) and the
    module-level ``helper()``.
    """
    lines = ["def helper():", "    pass"]
    expected: dict[str, Outcome] = {}
    qualname = ""
    for level in range(depth):
        pad = "    " * level
        qualname = f"{qualname}.C{level}" if qualname else f"C{level}"
        lines.append(f"{pad}class C{level}:")
        for number in range(methods):
            following = (number + 1) % methods
            lines.append(f"{pad}    def m{number}(self): self.m{following}(); helper()")
            caller = f"app:{qualname}.m{number}"
            expected[f"{caller}#call1"] = _resolved(f"app:{qualname}.m{following}")
            expected[f"{caller}#call2"] = RESOLVED_HELPER
    return NEWLINE.join(lines) + NEWLINE, expected


def test_deeply_nested_classes_with_many_methods_resolve_quickly() -> None:
    code, expected = _nested_classes(NEST_DEPTH, METHODS_PER_CLASS)
    tree = parse_bytes(code.encode())
    program = _program({FILE: tree})

    started = time.perf_counter()
    resolve(program, {FILE: tree})
    elapsed = time.perf_counter() - started

    assert _outcomes(program) == expected
    assert elapsed < NEST_SECONDS
