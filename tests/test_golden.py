"""Golden fixtures: each tests/fixtures/python/<case> must produce its expected.json."""

import json
from pathlib import Path

import pytest

from minstrel.cli import load_program
from minstrel.core.linearize import linearize
from minstrel.ir import Story

FIXTURES = Path(__file__).parent / "fixtures" / "python"
CASES = sorted(p.parent.name for p in FIXTURES.glob("*/expected.json"))


def summarize(story: Story) -> dict:
    """Reduce a Story to the golden format described in docs/decisions.md."""
    return {
        "entry": story.entry,
        "pages": [{"chunk": p.chunk_id, "depth": p.depth} for p in story.pages],
        "references": [
            {"call": c.call_site_id, "note": c.note, "target_page": c.target_page}
            for p in story.pages
            for c in p.calls
            if c.note != "placed"
        ],
        "not_on_path": story.not_on_path,
        "never_reached": story.never_reached,
        "diagnostics": [
            {"code": d.code, "file": d.file, "line": d.line} for d in story.diagnostics
        ],
    }


@pytest.mark.parametrize("case", CASES)
def test_golden(case: str) -> None:
    folder = FIXTURES / case
    program = load_program(folder)
    path_file = folder / "path.json"
    selection = json.loads(path_file.read_text()) if path_file.exists() else None
    actual = [
        summarize(linearize(program, entry, selection))
        for entry in program.entry_candidates[0]
    ]
    expected = json.loads((folder / "expected.json").read_text())["stories"]
    assert actual == expected


def test_every_fixture_has_expected() -> None:
    assert len(CASES) == len([p for p in FIXTURES.iterdir() if p.is_dir()])
