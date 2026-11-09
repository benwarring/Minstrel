"""Plain-text outline of a Story, printed by ``minstrel print``.

Pages are listed in reading order, indented by call depth, each followed by
one line per call walked on it. The two appendices and the story's
diagnostics come last. Strings from the analyzed code (names, ids, file
names, callee text, arm labels, messages) can hold terminal escape sequences
or bidi overrides, so every output line goes through ``escape_control_chars``
before it is returned. The outline's own punctuation is ASCII.
"""

from __future__ import annotations

import unicodedata

from minstrel.ir import (
    CallSite,
    Chunk,
    Diagnostic,
    Page,
    PageCall,
    Program,
    Story,
    iter_call_sites,
)

# Cc (controls such as ESC) and Cf (format characters such as bidi overrides)
# are the set docs/decisions.md requires. Cs, Zl and Zp are added because a
# lone surrogate can't be encoded for output and U+2028/U+2029 can act as
# line breaks.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

_MODE_TITLES = {"follow": "follow one path", "all": "show everything"}
_INDENT = "  "
_SEPARATOR = "  "
# Width the "number + name" part of a page line is padded to, so the
# file:line locations line up in one column.
_NAME_COLUMN_WIDTH = 32
_CALL_BADGES = {
    "placed": "-> page {page}",
    "see_page": "-> see page {page}",
    "recursive": "-> recursive, see page {page}",
    "external": "[external]",
    "unresolved": "[unresolved]",
    "depth_limit": "[depth limit]",
}


def escape_control_chars(text: str) -> str:
    r"""Replace characters that could drive or garble a terminal with escapes.

    Covers Unicode categories Cc (controls such as ESC and newline), Cf
    (format characters such as the bidi override U+202E), Cs (lone
    surrogates), Zl and Zp (line and paragraph separators). Each becomes
    the escape ``ascii()`` would write: ESC becomes ``\x1b``, a newline
    ``\n``, and U+202E a backslash-u escape of its four hex digits. All
    other text, non-ASCII included, is kept as is.

    Args:
        text: Text that may have come from the analyzed code.

    Returns:
        ``text`` with every such character escaped.
    """
    return "".join(_escape_char(char) for char in text)


def render_text(story: Story, program: Program) -> str:
    """Render ``story`` as a plain-text outline for the terminal.

    Args:
        story: The linearizer's output to show.
        program: The program the story was built from. It supplies each
            page's chunk name, file and line, and each call's callee text.

    Returns:
        The outline: a header, one line per page plus one per call walked on
        it, then the "Not on this path", "Never reached" and "Diagnostics"
        sections ("(none)" when empty). Control and format characters are
        escaped, no line has trailing whitespace, and the text ends with
        exactly one newline.

    Raises:
        KeyError: if a page names a chunk missing from ``program``, or a page
            call names a call site missing from that page's chunk.
    """
    chunks = {chunk.id: chunk for chunk in program.chunks}
    lines = [f"Story from {story.entry} ({_MODE_TITLES[story.mode]})", ""]
    for page in story.pages:
        lines += _page_lines(page, chunks[page.chunk_id])
    lines.append("")
    lines += _section("Not on this path", story.not_on_path)
    lines += _section("Never reached", story.never_reached)
    diagnostics = [_diagnostic_text(diagnostic) for diagnostic in story.diagnostics]
    lines += _section("Diagnostics", diagnostics)
    # Escaping whole finished lines is the single choke point, so no
    # code-derived field can be missed. rstrip drops trailing spaces that
    # code-derived text may bring.
    return "\n".join(escape_control_chars(line).rstrip() for line in lines) + "\n"


def _escape_char(char: str) -> str:
    """Return ``char`` escaped if its Unicode category is unsafe, else as is."""
    if unicodedata.category(char) in _UNSAFE_CATEGORIES:
        # ascii() spells it \xNN, \uNNNN, \UNNNNNNNN (or \n, \t); [1:-1]
        # drops the quotes it adds.
        return ascii(char)[1:-1]
    return char


def _page_lines(page: Page, chunk: Chunk) -> list[str]:
    """Format a page's heading line and one line per call walked on it."""
    number = f"{page.index:>3}. "
    indent = _INDENT * page.depth
    heading = f"{number}{indent}{chunk.name}".ljust(_NAME_COLUMN_WIDTH)
    heading += _SEPARATOR + _location(chunk.file, chunk.start_line)
    if page.via_arm is not None:
        heading += f"{_SEPARATOR}[via {page.via_arm}]"
    # Call lines sit one indent step to the right of the page's name.
    call_indent = " " * len(number) + indent + _INDENT
    sites = {site.id: site for site in iter_call_sites(chunk.body)}
    calls = [_call_text(call, sites[call.call_site_id]) for call in page.calls]
    return [heading, *(call_indent + text for text in calls)]


def _call_text(call: PageCall, site: CallSite) -> str:
    """Format one call line: the callee as written, then its badges."""
    parts = [site.callee_text, _CALL_BADGES[call.note].format(page=call.target_page)]
    if call.conditional:
        parts.append("[conditional]")
    if call.maybe_interrupted:
        parts.append("[maybe interrupted]")
    return _SEPARATOR.join(parts)


def _location(file: str | None, line: int | None) -> str:
    """Format ``file:line``; either part may be missing, giving "" if both are."""
    if file is None:
        return "" if line is None else f"line {line}"
    return file if line is None else f"{file}:{line}"


def _diagnostic_text(diagnostic: Diagnostic) -> str:
    """Format a diagnostic as location, code and message, skipping empty parts."""
    parts = [
        _location(diagnostic.file, diagnostic.line),
        diagnostic.code,
        diagnostic.message,
    ]
    return _SEPARATOR.join(part for part in parts if part)


def _section(title: str, items: list[str]) -> list[str]:
    """Format a titled list, one indented item per line, or "(none)" inline."""
    if not items:
        return [f"{title}: (none)"]
    return [f"{title}:", *(_INDENT + item for item in items)]
