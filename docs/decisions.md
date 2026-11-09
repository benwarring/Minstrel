# Decisions

Choices made during the build that `docs/PLAN.md` doesn't cover, one line of reasoning each.

## Planning (2026-10-23)

- No `core/graph.py`: the linearizer looks chunks up by id, and "never reached" is whatever an all-arms walk doesn't place.
- No `core/entries.py`: entry ranking is Python-specific (`__main__` guard, `main`), so it lives in the adapter's `entry_candidates`.
- `Story` gains `diagnostics`; `Page` gains `via_arm` and `calls` (per call site: `call_site_id`, `target_page`, `note`, `conditional`, `maybe_interrupted`), because the viewer and golden fixtures need per-call links and badges.
- `ir.iter_call_sites(block)` is the one walker for nested arm bodies, so resolver, linearizer and renderers can't disagree on traversal.
- `extract(tree, file)` receives the root-relative POSIX path and derives the module name from it, so ids stay stable when M5 adds packages.
- The Python adapter keeps its parsed trees, so `resolve(program)` reads imports, classes and assignments without language-specific IR fields.
- An ambiguous call site is a choice point keyed by its own id (`path[call_site_id] = chunk_id`, default first target), supported by the linearizer from M2 so M6 only touches the resolver.
- The linearizer is written once in M2 with path selection, since the IR already defines branch points; M3 exercises it end to end.
- `load_program` lives in `cli.py`, the only module allowed to import adapters besides the adapters themselves.
- Viewer JS is split: `linearize.js` is pure and Node-testable via `vm`; `viewer.js` holds DOM code. Keeps "no extra JavaScript dependencies".
- Unreadable or undecodable files are skipped with a diagnostic from M2, not M7.
- Golden fixtures are hand-written from the spec after each implementation passes review, never generated from the code's own output.

## IR details (M1)

- Block nodes serialize with a discriminator: `"node": "call" | "branch" | "terminator"`, so JSON round-trips without guessing types.
- `Diagnostic` is `code`, `message`, `file`, `line` (last two optional); golden fixtures compare `code` and location, not wording.
- `Arm.partial` is a block that runs before `body` and may be interrupted (the try body in an `except` arm): its calls are marked `maybe_interrupted` and its terminators ignored. Neutral enough for Java/C++ try.
- Try arms: "no exception" body = try body + else + finally; each `except` arm has partial = try body, body = handler + finally. One call site can appear in several arms under one id, so `iter_call_sites` and `Page.calls` dedupe by id.
- Loop arms: "body runs" body = loop body + loop else; "body skipped" body = loop else. `break`/`continue` end the loop arm's walk.
- Walk status: `return`/`raise` stop the rest of the chunk; `break`/`continue` are absorbed by the nearest enclosing loop branch point. In "show everything", the walk continues after a branch point unless every arm stopped.
- Known gaps (v1): a `finally` after a `return` inside the try body isn't walked; a loop `else` after a `continue` path isn't walked.
- Ids: chunk `<module>:<qualname>`, module top level `<module>:<module>` with the literal qualname `<module>` (e.g. `app:<module>`, which no Python name can collide with), a repeated qualname in one module gets `~2`, `~3`; `<chunk>#callN` counts from 1 in evaluation order (a call's receiver and arguments before the call, so `f(g())` numbers `g` first); `<chunk>#branchN` counts from 1 in source order; arms `<branch>.armN`.
- A `module_toplevel` chunk exists only when the module has call sites outside function bodies (top level, class bodies, main guard). `never_reached` excludes module top levels, since they run on import, not by call.
- Decorator and default-argument calls are not call sites in v1 (they run at definition time).
- `Page.active_ranges` = selected arm spans; `inactive_ranges` = unselected arm spans plus, after a `return`/`raise`, the rest of the chunk. Both empty in "show everything". Ranges are `[start_line, end_line]`, inclusive, file line numbers.
- `Page.breadcrumb` is chunk ids from the entry to this page; renderers show names.
- `Story` = `entry`, `mode`, `pages`, `not_on_path` (order the all-arms walk first reaches them), `never_reached` (program chunk order), `diagnostics` (one per unresolved call site walked, and depth-limit hits).
- Default entries: every candidate in the best tier present (main guard > function `main` > top level with calls); `--entry` overrides.
- `Program.entry_candidates` is `list[list[str]]`: tiers of chunk ids, best tier first, so the default (whole first tier) is readable from the IR.
- Conventions: `CallSite.col` is 0-based; `Page.index` is 1-based and the entry page has depth 0; `breadcrumb` includes the page's own chunk; note `recursive` means the target is on the current call stack (in the breadcrumb).

- `LanguageAdapter` gets a sixth member, `language: str` (e.g. `"python"`), because `Program.language` has to come from somewhere neutral.
- Chunk order in `Program.chunks`: per file, the module top level (if any) first, then defs in source order (pre-order). `start_line` of a decorated def is its first decorator's line, so decorators show in the source.
- `CallSite.line`/`col` point at the callee's name token (`k` in `h(1).k()`), so each call has a distinct spot for its viewer link.
- `Chunk.name` is the qualname without the `~N` suffix (`C.meth`, `outer.inner`); `source` is the chunk's lines joined with `
`, no trailing newline, CRLF normalized.
- Known gap (v1): the message in `assert x, msg()` isn't marked conditional.
- `CallSite.col` counts characters (code points) from the line start, not bytes, so renderers can index the line string directly.
- In a chained comparison `a() < b() < c()`, operands after the second are `conditional` (Python short-circuits them).
- Before parsing, a lone `\r` becomes `\n` (Python treats it as a line break; tree-sitter doesn't, which could hide calls), and files declaring a non-UTF-8 `# coding:` are rejected with `ValueError`.

## M0 status (2026-10-23)

- Built: `pyproject.toml` (hatchling, sdist allowlist so stray root files like `.env` never ship), `cli.py` stubs for all four commands, `tests/test_cli.py` (6 passing), `.venv` with the package installed editable.
- Deferred: `.gitignore` (awaiting the user's call).

## M2 contracts (resolver, linearizer, text)

- Columns and line text: `CallSite.col` indexes the lines of `Chunk.source` (whitespace before a file's first token, e.g. a form feed, isn't part of it). Renderers use `Chunk.source`, never re-read files.
- Resolver (M2, single module): bare names follow Python scoping — the chunk's own nested defs, then enclosing *function* scopes (class bodies are skipped, as in Python), then module-level defs, then imported names (external), then builtins (external), else unresolved. Several defs of one name in one scope make the call `ambiguous` (latest definition first), up to `MAX_TARGETS` (16); more candidates make it `unresolved`.
- Resolver: `self.m()`/`cls.m()` in a method resolve to `m` in the same class (inheritance waits for M6). `Foo()` for a class in scope resolves to `Foo.__init__`; a class with no `__init__` of its own is `external` (no init code to show) until M6 adds inherited ones, except that it is `unresolved` when any class in the file binds `__init__` (a base's in-file `__init__` may run; M2 fix round 3). A dotted callee whose root is imported or a builtin is `external`; a method called directly on a string, bytes or number literal (`", ".join`) is `external` (a longer chain starting with a literal is `unresolved`, since `"".__class__...` can reach project code); anything else (`obj.m`, `x[0]`, `f()()`, `super().m`) is `unresolved` until M5/M6.
- Resolver: identifiers are NFKC-normalized before matching, as Python does. Class, import and other bindings come from the tree, per scope; defs come from the chunks.
- Resolver binding rule (M2, from resolve.py security r2): lookup stops at the first scope in the chain that binds the name in *any* way. If every binding there is a `def` the call is resolved (one) or ambiguous (several); only a class: `__init__` as above; only imports: external. Any other mix, or any other binding kind there (assignment, augmented assignment, parameter, `for`/`with`/`except`/walrus/`match` target, `del`, `global`/`nonlocal` declaration, a lambda parameter or comprehension variable counted in the enclosing function), makes it `unresolved`. A wildcard import makes every same-scope def and class `unresolved` and otherwise-unknown names in that scope `external`. Wrong links are worse than missing links.
- Resolver: a bare `__name` (two leading underscores, not a dunder) inside a class is name-mangled by Python, so it's `unresolved`; `Foo()` is `unresolved` when `Foo` defines `__new__`; calls directly in a class body that the class itself binds are `unresolved`; chunks/classes with an empty name part (syntax errors) are ignored; callees starting with a list/dict/set/tuple display are `unresolved`.
- Resolver threat model (from resolve.py security r3): a `resolved` link means no binding visible in the source contradicts it. Rebinding written in the source must never be contradicted by a `resolved` link. Dynamic rebinding the source text can't show (`setattr`, `globals()`/`vars()`, `exec`, `sys.modules[...]`, metaclasses or class decorators rewriting a class) is a documented v1 gap: `resolved` is a static best guess, not a guarantee.
- Resolver rules added in M2 fix round 2: a file whose tree has syntax errors gets every call `unresolved` (Python can't run it, and an ERROR subtree can hide bindings); inside a class, a private `__x` binding also binds the mangled `_Class__x` as other (at module scope for `global`); attribute assignment/`del` targets anywhere in the file count: `self.b()`/`cls.b()` is `unresolved` if any `X.b` is assigned, `Foo()` if `__init__`/`__new__` is, every `self.m()` if `__class__`/`__dict__` is, a bare `a()` if `a.__code__` is; a qualname entered more than once (repeated `def`/`class`) makes every binding inside it other; a def or class that isn't directly in its class body (under `if`/`try`/`with`/`for`/`match`) binds as other, and a class binding `__getattribute__` makes `self.m()` `unresolved`; at module scope a def or class named like a builtin is `unresolved`, and in a scope with a wildcard import every def and class is other; `self.m()`/`cls.m()` require `self`/`cls` to be a parameter of the method itself; `Foo()` is `unresolved` when the class statement passes `metaclass=` or `**` (either may pick a metaclass), when any class in the file binds `__new__`, or when the file assigns an attribute `__init__`, `__new__`, `__call__` or `__class__`. Index structures stay linear in file size.
- Resolver rules added in M2 fix round 3: an attribute target can rebind a name without naming it, so a call is `unresolved` when any assigned or deleted attribute key matches a later part of its dotted callee, or its root when the root is found at module scope, as a builtin or as an import (`builtins.len = x`, `os.system = x`, `import app as me; me.helper = x`); a file assigning any `__code__` links no def and marks no call `external`; `obj.__dict__[k] = ...`/`del obj.__dict__[k]` count as assigning `__dict__`; `Foo()` is `unresolved` when any class in the file binds `__call__` or the file assigns an attribute `__call__` (a metaclass `__call__` may run without `metaclass=`). Cost: a file defining any callable class loses its `Foo()` links in v1.
- Resolver rules added in M2 fix round 4 (user-approved extra round): a call with more than `MAX_TARGETS` (16) candidate defs is `unresolved`, since each call site holds its own target list and K defs called M times would otherwise build K*M entries; a name found in a function scope may sit in a closure cell, so any `cell_contents` attribute target or `f_locals` item write in the file makes such calls `unresolved`; an item write to any `__dict__`, `__globals__` or `f_locals` makes calls whose root is module-level, builtin or imported `unresolved` (so a file with `self.__dict__[k] = v` loses its module-level links); calling a class with no `__init__` of its own is `unresolved` when any class in the file binds `__init__` or `__post_init__` (a dataclass's generated `__init__` calls `__post_init__`). `__builtins__["len"] = x` stays in the documented `globals()`/`vars()` gap.
- Resolver performance: each scope's lookup parent and in-class flag are computed once from its parent's, and calls resolve in a depth-first walk of def scopes with per-name binding stacks, so with the `MAX_TARGETS` cap, resolution is linear in file size however deeply scopes nest (a crafted 1 MB file went from 36 s to 1.8 s).
- Ambiguous targets list the latest definition first, so the default page is the body an unconditional redefinition leaves in place.
- Known gap (v1 resolver): `self.m()` links by receiver name (direct `self = ...` is tracked, but `self` swapped through a closure cell or `f_locals`, or a method replaced via a class-body `f_locals` write, isn't; carried to M6); a module-level `__builtins__ = {...}` rebinding leaves builtin calls `external`; `self.__m()` is looked up unmangled; `self.m()` inside a function nested in a method stays unresolved; `Foo.m()` on a class in scope is unresolved until M6.
- Python adapter (M2): each file is resolved as its own single-file `Program` (same chunk objects), so a file whose names blow `resolve`'s budget only leaves its own calls `unresolved`, with a `resolve_skipped` diagnostic; M5 revisits this for cross-file imports.
- Python adapter entry tiers: a main guard is exactly one top-level `if` whose condition is `__name__ == "__main__"` (either order, no parentheses; the name compared after NFKC as Python does, the string a plain unprefixed quoted literal), and only in a file with no syntax errors, so a broken file can't claim tier 1 with a decoy guard; anything else falls to the next tier it qualifies for. Tier 2 matches the id `<module>:main` only, so a repeated `def main` (`main~2`) isn't a candidate.
- Python adapter resolve: on a name-budget `ValueError` the adapter resets every call site of that file to unresolved/no targets before adding `resolve_skipped`, so the diagnostic holds even if `resolve` raised mid-pass; `UnicodeDecodeError` (a `ValueError` subclass) propagates.
- Linearizer: `linearize(program, entry, selection=None, mode="follow", max_depth=50) -> Story`. An invalid selection entry falls back to the default. Each call site is processed once per page. In "show everything", an ambiguous call walks every candidate and records one `PageCall` per candidate.
- Linearizer: `via_arm` is the label of the innermost arm being walked when the page was placed ("show everything" only). A placed callee gets the next page number before its own calls are walked (depth-first). Placing beyond `max_depth` records `depth_limit` and a diagnostic instead.
- Linearizer: walk statuses propagate as `return` (return/raise) or `break` (break/continue); a loop branch point absorbs `break`. In "show everything", the walk stops after a branch point only if every arm stopped, propagating `break` if any arm broke, else `return`.
- `via_arm` is the innermost arm around the placing call site in the caller's own chunk; ambiguous-call candidates aren't arms and don't set it.
- The linearizer never reads `Arm.terminates` (display only); walk statuses come from `Terminator` nodes, so extractors must emit return/raise/break/continue as Terminator nodes in arm bodies. Inside a `partial`, every terminator is ignored, nested ones too.
- The appendix (all-arms) walk uses the same `max_depth` and its diagnostics are dropped; only the requested walk's diagnostics reach the Story.
- Known gap (v1 linearizer): a chunk reachable only beyond `max_depth` is listed in `never_reached`, not `not_on_path`. Fix if real projects hit it: let the appendix walk count depth-limited targets as reached.
- Text renderer and CLI escape control and format characters (Unicode categories Cc, Cf, Cs, Zl and Zp: ESC, bidi controls, lone surrogates, line/paragraph separators) via `render.text.escape_control_chars` before printing, so a crafted file can't drive the terminal or reorder what's shown. Output is ASCII-safe punctuation (`->`, not arrows).

## M2 CLI and golden fixtures

- `load_program(path)`: a file is read alone (root = its parent); a directory is walked in sorted root-relative POSIX order, skipping symlinks (files and dirs), dot-directories, `__pycache__` and any directory holding `pyvenv.cfg` (a virtualenv), so a scan never leaves the root or reads installed packages.
- Run-wide caps: at most 10,000 files and 50 MB of source per run; files past either cap are skipped with one `file_limit` diagnostic. A 10,000-line project is far below both.
- A file that fails `parse`/`extract` (`OSError`, `ValueError`, incl. `UnicodeDecodeError`) is skipped with a `file_skipped` diagnostic; a file whose chunk ids collide with an already loaded file (e.g. `pkg.py` and `pkg/__init__.py` both give `pkg:<module>`) is skipped whole with `duplicate_module`, first in sorted order wins.
- `--entry NAME` matches a chunk id exactly, else a unique `Chunk.name`; no match or several matches is an error that lists the candidates. Without `--entry`, `print` prints one story per id in the first tier, in order, separated by a blank line; no candidates is an error.
- `--path FILE`: at most 1 MB, must be a JSON object of string to string, else a usage error (exit 2). It applies to every story printed.
- `entries` prints every tier, one candidate per line with its tier number and file:line, the default tier marked, so non-default candidates stay visible.
- Program diagnostics (`file_skipped`, `file_limit`, `duplicate_module`, `resolve_skipped`) go to stderr as warnings; story diagnostics stay in the outline. Every analyzed-code string printed (ids, names, file names, messages) goes through `escape_control_chars`; stdout/stderr are written with `errors="replace"` so a cp1252 console can't crash the run.
- Exit codes: 0 success (diagnostics included), 1 for a failed run (no files read, no entry, unknown `--entry`), 2 for usage errors (Typer's default).
- CLI details (writer, M2): a run that reads no files exits 1 for every command, after printing the skip warnings; a single file named on the command line is read whatever its extension (the walk only reads the adapter's extensions); an unlistable directory or an entry resolving outside the root (e.g. a Windows junction) gets `file_skipped`, while symlinks are skipped silently; `file_skipped` messages never contain absolute paths, since messages are stored in the IR and later the HTML; `--path` is validated before loading, so usage errors come first; stdout/stderr keep their encoding with `errors="replace"` rather than switching to UTF-8, which would garble non-ASCII letters on a cp1252 console; `load_program` lets `UnicodeDecodeError` from resolve propagate and the command wrapper turns it into exit 1.
- Golden fixture format: `expected.json` is `{"stories": [...]}`, one story per default entry in tier order; an optional `path.json` selection applies to every story. A story is `{"entry", "pages": [{"chunk", "depth"}], "references": [{"call", "note", "target_page"}], "not_on_path", "never_reached", "diagnostics": [{"code", "file", "line"}]}`; `references` lists every `PageCall` whose note isn't `placed`, in page then call order. Both linearizers are compared against this.

## M1 status (2026-10-23)

- Built: `ir.py` (IR, JSON round-trip, `iter_call_sites`), `adapters/base.py` (`LanguageAdapter`), `adapters/python/extract.py` (chunks, call sites in evaluation order, conditional flags, character columns, output budget, 1 MB / regular-file read limit, lone-`\r` and encoding handling), fixture inputs for the ten M1–M2 cases, 141 tests passing.
- Environment: tree-sitter pinned `<0.26` (0.26.0 segfaults in GC on Python 3.14 after many `Node.start_point` reads).
- Deferred to M3: reject NUL bytes (confirmed call hiding), NFKC def names. Open: no tree-sitter parse timeout (crafted 1 MB inputs measured at 0.6–1.7 s).

## M2 status (2026-11-09)

- Built: `adapters/python/resolve.py` (single-module scoping, `self.m()`, `Foo()` -> `__init__`, external/unresolved marking), `core/linearize.py` (path selection, both modes, appendices, depth limit), `render/text.py`, `cli.py` `entries`/`print`/`ir` with `load_program`, golden `expected.json` for the ten M1–M2 fixtures run by `tests/test_golden.py`; 422 tests passing.
- `__pycache__/` is ignored everywhere and the previously committed `.pyc` files are untracked.
- Deferred to M3: reject NUL bytes, NFKC def names (carried from M1). `build` stays a stub until M4.
- Known gaps: as listed under "M2 contracts" (resolver `self` tracking, depth-limited chunks in `never_reached`, no tree-sitter parse timeout).
