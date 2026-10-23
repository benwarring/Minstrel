# Code Storybook: Build Plan for Claude Code

Oct 23, 2026 · @Ben

## Overview

Build a CLI tool (working name `M``in``s``t``r``e``l`) that statically reads Python code, starts at an entry point, and lays its functions out as pages in the order they would run. Readers can switch which arm of each conditional they follow, and the page order updates to match.

Version 1 targets Python and outputs one self-contained, interactive HTML file. The core must stay language-neutral so Java and C can be added later as new adapters, without touching the linearizer or the viewer.

Given this input, the story is page 1 `main`, then page 2 `helloWorld`:

```python
def helloWorld():
    print("Hello, World")

def main():
    helloWorld()
```

Claude Code should work through the milestones in order, finishing each milestone's acceptance criteria and tests before starting the next. Keep this plan in the repo as `docs/PLAN.md`.

## Scope

Version 1 is static analysis of Python 3 code, with selectable paths through conditionals. Everything else is deferred, but the design must not block it.

In version 1:

- Python 3 source, including `match` statements. No Python 2.
- Chunks are functions, methods, nested functions, and module top-level code.
- Branch points for `if`/`elif`/`else`, `match`/`case`, `try`/`except`/`else`/`finally`, and `for`/`while` loops (including loop `else`).
- Calls resolved by name within a file first, then across files and imports in a package (milestone 5), then through classes and inheritance (milestone 6).
- Output as JSON IR, a terminal outline, and a self-contained HTML viewer.

Not in version 1:

- Dynamic tracing (running the code and recording the real order).
- Java and C adapters. The adapter interface must be designed so they fit later.
- Full type inference. Where a call could go to several implementations, the reader chooses, as with a branch.
- Splitting a function into several pages at its call sites. The IR should keep enough position data to add this later.
- An IDE extension.

Expected per-language approach, for planning the adapter interface:

| Language | Parser | Call resolution | Known hard cases |
| --- | --- | --- | --- |
| Python (v1) | tree-sitter-python | Own scope and import resolver; Jedi as an optional upgrade | Duck typing, dynamic attributes, `getattr` |
| Java (later) | tree-sitter-java | Packages, imports, class hierarchy | Overrides, interfaces, lambdas, reflection |
| C (later) | tree-sitter-c | Translation units and headers | Function pointers, macros, preprocessor conditionals |

## Architecture

A per-language adapter turns source into a language-neutral IR, and a shared core turns that IR into stories. Adding Java or C means writing a new adapter; the core and viewer stay unchanged.

&#91;embedded content: pipeline · 3 adapter stages, the IR, 3 core stages, the viewer\]

Stages in order: parse, extract and resolve (adapter), then build graph, linearize and render (core). The reader's arm choices form a path that feeds the linearizer, which also runs inside the viewer so switching arms is instant.

## Data model

The IR is the contract between adapters and the core. It holds no language-specific types: anything language-flavored (like the word `elif`) arrives as a display string from the adapter.

Each chunk's body is an ordered block of three node kinds: call sites, branch points, and terminators. Ordinary statements are not modeled; line spans are enough for display.

| Entity | Key fields | Notes |
| --- | --- | --- |
| Program | `schema_version`, `language`, `files`, `chunks`, `entry_candidates`, `diagnostics` | Top-level IR document, serialized as JSON |
| Chunk | `id`, `kind` (function, method, module\_toplevel), `name`, `file`, `start_line`, `end_line`, `source`, `body` | `id` is the qualified name, e.g. `pkg.mod:Class.method` |
| Block | ordered list of CallSite, BranchPoint, Terminator | Order is source order, which is execution order within a chunk |
| CallSite | `id`, `line`, `col`, `callee_text`, `targets` (chunk ids), `status` (resolved, ambiguous, external, unresolved), `conditional` | `conditional` marks calls inside `and`/`or`, ternaries, comprehensions, lambdas |
| BranchPoint | `id`, `kind` (if, match, try, loop), `line`, `arms`, `default_arm` | Calls in the condition belong to the block before the branch point |
| Arm | `id`, `label`, `start_line`, `end_line`, `body`, `terminates` | `label` is display text, e.g. `if user.is_admin`, `except ValueError`, `loop body skipped` |
| Terminator | `kind` (return, raise, break, continue), `line` | Ends the walk of the enclosing block on that path |
| Path | map of BranchPoint id to Arm id | Missing entries mean the default arm |
| Story | ordered `pages`, `not_on_path`, `never_reached` | Output of the linearizer |
| Page | `index`, `chunk_id`, `depth`, `breadcrumb`, `reached_from` (call site id), `active_ranges`, `inactive_ranges` | Ranges drive highlighting and dimming in the viewer |

Ids must be stable across runs so saved paths keep working. Build branch and call ids from the chunk id plus an ordinal within the chunk (e.g. `app:main#branch2`), not from line numbers.

## Branching and path selection

Every branch point has a default arm, and the reader can switch any branch to another arm. Because each chunk appears as a full page only once, a selection applies to that branch point everywhere, not per call.

How each construct maps to arms:

| Construct | Arms | Default |
| --- | --- | --- |
| `if` / `elif` / `else` | One arm per clause; a missing `else` becomes an empty "condition false" arm | First clause |
| `match` / `case` | One arm per `case`; an empty "no case matched" arm if there is no wildcard | First case |
| `try` / `except` / `else` / `finally` | "No exception" (try, else, finally) and one arm per `except` (try, handler, finally) | No exception |
| `for` / `while` | "Body runs" (body once, then loop `else` if present) and "body skipped" | Body runs |
| Ambiguous call | One arm per candidate implementation, labeled "which implementation?" | First candidate |

Rules the linearizer must respect:

1. Calls inside a condition (`if is_valid(x):`) run before the arm is chosen, so they are walked on every path.
2. An arm that ends in `return` or `raise` stops the walk of the rest of its chunk on that path. `break` and `continue` stop the rest of the loop body.
3. In an `except` arm, the try body is shown as "may run partially": its calls stay in the story but are marked as possibly interrupted.
4. Calls marked `conditional` (inside `and`/`or`, ternaries, comprehensions) are walked in v1 and badged as conditional. Turning short-circuits into real branch points is a later enhancement.

The viewer offers two modes. "Follow one path" walks only selected arms. "Show everything" walks all arms in source order, labels each page with the arm it came from, and is useful as an overview. The current selection is stored in the URL hash so a path can be bookmarked or shared.

## Linearization rules

Page order is a depth-first walk from the entry chunk, which mirrors execution: a callee and everything it calls appear before the caller's next call.

1. The selected entry chunk is page 1.
2. Walk its block in source order. At a resolved call to a chunk not yet placed, place that chunk as the next page and walk it completely before continuing.
3. At a branch point, walk only the selected arm (or every arm in "show everything" mode).
4. A call to a chunk already placed adds no page. Its call site becomes a link: "see page N".
5. Recursion and cycles follow rule 4, labeled "recursive call, see page N".
6. External calls (standard library, third-party packages) add no page. The call site is badged as external.
7. Unresolved calls add no page. The call site is flagged and listed in the diagnostics panel.
8. A walk deeper than a configurable limit (default 50) stops and records a diagnostic.
9. Chunks reachable from the entry on some path but not the current one go in a "Not on this path" appendix. Chunks unreachable on every path go in "Never reached", which doubles as a dead-code report.

Entry detection, in priority order: the `if __name__ == "__main__":` block, a function named `main`, then module top-level code that makes calls. Users can override with `--entry`, and a codebase with several entry points produces one story per entry.

Python-specific resolution notes for the adapter: `Foo()` resolves to `Foo.__init__` when defined. Decorators run at definition time, so they are shown in the chunk's source but not walked in v1.

## Output and viewer

The tool writes one self-contained HTML file that works offline, plus JSON and terminal outputs for testing and scripting.

CLI commands:

| Command | Does |
| --- | --- |
| `m``i``ns``t``r``e``l`` ``entries \<path>` | Lists entry-point candidates |
| `m``i``n``strel` `build <path> [--entry NAME] [-o story.html]` | Writes the interactive HTML viewer |
| `m``in``s``t``r``e``l` `print <path> [--entry NAME] [--path path.json]` | Prints the page outline to the terminal |
| `m``i``ns``t``r``el` `ir <path> [-o ir.json]` | Dumps the resolved IR |

The linearizer runs in two places. Python is the canonical implementation, used by `print` and the tests. The viewer has a small JavaScript port so arm switching is instant without a server. Both run against the same golden fixtures, so they cannot drift apart.

Viewer behavior:

- Pages appear in reading order as a vertical scroll, each with a breadcrumb of the call stack (e.g. `main → load_config → parse_line`).
- Source is syntax-highlighted at build time with Pygments. Lines in inactive arms are dimmed.
- Each call site is a link to its target page, or to "see page N" for repeats. External and unresolved calls get badges.
- Each branch point shows an arm selector on its line. Changing it re-runs the JavaScript linearizer and re-renders.
- A sidebar holds a table of contents indented by depth, the diagnostics list, and the two appendices.
- A toggle switches between "Follow one path" and "Show everything". The selection is kept in the URL hash.
- All CSS and JavaScript are inlined, with light and dark themes. No network access.

## Repository layout and tech stack

The tool is written in Python 3.11+, with a vanilla JavaScript viewer and no front-end build step.

| Need | Choice | Why |
| --- | --- | --- |
| Parsing | `tree-sitter` + `tree-sitter-python` (later `-java`, `-c`) | One toolchain for all three languages, tolerant of syntax errors, exact positions |
| Highlighting | Pygments | Mature, supports Python, Java and C, renders to HTML at build time |
| CLI | Typer | Small, typed command definitions |
| Tests | pytest; Node's built-in test runner for the viewer linearizer | No extra JavaScript dependencies |
| Lint and format | ruff | One tool for both |

```plain
minstrel/
  pyproject.toml
  CLAUDE.md                 # points Claude Code to docs/PLAN.md
  docs/PLAN.md              # this plan
  docs/decisions.md         # choices made during the build
  src/minstrel/
    cli.py
    ir.py                   # dataclasses, JSON (de)serialization, schema version
    core/
      graph.py              # call graph from resolved IR
      linearize.py          # canonical linearizer
      entries.py            # entry-point ranking
    adapters/
      base.py               # LanguageAdapter protocol
      python/
        extract.py          # tree-sitter tree -> chunks, call sites, branch points
        resolve.py          # scopes, imports, classes
    render/
      text.py
      html.py
      viewer/               # template.html, viewer.js, viewer.css
  tests/
    fixtures/python/<case>/ # input files, optional path.json, expected.json
  viewer-tests/             # runs the same fixtures against viewer.js
```

The `LanguageAdapter` protocol has five members: `extensions`, `parse(file)`, `extract(tree, file) -> list[Chunk]`, `resolve(program)` (fills call targets and statuses), and `entry_candidates(program)`. Nothing in `core/`, `render/` or `ir.py` may import from `adapters/`.

## Milestones

Eight milestones (M0–M7) take the tool from an empty repo to a hardened Python v1. Each ends with its acceptance criteria passing.

1. **M0 Skeleton.** Package, `pyproject.toml`, Typer CLI stubs for all four commands, pytest and ruff configured, `CLAUDE.md` written.
   - Done when ` minstrel ` `--help` lists the commands and an empty test suite passes.
2. **M1 Extraction.** Python adapter turns a file into chunks (functions, methods, nested functions, module top level) with ordered call sites. IR serializes to JSON and back.
   - Done when extraction fixtures pass and IR round-trips without loss.
3. **M2 Single-file story, no branches.** Resolve direct name calls, nested scopes, `self.method()` within a class, and `Foo()` to `Foo.__init__`. Mark external calls. Implement linearizer rules 1–2 and 4–9 and ` minstrel  ``print`.
   - Done when the hello-world example prints `main` then `helloWorld`, and the chain, repeated-helper, recursion, external and unresolved fixtures pass.
4. **M3 Branches.** Extract branch points and terminators for every construct in the branching table. Add path selection to the linearizer and the `--path` option.
   - Done when each construct has a fixture with expected output for its default path and at least one alternate path, including early return.
5. **M4 HTML viewer.** `m``instrel` `build` writes the self-contained viewer described above, with the JavaScript linearizer.
   - Done when the file opens offline, switching an arm reorders pages, URL-hash paths reload correctly, and the Python and JavaScript linearizers agree on every fixture.
6. **M5 Multi-file Python.** Walk a directory or package; resolve `import x`, `from x import y`, relative imports and `__init__.py` re-exports; rank entry candidates across the project.
   - Done when a three-module fixture package produces the expected story.
7. **M6 Classes and dispatch.** Inheritance, `super()`, and overridden methods. Infer attribute types from simple assignments (`self.repo = Repo()`) and type hints. Calls with several candidates become "which implementation?" branch points.
   - Done when the inheritance and dispatch fixtures pass.
8. **M7 Hardening.** Run on two or three small real open-source Python projects. Skip unparseable files with a diagnostic. Target under 10 seconds for a 10,000-line project.
   - Done when the real-project runs complete and their diagnostics are reviewed and triaged in `docs/decisions.md`.

After v1, in rough order: the Java adapter, the C adapter, a dynamic-trace mode, splitting functions at call sites, and an IDE extension.

## Testing

Golden fixtures are the backbone: each is a tiny program with its expected story, and both linearizers must match them.

Each fixture folder holds the input source files, an optional `path.json` with arm selections, and an `expected.json` listing pages (chunk id and depth), references, appendices and diagnostics. Unit tests cover extraction and resolution separately, so a failure points at the right stage.

Fixtures to create, by milestone:

| Milestone | Fixtures |
| --- | --- |
| M1–M2 | `hello_world`, `chain`, `repeated_helper`, `recursion`, `mutual_recursion`, `nested_function`, `external_calls`, `unresolved_call`, `dead_code`, `multiple_entries` |
| M3 | `if_else`, `elif_chain`, `missing_else`, `early_return`, `match_case`, `try_except`, `try_finally`, `for_loop`, `while_else`, `calls_in_condition`, `short_circuit` |
| M5 | `package_imports`, `relative_imports`, `init_reexport` |
| M6 | `class_methods`, `inheritance_override`, `super_call`, `attribute_types` |

The viewer gets one smoke test that loads the built HTML in Node, switches an arm, and checks the new page order. A full browser test (e.g. Playwright) is optional.

## Working instructions for Claude Code

Work one milestone at a time, tests first, and keep the core language-neutral.

- Read `docs/PLAN.md` and `docs/decisions.md` at the start of every session.
- For each milestone, write its fixtures and expected outputs first, then the implementation. Do not start the next milestone until all tests and ruff pass.
- Never import from `adapters/` in `core/`, `render/` or `ir.py`. If the core seems to need a language detail, add a neutral field to the IR instead.
- Any IR change bumps `schema_version` and updates affected fixtures in the same commit.
- Keep modules small and readable; this is a tool for reading code, so its own code should read well.
- When this plan doesn't cover a choice, take the simplest option that keeps Java and C possible, and record it in `docs/decisions.md` with one line of reasoning.
- Ask before adding any dependency not listed in the tech-stack table.
- At the end of each milestone, add a short status note to `docs/decisions.md`: what was built, what was deferred, known gaps.

## Open questions

Each question has a default so Claude Code can start now; change any of them before or during the build.

| Question | Default assumed |
| --- | --- |
| Tool name | `M``ins``t``r``el`(placeholder) |
| Where do you want to read stories: browser, terminal, or eventually an editor like VS Code? | Self-contained HTML first, terminal outline for testing |
| Parser for Python: tree-sitter for all languages, or Python's `ast` plus Jedi for stronger Python resolution? | tree-sitter, with Jedi as a later optional resolver |
| How large are the codebases you'll run it on? | Up to about 10,000 lines for v1 |
| Should a repeated call show the callee again or link back? | Link back ("see page N") |
| Loops: one "runs once" pass, or let the reader pick a number of iterations? | Runs once, or skipped |
| Open source? Which license? | Decide later; MIT if public |
| Python version the tool itself requires | 3.11+ |
