"""The ``LanguageAdapter`` protocol: what each language adapter gives the core.

The CLI runs an adapter over a project in order: ``parse`` and ``extract`` each
file, gather the chunks into a ``Program``, ``resolve`` it, then ask for
``entry_candidates``. The core only ever sees the IR from ``minstrel.ir``;
parse trees stay private to the adapter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from minstrel.ir import Chunk, Program


class LanguageAdapter(Protocol):
    """Turns one language's source files into Minstrel's language-neutral IR.

    Attributes:
        language: Language name copied into ``Program.language``, e.g.
            ``"python"``.
        extensions: File suffixes this adapter reads, with the dot, e.g.
            ``(".py",)``.
    """

    language: str
    extensions: tuple[str, ...]

    def parse(self, path: Path) -> object:
        """Read and parse one source file.

        Args:
            path: The file to read.

        Returns:
            A parse tree whose type is private to the adapter; the core passes
            it back to ``extract`` without looking inside.

        Raises:
            OSError: The file can't be read.
            ValueError: The file can't be decoded as text (for example
                ``UnicodeDecodeError``).
        """
        ...

    def extract(self, tree: object, file: str) -> list[Chunk]:
        """Turn one parsed file into chunks.

        Args:
            tree: A tree returned by this adapter's ``parse``.
            file: The file's root-relative POSIX path, used for ``Chunk.file``
                and to derive the module name in chunk ids.

        Returns:
            The file's chunks in ``Program.chunks`` order, each with its call
            sites (and branch points) in order and targets not yet filled.
        """
        ...

    def resolve(self, program: Program) -> None:
        """Fill every call site's ``targets`` and ``status`` in place.

        May append to ``program.diagnostics``. Adapters may keep the trees
        from ``parse`` to do this instead of adding language fields to the IR.

        Args:
            program: The program built from this adapter's ``extract`` output.
        """
        ...

    def entry_candidates(self, program: Program) -> list[list[str]]:
        """Rank the program's possible entry points.

        Args:
            program: A resolved program.

        Returns:
            Tiers of chunk ids, best tier first; the default entries are the
            whole first tier. Empty if there are no candidates.
        """
        ...
