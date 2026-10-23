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
- Ids: chunk `<module>:<qualname>`, module top level `<module>:<module>`, a repeated qualname in one module gets `~2`, `~3`; `<chunk>#callN` and `<chunk>#branchN` count from 1 in source order; arms `<branch>.armN`.
- A `module_toplevel` chunk exists only when the module has call sites outside function bodies (top level, class bodies, main guard). `never_reached` excludes module top levels, since they run on import, not by call.
- Decorator and default-argument calls are not call sites in v1 (they run at definition time).
- `Page.active_ranges` = selected arm spans; `inactive_ranges` = unselected arm spans plus, after a `return`/`raise`, the rest of the chunk. Both empty in "show everything". Ranges are `[start_line, end_line]`, inclusive, file line numbers.
- `Page.breadcrumb` is chunk ids from the entry to this page; renderers show names.
- `Story` = `entry`, `mode`, `pages`, `not_on_path` (order the all-arms walk first reaches them), `never_reached` (program chunk order), `diagnostics` (one per unresolved call site walked, and depth-limit hits).
- Default entries: every candidate in the best tier present (main guard > function `main` > top level with calls); `--entry` overrides.

## M0 status (2026-10-23)

- Built: `pyproject.toml` (hatchling, sdist allowlist so stray root files like `.env` never ship), `cli.py` stubs for all four commands, `tests/test_cli.py` (6 passing), `.venv` with the package installed editable.
- Deferred: `.gitignore` (awaiting the user's call).
