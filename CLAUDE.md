# Minstrel

Read `docs/PLAN.md` and `docs/decisions.md` at the start of every session.

- Work one milestone at a time; don't start the next until all tests and ruff pass.
- Never import from `minstrel.adapters` in `core/`, `render/` or `ir.py`. If the core needs a language detail, add a neutral IR field.
- Any IR change bumps `SCHEMA_VERSION` in `ir.py` and updates affected fixtures in the same commit.
- Ask before adding a dependency not in the PLAN.md tech-stack table.
- Record choices the plan doesn't cover in `docs/decisions.md`, one line of reasoning each.

Commands (Windows, from repo root):

- Tests: `.venv/Scripts/python -m pytest`
- Viewer tests: `node --test viewer-tests/`
- Lint: `.venv/Scripts/ruff check .`
