"""Python call resolution (M2: one module at a time).

Fills each CallSite's ``targets`` and ``status`` by looking its callee text up
in the scopes of the chunk that makes the call, following docs/decisions.md
("M2 contracts").

Threat model: a link is a static best guess. ``resolved`` means no binding
written in the file contradicts it; rebinding that the source text can't show
(``setattr``, ``globals()``/``vars()``, ``exec``, ``sys.modules[...]``,
metaclasses or class decorators rewriting a class) isn't tracked. A wrong link
is worse than a missing one, so anything the lookup can't vouch for is
``unresolved``:

- In a file with syntax errors, every call: Python can't run the file, and an
  ERROR subtree can hide bindings.
- A bare name is looked up in the chunk's own scope, each enclosing
  *function* scope (class bodies are skipped, as in Python), then module
  scope, stopping at the first scope that binds it in any way. Only defs
  there: ``resolved`` (or ``ambiguous``, latest definition first, but
  ``unresolved`` past ``MAX_TARGETS`` defs, as for every call). Only a
  class: see below. Only imports: ``external``. Any other binding
  (assignment, parameter, loop target, ``del``, ``global``...) or a mix:
  ``unresolved``. Bound nowhere: ``external`` for a builtin, else
  ``unresolved``.
- A def or class also counts as another binding where its name may hold
  something else when a call runs: under ``if``/``try``/``with``/``for``/
  ``match`` in a class body (an inherited name shows through), at module
  level under a builtin's name (code that runs first sees the builtin), in a
  scope with ``from m import *`` (whose other names are ``external``), and
  inside a qualname several ``def``/``class`` statements share (they share
  one table).
- Inside a class, Python mangles ``__x`` to ``_Class__x``: a bare ``__x``
  call there is ``unresolved``, and binding either spelling there binds the
  other as ``other``. A call directly in a class body sees the class's names
  first, so it is ``unresolved`` when any class body binds its name.
- An attribute target can rebind a name without naming it, and doesn't show
  which object it writes to: a module-level name is an attribute of the
  module (``import app as me; me.helper = x``), a builtin one of
  ``builtins`` (``builtins.len = x``), and an import's attributes belong to
  another module (``os.system = x``). So a call is ``unresolved`` when the
  file assigns or deletes an attribute named like (``_attribute_keys``) any
  later part of its dotted callee, or like its root when the lookup finds
  the root at module level, as a builtin or as an import. Found there, any
  item write to an ``__dict__``, ``__globals__`` or ``f_locals`` also makes
  it ``unresolved`` (so ``self.__dict__[k] = v`` costs the file its
  module-level links). Found in a function scope, where a closure cell may
  hold it, so does any ``cell_contents`` target or ``f_locals`` item write.
- ``self.m()``/``cls.m()``, where ``self``/``cls`` is a parameter the calling
  method never rebinds, resolve to the defs of ``m`` in the same class, unless
  the file assigns or deletes an attribute ``m`` (or a spelling mangling may
  equate with it), ``__class__``, ``__dict__`` or ``__getattribute__`` on any
  object, assigns or deletes an item of some ``obj.__dict__``, or a class
  binds ``__getattribute__``.
- Calling a class links its own ``__init__``. With none it is ``external``
  until M6 adds inherited ones, but ``unresolved`` if any class in the file
  binds ``__init__`` (a base's may run) or ``__post_init__`` (a dataclass's
  generated ``__init__`` calls it). It is ``unresolved`` if any class in
  the file binds ``__new__`` or ``__call__`` (a base's ``__new__`` or a
  metaclass's ``__call__`` runs first), a class statement passes
  ``metaclass=`` or ``**``, or the file assigns an attribute ``__init__``,
  ``__new__``, ``__call__`` or ``__class__``.
- A file assigning any ``__code__`` swaps a function's body without rebinding
  a name, so none of its defs is linked and no call is ``external``.
- A dotted callee whose root is only imported, or a builtin, is ``external``;
  a method right on a string, bytes or number literal (``", ".join``) is
  ``external``; anything else (``obj.m``, ``x[0]``, ``f()()``, ``[f][0]``,
  ``super().m``) is ``unresolved``.

Identifiers are NFKC-normalized on both sides, as Python does. Defs come from
the chunks; every other binding comes from the file's tree. Lambda parameters
and comprehension variables count as bindings of the enclosing scope, whose
chunk the calls in them belong to. Each scope's place in the lookup chain is
worked out once, from its parent's, and lookups keep each name's binding
scopes on the current chain (``_VisibleBindings``), so resolving a file takes
time linear in its size however deeply its scopes nest. ``_resolve_callee``
is the one place callees are looked up, so M5 (names imported from other
files) and M6 (inheritance, attribute types) extend it.
"""

from __future__ import annotations

import builtins
import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from itertools import accumulate, pairwise
from typing import Literal

from tree_sitter import Node, Tree

from minstrel.adapters.python.extract import _text
from minstrel.ir import CallSite, CallStatus, Chunk, Program, iter_call_sites

# Names that resolve without any def or import, from the running interpreter.
_BUILTINS = frozenset(dir(builtins))

_SELF_NAMES = frozenset({"self", "cls"})
_DEFINITIONS = frozenset({"function_definition", "class_definition"})
_IMPORTS = frozenset(
    {"import_statement", "import_from_statement", "future_import_statement"}
)

# Attributes whose assignment anywhere can change what ``self.m`` finds: the
# instance's class, its namespace, or attribute lookup itself.
_INSTANCE_HOOKS = frozenset({"__class__", "__dict__", "__getattribute__"})
# Attributes whose assignment anywhere can change what calling a class runs.
_CONSTRUCTOR_HOOKS = frozenset({"__init__", "__new__", "__call__", "__class__"})
# Names that, bound in any class body, may run before a class's ``__init__``
# when it's called, and skip it: a base's ``__new__``, a metaclass's
# ``__call__`` (a metaclass may be used without ``metaclass=``, as in
# ``Base = Meta("Base", (), {})``).
_CONSTRUCTOR_OVERRIDES = frozenset({"__new__", "__call__"})
# Names that, bound in any class body, mean calling a class with no
# ``__init__`` of its own may still run project code: a base's ``__init__``,
# or the ``__post_init__`` a dataclass-generated ``__init__`` calls.
_INIT_RUNNERS = frozenset({"__init__", "__post_init__"})
# An attribute whose assignment anywhere swaps a function's body.
_CODE_HOOK = "__code__"
# Namespaces whose item writes (``me.__dict__["f"] = x``,
# ``f.__globals__["f"] = x``, ``frame.f_locals["f"] = x``) rebind a module-level
# or builtin name, or an import's attribute, without naming it in a target.
_NAMESPACE_ITEMS = frozenset({"__dict__", "__globals__", "f_locals"})
# Attributes whose assignment (``run.__closure__[0].cell_contents = x``) or item
# write (``frame.f_locals["f"] = x``, Python 3.13+) rebinds a function-level
# name without naming it.
_CELL_HOOKS = frozenset({"cell_contents", "f_locals"})

# Most target chunks one call site may hold. Each call site copies its own
# list, so K same-scope defs of one name called M times would build K * M
# entries, quadratic in the file's size (2,000 ``def f():0`` lines and 20,000
# ``f()`` lines make 40 million); with more candidates than this a call is
# ``unresolved``, which keeps resolving linear in the file's size.
MAX_TARGETS = 16

# How a scope binds a name: by ``def``, ``class``, import, as a parameter of
# the def that is the scope, or any other way.
_Kind = Literal["def", "class", "import", "parameter", "other"]

# Nodes binding the names in one field: ``x = 1``, ``x: int``, ``x += 1``,
# ``for x in``, ``[... for x in ...]``, ``(x := 1)``, ``type X = int``, and
# ``lambda x: ...`` (counted in the enclosing scope, whose chunk its calls
# belong to).
_TARGET_FIELDS = {
    "assignment": "left",
    "augmented_assignment": "left",
    "for_statement": "left",
    "for_in_clause": "left",
    "named_expression": "name",
    "type_alias_statement": "left",
    "lambda": "parameters",
}
# Nodes binding every name in them but the parts they read: ``del a, b``,
# ``global a``, ``nonlocal a``, and a ``case`` clause's patterns.
_NAME_LISTS = frozenset(
    {"delete_statement", "global_statement", "nonlocal_statement", "case_clause"}
)
# Target parts that bind no name: ``a[i] = 1`` reads ``a`` and ``i``, and a
# ``case`` clause's guard and body aren't its pattern. (``_target_parts``
# takes attribute targets and ``_NAMESPACE_ITEMS`` items apart itself.)
_NOT_TARGETS = frozenset({"subscript", "if_clause", "block"})
# Target parts where only the first named child binds: a parameter, whose
# default and annotation are read.
_BINDS_FIRST = frozenset(
    {"default_parameter", "typed_parameter", "typed_default_parameter"}
)
# Pattern parts whose first named child is read: the class ``Point`` and the
# keyword ``x`` in ``case Point(x=px)``.
_READS_FIRST = frozenset({"class_pattern", "keyword_pattern"})

# Stands in for a def or class name a syntax error left out. Chunks under such
# a name are skipped (see _names_in_file), so no lookup reaches what it binds.
_MISSING_NAME = "<missing>"

# Characters of qualnames and mangled ``_Class__x`` names the walk over one
# file may build: _NAME_CHARS_PER_BYTE times the file size, but at least
# _MIN_NAME_CHARS. Each such name repeats enclosing names, so many defs or
# private names under long names could otherwise build far more than the
# file holds; normal code stays far below this.
_NAME_CHARS_PER_BYTE = 50
_MIN_NAME_CHARS = 1_000_000

# A string or bytes literal with any prefix: the greedy ``.*`` up to the last
# quote also takes in implicit concatenation (``"a" "b"``). A number literal:
# digits, letters and underscores (``1_000``, ``3j``), an optional fraction,
# and a sign only right after an exponent's ``e`` or ``E`` (``1.5e+3``).
_STRING_LITERAL = re.compile(r"""[rRbBuUfFtT]{0,2}["'].*["']""", re.DOTALL)
_NUMBER_LITERAL = re.compile(r"[0-9]\w*(?:\.\w*)?(?:(?<=[eE])[+-]\w+)?")


class _LineRanges:
    """Inclusive line ranges, for asking whether any of them covers a line."""

    def __init__(self, ranges: list[tuple[int, int]]) -> None:
        ordered = sorted(ranges)
        self.starts = [first for first, _ in ordered]
        # reach[i] is the furthest last line among ordered[: i + 1], so one
        # bisect answers covers() even when ranges nest.
        self.reach = list(accumulate((last for _, last in ordered), max))

    def covers(self, line: int) -> bool:
        """Return True if some range has ``first <= line <= last``."""
        index = bisect_right(self.starts, line) - 1
        return index >= 0 and self.reach[index] >= line


@dataclass
class _FileNames:
    """What one file binds, for looking callee names up.

    Scopes are qualnames of the enclosing def or class, ``""`` for module
    level, so ``(scope, name)`` pairs match chunk qualnames split at the last
    dot.

    Attributes:
        file: The file's root-relative path, for error messages.
        has_syntax_error: True if the file's tree has syntax errors; nothing
            else is collected then.
        bindings: scope -> name -> how the scope binds it.
        defs: ``(scope, name)`` -> ids of the chunks defining ``name``
            directly in ``scope``, in source order.
        entries: qualname -> how many ``def`` and ``class`` statements have it.
        wildcard_scopes: Scopes with a ``from m import *``.
        nonlocal_names: Names some ``nonlocal`` statement declares.
        attributes: The keys (``_attribute_keys``) of every attribute some
            target in the file assigns or deletes: ``x`` in ``obj.x = 1``,
            ``for obj.x in``, ``del obj.x``; and ``__dict__`` for an item
            target of an ``__dict__`` attribute (``obj.__dict__[k] = 1``),
            likewise for the rest of ``_NAMESPACE_ITEMS``.
        custom_metaclass: True if some class statement passes ``metaclass=``
            or ``**``.
        class_names: Every name some class body binds.
        class_spans: owner -> first and last line of each class body whose
            calls belong to chunk ``owner`` (``""`` for the module top level).
        class_lines: ``class_spans``, indexed for line lookups.
        chars_left: Walk-only: characters of names the walk may still build
            (``_charge``).
    """

    file: str = ""
    has_syntax_error: bool = False
    bindings: dict[str, dict[str, set[_Kind]]] = field(default_factory=dict)
    defs: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    entries: Counter[str] = field(default_factory=Counter)
    wildcard_scopes: set[str] = field(default_factory=set)
    nonlocal_names: set[str] = field(default_factory=set)
    attributes: set[str] = field(default_factory=set)
    custom_metaclass: bool = False
    class_names: set[str] = field(default_factory=set)
    class_spans: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    class_lines: dict[str, _LineRanges] = field(default_factory=dict)
    chars_left: int = 0


@dataclass(frozen=True)
class _Where:
    """Where the binding walk is, as recording a binding needs it.

    Attributes:
        scope: Qualname of the def or class whose scope a node binds in
            (``""`` for module level).
        owner: Qualname of the def whose chunk the node's calls belong to
            (``""`` for the module top level).
        mangle_prefix: What Python puts before a private ``__x`` in the
            innermost class around the node: ``_`` and the class name without
            its leading underscores. None outside classes, and in a class
            named only with underscores (Python doesn't mangle there).
        is_class: True if ``scope`` is a class body.
    """

    scope: str
    owner: str
    mangle_prefix: str | None
    is_class: bool


# A node to walk, where it is, and whether it is a statement directly in the
# class body ``where.scope`` (not under ``if``, ``try``... in it).
_WalkEntry = tuple[Node, _Where, bool]


@dataclass(frozen=True)
class _ScopeInfo:
    """Where a def or class qualname sits, as bare-name lookups need it.

    Attributes:
        qualname: The qualname; one string object per scope, so the dicts
            ``_resolve_file`` keys by it don't compare long equal strings.
        lookup_parent: The scope a bare name this one doesn't bind is looked
            up in next: the nearest enclosing def, or ``""`` for the module
            (None for the module itself). Enclosing class bodies are skipped,
            as in Python; a qualname that is a def as well as a class isn't
            (``_mark_unsure_scopes`` covers its names).
        is_lookup_scope: False for a class body that isn't also a def, which
            code nested in it doesn't see.
        is_class: True if the qualname is bound as a class.
        in_class: True if some enclosing qualname is a class, so Python
            mangles ``__name`` in this scope.
    """

    qualname: str
    lookup_parent: str | None
    is_lookup_scope: bool
    is_class: bool
    in_class: bool


# The module's entry in a file's ``_ScopeInfo`` cache, where every walk up a
# qualname's enclosing scopes ends.
_MODULE_SCOPE = _ScopeInfo(
    qualname="",
    lookup_parent=None,
    is_lookup_scope=True,
    is_class=False,
    in_class=False,
)


@dataclass(frozen=True)
class _Caller:
    """The chunk making a call, as callee lookups need it.

    Attributes:
        qualname: The chunk's NFKC-normalized qualname, ``""`` for the module
            top level; it keys ``_FileNames.class_lines``, and a method's
            parameters bind in it.
        method_class: Qualname of the class when the chunk is a method.
        in_class: True if the chunk is inside a class (a method, or a def
            nested in one), where Python mangles ``__name``.
    """

    qualname: str
    method_class: str | None
    in_class: bool


class _VisibleBindings:
    """The bindings bare names reach from the scope ``_resolve_by_scope`` is in.

    For each name, the scopes on the current lookup chain that bind it, with
    their depth on the chain, innermost last; likewise the scopes with a
    wildcard import. Entering or leaving a scope costs its own bindings, and
    a lookup costs the same however long the chain is.
    """

    def __init__(self, names: _FileNames) -> None:
        self.names = names
        self.bound: dict[str, list[tuple[int, str, set[_Kind]]]] = {}
        self.wildcards: list[tuple[int, str]] = []

    def enter(self, scope: str, depth: int) -> None:
        """Add the bindings of ``scope``, ``depth`` scopes below the module."""
        for name, kinds in self.names.bindings.get(scope, {}).items():
            self.bound.setdefault(name, []).append((depth, scope, kinds))
        if scope in self.names.wildcard_scopes:
            self.wildcards.append((depth, scope))

    def leave(self, scope: str) -> None:
        """Drop the bindings of ``scope``, the innermost scope entered."""
        for name in self.names.bindings.get(scope, {}):
            self.bound[name].pop()
        if scope in self.names.wildcard_scopes:
            self.wildcards.pop()

    def find(self, name: str) -> tuple[str | None, set[_Kind]]:
        """Return the innermost scope binding ``name``, and how it does.

        A scope with a wildcard import binds as ``import`` every name it
        doesn't otherwise bind. Bound nowhere: ``(None, {"import"})`` for a
        builtin (external, like an import), else ``(None, set())``.
        """
        scopes = self.bound.get(name)
        binding = scopes[-1] if scopes else None
        wildcard = self.wildcards[-1] if self.wildcards else None
        if wildcard is not None and (binding is None or wildcard[0] > binding[0]):
            return wildcard[1], {"import"}
        if binding is not None:
            return binding[1], binding[2]
        return None, ({"import"} if name in _BUILTINS else set())


def resolve(program: Program, trees: dict[str, Tree]) -> None:
    """Fill every call site's ``targets`` and ``status`` in place.

    Statuses: ``resolved`` (one target chunk), ``ambiguous`` (several, at most
    ``MAX_TARGETS``, latest definition first), ``external`` or ``unresolved``
    (no targets); every call in a file with syntax errors is ``unresolved``.
    Adds no diagnostics: the linearizer reports the unresolved calls it walks.

    Args:
        program: The extracted program; its chunks' call sites are updated.
        trees: Each file's tree-sitter tree, keyed by the same root-relative
            path as ``Chunk.file``.

    Raises:
        KeyError: if a chunk's file has no tree in ``trees``.
        UnicodeDecodeError: if a tree's source isn't UTF-8 (``extract``
            rejects such files first).
        ValueError: if a file's qualnames and mangled names add up to far
            more than its size (see ``_NAME_CHARS_PER_BYTE``).
    """
    chunks_by_file: dict[str, list[Chunk]] = {file: [] for file in trees}
    for chunk in program.chunks:
        chunks_by_file[chunk.file].append(chunk)
    files = {
        file: _names_in_file(file, trees[file], chunks)
        for file, chunks in chunks_by_file.items()
    }
    for file, chunks in chunks_by_file.items():
        _resolve_file(chunks, files[file])


def _names_in_file(file: str, tree: Tree, chunks: list[Chunk]) -> _FileNames:
    """Collect what one file binds: its tree's bindings and its def chunks.

    A tree with syntax errors gives just ``has_syntax_error``: Python can't
    run the file, and an ERROR subtree can hide bindings.

    Raises:
        ValueError: if the names the walk builds pass its budget
            (``_names_in_tree``).
    """
    if tree.root_node.has_error:
        return _FileNames(file=file, has_syntax_error=True)
    names = _names_in_tree(tree, file)
    for chunk in chunks:
        parts = _dotted_parts(chunk.name)
        # A qualname with a part a syntax error left out can't be called.
        if chunk.kind == "module_toplevel" or parts is None:
            continue
        scope, name = ".".join(parts[:-1]), parts[-1]
        names.defs.setdefault((scope, name), []).append(chunk.id)
        _bind(names, scope, [name], "def")
    _mark_unsure_scopes(names)
    names.class_names = _class_body_names(names)
    names.class_lines = {
        owner: _LineRanges(spans) for owner, spans in names.class_spans.items()
    }
    return names


def _names_in_tree(tree: Tree, file: str) -> _FileNames:
    """Collect every binding in a file's tree.

    ``defs``, ``class_names`` and ``class_lines`` stay empty; ``_names_in_file``
    fills them. Walks with an explicit stack, since a tree can be deeper than Python's
    recursion limit.

    Raises:
        ValueError: if the qualnames and mangled names it builds pass
            ``_NAME_CHARS_PER_BYTE`` per byte of the file (``_charge``).
    """
    budget = max(_NAME_CHARS_PER_BYTE * tree.root_node.end_byte, _MIN_NAME_CHARS)
    names = _FileNames(file=file, chars_left=budget)
    top = _Where(scope="", owner="", mangle_prefix=None, is_class=False)
    stack: list[_WalkEntry] = [(tree.root_node, top, False)]
    while stack:
        node, where, direct = stack.pop()
        if node.type in _DEFINITIONS:
            children = _enter_definition(node, where, direct, names)
        else:
            _record_bindings(node, where, names)
            # A decorated def or class sits where its decorators' statement does.
            inherited = direct and node.type == "decorated_definition"
            children = [(child, where, inherited) for child in node.named_children]
        stack.extend(reversed(children))
    return names


def _enter_definition(
    node: Node, where: _Where, direct: bool, names: _FileNames
) -> list[_WalkEntry]:
    """Record a def or class statement; return its children to walk.

    Only the body runs in the new scope: defaults, annotations and
    superclasses run in ``where.scope``. A def's parameters (and type
    parameters) bind in its own scope; a class's type parameters bind in its
    owner's, which the class body and its methods both see.

    The name also binds as ``other`` where it may not hold this def or class
    when a call runs: under a compound statement in a class body (``direct``
    is False; if it doesn't run, an inherited name shows through), and at
    module level under a builtin's name (code that runs first sees the
    builtin).

    Raises:
        ValueError: once the names built spend the walk's budget (``_charge``).
    """
    is_def = node.type == "function_definition"
    raw_name = _text(node.child_by_field_name("name"))
    name = _normalize(raw_name) if raw_name.isidentifier() else _MISSING_NAME
    inner = _qualname(where.scope, name)
    _charge(names, len(inner))
    names.entries[inner] += 1
    if name != _MISSING_NAME:
        kind: _Kind = "def" if is_def else "class"
        _bind(names, where.scope, [name], kind, where.mangle_prefix)
        maybe_skipped = where.is_class and not direct
        if maybe_skipped or (not where.scope and name in _BUILTINS):
            _bind(names, where.scope, [name], "other", where.mangle_prefix)
    type_parameters = _target_names(node.child_by_field_name("type_parameters"))
    body = node.child_by_field_name("body")
    header = [(child, where, False) for child in node.named_children if child != body]
    if not is_def:
        _bind(names, where.owner, type_parameters, "other", where.mangle_prefix)
        return [*header, *_enter_class_body(node, inner, where, names)]
    parameters = _target_names(node.child_by_field_name("parameters"))
    _bind(names, inner, parameters, "parameter", where.mangle_prefix)
    _bind(names, inner, type_parameters, "other", where.mangle_prefix)
    inside = replace(where, scope=inner, owner=inner, is_class=False)
    return header if body is None else [*header, (body, inside, False)]


def _enter_class_body(
    node: Node, qualname: str, where: _Where, names: _FileNames
) -> list[_WalkEntry]:
    """Record a class's metaclass and body lines; return its body statements.

    Each statement walks in the class's scope, with the class's name as the
    mangling prefix, marked as directly in the class body.
    """
    if _may_pick_metaclass(node.child_by_field_name("superclasses")):
        names.custom_metaclass = True
    body = node.child_by_field_name("body")
    if body is None:
        return []
    span = (body.start_point.row + 1, body.end_point.row + 1)
    names.class_spans.setdefault(where.owner, []).append(span)
    stripped = qualname.rpartition(".")[2].lstrip("_")
    prefix = f"_{stripped}" if stripped else None
    inside = replace(where, scope=qualname, mangle_prefix=prefix, is_class=True)
    return [(statement, inside, True) for statement in body.named_children]


def _may_pick_metaclass(superclasses: Node | None) -> bool:
    """Return True if a class statement's arguments may pick its metaclass.

    ``metaclass=M`` does, and so may ``**options``.
    """
    arguments = superclasses.named_children if superclasses is not None else []
    return any(
        argument.type == "dictionary_splat"
        or (
            argument.type == "keyword_argument"
            and _normalize(_text(argument.child_by_field_name("name"))) == "metaclass"
        )
        for argument in arguments
    )


def _record_bindings(node: Node, where: _Where, names: _FileNames) -> None:
    """Record the names and attribute names ``node`` itself binds.

    The walk visits ``node``'s children separately, so nested bindings (a
    walrus inside a target, say) are recorded where they occur.
    """
    node_type = node.type
    if node_type in _IMPORTS:
        _record_import(node, where, names)
        return
    if node_type == "aliased_import":
        return  # Recorded with its import statement.
    targets: Iterable[Node | None]
    if node_type in _TARGET_FIELDS:
        targets = [node.child_by_field_name(_TARGET_FIELDS[node_type])]
    elif node_type in _NAME_LISTS:
        targets = [node]
    else:
        # ``with x as y``, ``except E as e``, ``case p as y``.
        targets = _after_as(node)
    bound, attributes = _target_parts(targets)
    prefix = where.mangle_prefix
    if node_type == "global_statement":
        # Module-level names this scope may rebind.
        _bind(names, "", bound, "other", prefix)
    elif node_type == "nonlocal_statement":
        names.nonlocal_names.update(bound, _aliases(names, bound, prefix))
    _bind(names, where.scope, bound, "other", prefix)
    for attribute in attributes:
        names.attributes.update(_attribute_keys(attribute))


def _record_import(node: Node, where: _Where, names: _FileNames) -> None:
    """Record an import statement's names in ``where.scope``.

    ``from m import *`` binds names we can't see: ``_mark_unsure_scopes``
    makes the scope's defs and classes ``other``, and lookups
    (``_VisibleBindings.find``, ``_kinds``) count every name the scope
    doesn't otherwise bind as imported.
    """
    if any(child.type == "wildcard_import" for child in node.named_children):
        names.wildcard_scopes.add(where.scope)
    # Both statements put each imported module or name in a ``name`` field.
    imported = [_imported_name(child) for child in node.children_by_field_name("name")]
    _bind(names, where.scope, imported, "import", where.mangle_prefix)


def _imported_name(imported: Node) -> str:
    """Return the name an import binds: ``y`` for ``x as y``, ``x`` for ``x.y``."""
    if imported.type == "aliased_import":
        return _normalize(_text(imported.child_by_field_name("alias")))
    return _normalize(_text(imported).split(".")[0].strip())


def _target_parts(targets: Iterable[Node | None]) -> tuple[list[str], list[str]]:
    """Return the names and the attribute names binding targets bind.

    Both NFKC-normalized. In ``a, b.x, *c = ...``: names ``a`` and ``c``, and
    attribute ``x`` (``b`` is read, and so are subscripts). In parameters
    ``(x, y=f(), *args: T)``, the parameter names (defaults and annotations
    are read). In a ``case`` pattern, its captures (``Point(x=px)`` binds only
    ``px``; ``Color.RED`` binds nothing). An item of an ``__dict__``
    attribute (``obj.__dict__[k]``) counts as attribute ``__dict__``, and
    likewise for the rest of ``_NAMESPACE_ITEMS``.
    """
    bound: list[str] = []
    attributes: list[str] = []
    stack = [target for target in targets if target is not None]
    while stack:
        node = stack.pop()
        if node.type == "identifier":
            bound.append(_normalize(_text(node)))
        elif node.type == "attribute":
            # ``b.x = ...`` reads ``b`` and rebinds its attribute ``x``.
            attribute = node.child_by_field_name("attribute")
            attributes.append(_normalize(_text(attribute)))
        elif node.type == "subscript" and (namespace := _namespace_item(node)):
            # ``obj.__dict__[k] = ...`` sets an attribute of ``obj`` with no
            # attribute target (``__globals__`` and ``f_locals`` alike).
            attributes.append(namespace)
        else:
            stack.extend(_bound_children(node))
    return bound, attributes


def _namespace_item(subscript: Node) -> str:
    """Return ``__dict__`` for ``obj.__dict__[k]``, etc. (``_NAMESPACE_ITEMS``).

    ``""`` when ``subscript`` isn't an item of such an attribute.
    """
    value = subscript.child_by_field_name("value")
    if value is None or value.type != "attribute":
        return ""
    attribute = _normalize(_text(value.child_by_field_name("attribute")))
    return attribute if attribute in _NAMESPACE_ITEMS else ""


def _target_names(target: Node | None) -> list[str]:
    """Return the names ``target`` binds (see ``_target_parts``)."""
    return _target_parts([target])[0]


def _bound_children(node: Node) -> list[Node]:
    """Return the children of a part of a binding target that may bind names."""
    children = node.named_children
    if node.type in _NOT_TARGETS or (node.type == "dotted_name" and len(children) > 1):
        return []
    if node.type in _BINDS_FIRST:
        return children[:1]
    if node.type in _READS_FIRST:
        return children[1:]
    return children


def _after_as(node: Node) -> list[Node]:
    """Return ``node``'s children right after an ``as``: ``y`` in ``x as y``."""
    return [child for before, child in pairwise(node.children) if before.type == "as"]


def _attribute_keys(name: str) -> set[str]:
    """Return the keys ``_FileNames.attributes`` files attribute ``name`` under.

    ``name`` itself, and its private tail from the last ``__`` when there is
    one: written in class ``C``, ``obj.__x`` is ``obj._C__x``, so ``__x`` and
    every ``_Class__x`` may be one attribute, and all end in that tail. No
    class name is needed, so no long mangled name is ever built.
    """
    index = name.rfind("__")
    tail = name[index:] if index >= 0 else ""
    return {name, tail} if _is_private(tail) else {name}


def _mark_unsure_scopes(names: _FileNames) -> None:
    """Add ``other`` to the bindings no single def or class can be vouched for.

    - Every name bound in a qualname entered more than once (repeated
      ``def``/``class`` statements, or a class and a def of one name): the
      copies share one table, so a lookup can't tell whose binding it found.
    - Every def and class in a scope with a wildcard import: before the def
      runs, or after the import, the name holds what the import brought in.
    - Function-level names some ``nonlocal`` declares: a nested def may rebind
      them. All non-module scopes count, which needs no scope analysis.
    """
    for scope, scope_names in names.bindings.items():
        repeated = names.entries[scope] > 1
        wildcard = scope in names.wildcard_scopes
        for name, kinds in scope_names.items():
            replaceable = wildcard and ("def" in kinds or "class" in kinds)
            rebindable = bool(scope) and name in names.nonlocal_names
            if repeated or replaceable or rebindable:
                kinds.add("other")


def _class_body_names(names: _FileNames) -> set[str]:
    """Return every name some class body in the file binds.

    One set for the whole file, so the class-body check in ``_resolve_callee``
    stays linear in the file's size however classes nest and repeat.
    """
    return {
        name
        for scope, scope_names in names.bindings.items()
        if "class" in _defined_as(names, scope)
        for name in scope_names
    }


def _resolve_file(chunks: list[Chunk], names: _FileNames) -> None:
    """Resolve every call site in one file's chunks against its ``names``."""
    infos = {"": _MODULE_SCOPE}
    callers: dict[str, list[tuple[Chunk, _Caller]]] = {}
    for chunk in chunks:
        caller = _caller(chunk, names, infos)
        if caller is not None:
            callers.setdefault(caller.qualname, []).append((chunk, caller))
            continue
        # A syntax error left a part of the chunk's name out, so its scopes
        # can't be told apart from others (and it is no call's target).
        for site in iter_call_sites(chunk.body):
            site.status, site.targets = _no_targets(external=False)
    _resolve_by_scope(callers, _lookup_children(infos), names)


def _caller(
    chunk: Chunk, names: _FileNames, infos: dict[str, _ScopeInfo]
) -> _Caller | None:
    """Return what callee lookups need about ``chunk`` (None if a part is unnamed).

    Adds the scopes of its qualname to ``infos`` (``_scope_info``).
    """
    if chunk.kind == "module_toplevel":
        # Top-level code sees module scope; _resolve_callee checks whether a
        # call is in a class body, which sees the class's names first.
        return _Caller(qualname="", method_class=None, in_class=False)
    parts = _dotted_parts(chunk.name)
    if parts is None:
        return None
    info = _scope_info(".".join(parts), names, infos)
    return _Caller(
        qualname=info.qualname,
        method_class=".".join(parts[:-1]) if chunk.kind == "method" else None,
        in_class=info.in_class,
    )


def _scope_info(
    qualname: str, names: _FileNames, infos: dict[str, _ScopeInfo]
) -> _ScopeInfo:
    """Return the ``_ScopeInfo`` of def or class ``qualname``, cached in ``infos``.

    Each scope's info comes from its parent's and is built once, so a file's
    scopes cost their qualnames' total length however deep they nest (the
    walk's name budget bounds that). ``infos`` must hold ``_MODULE_SCOPE``.
    """
    # Climb to the nearest scope already known (the module at worst), then
    # build each missing one from its parent's on the way back down.
    missing = [qualname]
    while missing[-1] not in infos:
        missing.append(missing[-1].rpartition(".")[0])
    parent = infos[missing.pop()]
    for scope in reversed(missing):
        kinds = _defined_as(names, scope)
        is_class = "class" in kinds
        info = _ScopeInfo(
            qualname=scope,
            lookup_parent=(
                parent.qualname if parent.is_lookup_scope else parent.lookup_parent
            ),
            is_lookup_scope=not is_class or "def" in kinds,
            is_class=is_class,
            in_class=parent.is_class or parent.in_class,
        )
        infos[scope] = info
        parent = info
    return parent


def _lookup_children(infos: dict[str, _ScopeInfo]) -> dict[str, list[str]]:
    """Return scope -> the scopes that look bare names up in it next.

    Class bodies that aren't also defs are left out: no chunk's lookups
    start or pass there.
    """
    children: dict[str, list[str]] = {}
    for info in infos.values():
        if info.lookup_parent is not None and info.is_lookup_scope:
            children.setdefault(info.lookup_parent, []).append(info.qualname)
    return children


def _resolve_by_scope(
    callers: dict[str, list[tuple[Chunk, _Caller]]],
    children: dict[str, list[str]],
    names: _FileNames,
) -> None:
    """Resolve each chunk's calls from its own scope, visiting scopes from the module.

    Depth first along ``children``, so the scopes entered at any moment are
    exactly one chunk's lookup chain: the def's own scope, each enclosing def,
    then the module. An explicit stack, since scopes can nest past Python's
    recursion limit; each scope goes back on it marked ``leaving`` to drop
    its bindings once everything inside it is done.
    """
    visible = _VisibleBindings(names)
    stack: list[tuple[str, int, bool]] = [("", 0, False)]
    while stack:
        scope, depth, leaving = stack.pop()
        if leaving:
            visible.leave(scope)
            continue
        visible.enter(scope, depth)
        for chunk, caller in callers.get(scope, []):
            for site in iter_call_sites(chunk.body):
                outcome = _resolve_callee(site, names, caller, visible)
                site.status, site.targets = outcome
        stack.append((scope, depth, True))
        stack.extend((child, depth + 1, False) for child in children.get(scope, []))


def _defined_as(names: _FileNames, qualname: str) -> set[_Kind]:
    """Return how ``qualname``'s last name is bound in the scope around it."""
    scope, _, name = qualname.rpartition(".")
    return names.bindings.get(scope, {}).get(name, set())


def _resolve_callee(
    site: CallSite, names: _FileNames, caller: _Caller, visible: _VisibleBindings
) -> tuple[CallStatus, list[str]]:
    """Return the status and target chunk ids for one call site's callee.

    Args:
        site: The call; its ``callee_text`` (e.g. ``self.save``) and ``line``
            are read.
        names: What the calling chunk's file binds.
        caller: The chunk making the call.
        visible: The bindings bare names reach from ``caller``'s scope.
    """
    if names.has_syntax_error:
        return _no_targets(external=False)
    parts = _dotted_parts(site.callee_text)
    if parts is None:
        literal = _is_literal(site.callee_text)
        return _no_targets(external=literal and not _code_swapped(names))
    root = parts[0]
    in_class_body = _in_class_body(names, caller.qualname, site.line)
    if _is_private(root) and (caller.in_class or in_class_body):
        # Python reads ``__x`` inside class C as ``_C__x``.
        return _no_targets(external=False)
    if in_class_body and root in names.class_names:
        # A class body sees its class's own names before the chunk's scopes.
        return _no_targets(external=False)
    if (
        caller.method_class is not None
        and len(parts) == 2
        and root in _SELF_NAMES
        # The method's own parameter, never rebound in it.
        and _kinds(names, caller.qualname, root) == {"parameter"}
    ):
        # Methods inherited from a base class wait for M6.
        return _method_call(names, caller.method_class, parts[1])
    scope, kinds = visible.find(root)
    if _may_be_patched(names, parts, scope, kinds):
        return _no_targets(external=False)
    if len(parts) > 1 or scope is None or kinds == {"import"}:
        # Dotted: an attribute of anything but an import or a builtin needs
        # attribute types (M6). Bare: an import, a builtin, or bound nowhere.
        imported = kinds == {"import"}
        return _no_targets(external=imported and not _code_swapped(names))
    if kinds == {"class"}:
        return _class_call(_qualname(scope, root), names)
    return _def_targets(names, scope, root)


def _may_be_patched(
    names: _FileNames, parts: list[str], scope: str | None, kinds: set[_Kind]
) -> bool:
    """Return True if an attribute target may have replaced what a callee finds.

    A target doesn't show which object it writes to, so any target whose key
    (``_attribute_keys``) matches counts. ``parts`` is the dotted callee, and
    ``scope`` and ``kinds`` are where and how its root was found. The root
    counts when found at module level (``import app as me; me.helper = x``
    rebinds module-level ``helper``), as a builtin (``builtins.len = x``) or
    as an import (whose module any code may patch); every later part counts
    (``os.system = x`` for ``os.system()``). When the root counts, so does any
    namespace item write (``_NAMESPACE_ITEMS``). A root found in a
    function scope may sit in a closure cell, which any ``_CELL_HOOKS``
    target or item write may have rebound.
    """
    root_patchable = scope == "" or "import" in kinds
    if root_patchable and not _NAMESPACE_ITEMS.isdisjoint(names.attributes):
        return True
    if scope and not _CELL_HOOKS.isdisjoint(names.attributes):
        return True
    patchable = parts if root_patchable else parts[1:]
    return any(_attribute_keys(part) & names.attributes for part in patchable)


def _in_class_body(names: _FileNames, owner: str, line: int) -> bool:
    """Return True if ``line`` of chunk ``owner`` is directly in a class body.

    Calls in a class's methods belong to other chunks, so only the body's own
    statements count.
    """
    ranges = names.class_lines.get(owner)
    return ranges is not None and ranges.covers(line)


def _kinds(names: _FileNames, scope: str, name: str) -> set[_Kind]:
    """Return how ``scope`` binds ``name``, empty if it doesn't.

    Under a wildcard import, a name ``scope`` doesn't otherwise bind counts as
    imported.
    """
    kinds = names.bindings.get(scope, {}).get(name)
    if kinds:
        return kinds
    return {"import"} if scope in names.wildcard_scopes else set()


def _method_call(
    names: _FileNames, class_qualname: str, method: str
) -> tuple[CallStatus, list[str]]:
    """Status and targets for ``self.method()``/``cls.method()`` in a method.

    The defs of ``method`` in the method's class ``class_qualname``, unless
    something in the file may change what the lookup finds: a target
    assigning or deleting an attribute ``method`` on any object (or a
    spelling mangling may equate with it, see ``_attribute_keys``) or one of
    ``_INSTANCE_HOOKS``, or a class binding ``__getattribute__``.
    """
    replaced = _attribute_keys(method) | _INSTANCE_HOOKS
    if replaced & names.attributes or "__getattribute__" in names.class_names:
        return _no_targets(external=False)
    return _def_targets(names, class_qualname, method)


def _class_call(class_qualname: str, names: _FileNames) -> tuple[CallStatus, list[str]]:
    """Status and targets for calling a class: its own ``__init__``.

    ``unresolved`` when other code may run first and skip it (a metaclass
    ``__call__``, when some class statement in the file passes ``metaclass=``
    or ``**``; one of ``_CONSTRUCTOR_OVERRIDES`` any class in the file binds,
    its base or metaclass perhaps), when a target assigns one of
    ``_CONSTRUCTOR_HOOKS`` on any object, or when the class binds
    ``__init__`` other than by ``def``. With no ``__init__`` of its own:
    ``external`` (no init code to show) until M6 adds inherited ones, but
    ``unresolved`` when some class in the file binds one of ``_INIT_RUNNERS``
    (its base's ``__init__``, or a dataclass's ``__post_init__``, may run) or
    the file assigns a ``__code__``.
    """
    replaced = not _CONSTRUCTOR_HOOKS.isdisjoint(names.attributes)
    overridden = not _CONSTRUCTOR_OVERRIDES.isdisjoint(names.class_names)
    if replaced or overridden or names.custom_metaclass:
        return _no_targets(external=False)
    if not _kinds(names, class_qualname, "__init__"):
        inherited = not _INIT_RUNNERS.isdisjoint(names.class_names)
        return _no_targets(external=not inherited and not _code_swapped(names))
    return _def_targets(names, class_qualname, "__init__")


def _def_targets(
    names: _FileNames, scope: str, name: str
) -> tuple[CallStatus, list[str]]:
    """Status and targets for ``name`` in ``scope``: its defs, if only defs bind it.

    Unresolved, with no targets, in a file that assigns some ``__code__``
    (``_code_swapped``), or when more than ``MAX_TARGETS`` defs bind it.
    """
    chunk_ids = names.defs.get((scope, name))
    swapped = _code_swapped(names)
    if swapped or not chunk_ids or len(chunk_ids) > MAX_TARGETS:
        return _no_targets(external=False)
    if _kinds(names, scope, name) != {"def"}:
        return _no_targets(external=False)
    status: CallStatus = "resolved" if len(chunk_ids) == 1 else "ambiguous"
    # Latest first, so the default page is the def an unconditional
    # redefinition leaves in place. A copy, so call sites never share a list.
    return status, chunk_ids[::-1]


def _code_swapped(names: _FileNames) -> bool:
    """Return True if the file assigns or deletes some ``__code__``.

    That swaps a function's body without rebinding a name, through any alias
    and in any module the function comes from, so neither a def's body nor
    an ``external`` badge can be vouched for.
    """
    return _CODE_HOOK in names.attributes


def _no_targets(*, external: bool) -> tuple[CallStatus, list[str]]:
    """Status ``external`` or ``unresolved``, with no targets."""
    status: CallStatus = "external" if external else "unresolved"
    return status, []


def _bind(
    names: _FileNames,
    scope: str,
    bound: list[str],
    kind: _Kind,
    mangle_prefix: str | None = None,
) -> None:
    """Record that ``scope`` binds each name in ``bound`` as ``kind``.

    Inside a class (``mangle_prefix`` set), each name's other spelling
    (``_aliases``) is bound too, as ``other``: it is the same name to Python,
    and no def under that spelling vouches for it.

    Raises:
        ValueError: once the names built spend the walk's budget (``_charge``).
    """
    scope_names = names.bindings.setdefault(scope, {})
    for name in bound:
        scope_names.setdefault(name, set()).add(kind)
    for alias in _aliases(names, bound, mangle_prefix):
        scope_names.setdefault(alias, set()).add("other")


def _aliases(
    names: _FileNames, bound: list[str], mangle_prefix: str | None
) -> list[str]:
    """Return the other spelling Python gives each name in ``bound`` that has one.

    In a class whose ``mangle_prefix`` is ``_C``, Python stores a private
    ``__x`` as ``_C__x``, so code there reaches one name through either
    spelling. Outside classes (``mangle_prefix`` None) nothing is mangled.

    Raises:
        ValueError: once the mangled names built spend the walk's budget.
    """
    aliases: list[str] = []
    if mangle_prefix is None:
        return aliases
    for name in bound:
        unmangled = name.removeprefix(mangle_prefix)
        if _is_private(name):
            # Charged before it's built: the prefix can be as long as the file.
            _charge(names, len(mangle_prefix) + len(name))
            aliases.append(mangle_prefix + name)
        elif unmangled != name and _is_private(unmangled):
            aliases.append(unmangled)
    return aliases


def _charge(names: _FileNames, chars: int) -> None:
    """Spend ``chars`` of the walk's budget for building names.

    Raises:
        ValueError: once the walk has built more than its budget allows.
    """
    names.chars_left -= chars
    if names.chars_left < 0:
        raise ValueError(
            f"{names.file}: qualnames and mangled names too long to index"
        )


def _qualname(scope: str, name: str) -> str:
    """Return the qualname of ``name`` bound in ``scope`` (``""``: module)."""
    return f"{scope}.{name}" if scope else name


def _is_private(name: str) -> bool:
    """Return True for a name Python mangles in a class: ``__x``, not ``__x__``."""
    return name.startswith("__") and not name.endswith("__")


def _dotted_parts(callee_text: str) -> list[str] | None:
    """Split a dotted name like ``os.path.join`` into NFKC-normalized parts.

    Also splits chunk qualnames. Whitespace around the dots is allowed, as in
    Python. Validity is checked before normalizing, as Python's tokenizer
    does.

    Returns:
        The parts, or None if ``callee_text`` isn't a dotted identifier chain.
    """
    parts = [part.strip() for part in callee_text.split(".")]
    if not all(part.isidentifier() for part in parts):
        return None
    return [_normalize(part) for part in parts]


def _is_literal(callee_text: str) -> bool:
    """Return True for a method right on a string, bytes or number literal.

    ``", ".join`` and ``1 .bit_length`` count. A longer chain doesn't: from a
    literal, ``"".maketrans(d)[97]`` or ``"".__class__.__mro__[1]`` can reach
    project code. Neither do displays (``[f][0]``, ``(f,)[0]``).
    """
    receiver, dot, method = callee_text.rpartition(".")
    receiver = receiver.strip()
    is_literal = bool(
        _STRING_LITERAL.fullmatch(receiver) or _NUMBER_LITERAL.fullmatch(receiver)
    )
    return bool(dot) and method.strip().isidentifier() and is_literal


def _normalize(identifier: str) -> str:
    """NFKC-normalize ``identifier``, as Python does when it reads a name."""
    return unicodedata.normalize("NFKC", identifier)
