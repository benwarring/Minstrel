"""Tests for the ``minstrel`` command surface defined in ``minstrel.cli``.

M0 commands are stubs, so these tests pin the command names and their
documented options rather than the stub behavior.
"""

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from minstrel.cli import app

COMMANDS = ["entries", "build", "print", "ir"]


def _has_token(text: str, token: str) -> bool:
    """Return True if ``token`` appears in ``text`` as a whole word.

    A plain substring check is not enough: ``-o`` is inside ``--output``.
    """
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_help_lists_all_commands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in COMMANDS:
        assert _has_token(result.output, command)


@pytest.mark.parametrize(
    ("command", "options"),
    [
        ("entries", []),
        ("build", ["--entry", "-o"]),
        ("print", ["--entry", "--path"]),
        ("ir", ["-o"]),
    ],
)
def test_command_help_shows_documented_options(
    runner: CliRunner, command: str, options: list[str]
) -> None:
    result = runner.invoke(app, [command, "--help"])

    assert result.exit_code == 0
    for option in options:
        assert _has_token(result.output, option)


def test_stub_command_exits_nonzero(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["entries", str(tmp_path)])

    assert result.exit_code == 1
    # SystemExit means a clean typer.Exit, not an unhandled crash.
    assert isinstance(result.exception, SystemExit)
