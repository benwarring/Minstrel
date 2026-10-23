"""Command-line interface for Minstrel.

Defines the Typer ``app`` behind the ``minstrel`` console script. In M0
every command is a stub; later milestones replace the bodies.
"""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

app = typer.Typer(
    help="Lay out Python functions as pages in execution order.",
    no_args_is_help=True,
)

SourcePath = Annotated[
    Path,
    typer.Argument(exists=True, help="Python file or directory to read."),
]
EntryOption = Annotated[
    str | None,
    typer.Option("--entry", help="Entry point to start from (default: top-ranked)."),
]


def _not_implemented(command: str) -> NoReturn:
    """Report that ``command`` is a stub and exit with status 1.

    Args:
        command: CLI command name shown in the message.

    Raises:
        typer.Exit: always, with exit code 1.
    """
    typer.echo(f"minstrel {command}: not implemented yet", err=True)
    raise typer.Exit(code=1)


@app.command()
def entries(path: SourcePath) -> None:
    """List entry-point candidates."""
    _not_implemented("entries")


@app.command()
def build(
    path: SourcePath,
    entry: EntryOption = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="HTML file to write.")
    ] = Path("story.html"),
) -> None:
    """Write the interactive HTML viewer."""
    _not_implemented("build")


@app.command("print")
def print_outline(
    path: SourcePath,
    entry: EntryOption = None,
    path_file: Annotated[
        Path | None,
        typer.Option(
            "--path",
            exists=True,
            dir_okay=False,
            help="JSON file mapping branch ids to selected arm ids.",
        ),
    ] = None,
) -> None:
    """Print the page outline to the terminal."""
    _not_implemented("print")


@app.command()
def ir(
    path: SourcePath,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="JSON file to write (default: stdout)."),
    ] = None,
) -> None:
    """Dump the resolved IR as JSON."""
    _not_implemented("ir")
