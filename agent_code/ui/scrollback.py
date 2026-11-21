"""Commit path from finished content to the native terminal scrollback.

OutputChunk is rendered to ANSI by Rich and then written with ``sys.stdout.write``
(inside patch_stdout) into the scrollback above the prompt -- native
scrolling/selection/copy, with pt no longer managing a history region. Fragment
styles use Rich's inline vocabulary, so they reach ``rich.text.Text`` unchanged;
only fragments persisted by older versions go through the legacy pt shim.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterable
from io import StringIO

from prompt_toolkit.application.current import get_app_or_none, get_app_session
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.text import Text

from ..output import OutputChunk, StyleAndText, chunk_fragments

_ansi_console = Console(
    file=StringIO(),
    force_terminal=True,
    color_system="truecolor",
    highlight=False,
    # Match output.py: keep the offscreen renderer wider than any real terminal so
    # padding is not silently truncated at Rich's 80-column non-tty fallback. Wrapping
    # is the terminal's job, not the fragment renderer's.
    width=4096,
)

# ---------------------------------------------------------------------------
# Legacy prompt_toolkit-vocabulary compatibility
#
# Older transcript records (and anything built before the rich-vocabulary
# unification) stored fragment styles in prompt_toolkit's inline vocabulary
# ("bold ansiwhite bg:#444444"). Rich does not know the "ansi*" spellings or the
# "bg:"/"fg:" prefixes, so such styles are translated before reaching
# rich.text.Text. New chunks always carry Rich vocabulary and skip this path.
# ---------------------------------------------------------------------------

# prompt_toolkit's ANSI color names -> Rich's equivalent names.
_LEGACY_PT_ANSI_COLORS = {
    "ansiblack": "black",
    "ansired": "red",
    "ansigreen": "green",
    "ansiyellow": "yellow",
    "ansiblue": "blue",
    "ansimagenta": "magenta",
    "ansicyan": "cyan",
    "ansiwhite": "white",
    "ansibrightblack": "bright_black",
    "ansibrightred": "bright_red",
    "ansibrightgreen": "bright_green",
    "ansibrightyellow": "bright_yellow",
    "ansibrightblue": "bright_blue",
    "ansibrightmagenta": "bright_magenta",
    "ansibrightcyan": "bright_cyan",
    "ansibrightwhite": "bright_white",
    "ansidefault": "default",
}

# prompt_toolkit attribute spellings -> Rich spellings. Rich accepts most of the same
# names; the "no*" variants need "not <attr>" and hidden is called conceal.
_LEGACY_PT_ATTRS = {
    "bold": "bold",
    "dim": "dim",
    "italic": "italic",
    "underline": "underline",
    "blink": "blink",
    "reverse": "reverse",
    "strike": "strike",
    "hidden": "conceal",
    "nobold": "not bold",
    "nodim": "not dim",
    "noitalic": "not italic",
    "nounderline": "not underline",
    "noblink": "not blink",
    "noreverse": "not reverse",
    "nostrike": "not strike",
    "nohidden": "not conceal",
}


def _legacy_pt_color(token: str) -> str:
    """prompt_toolkit color token -> Rich color token (empty means drop it)."""
    return _LEGACY_PT_ANSI_COLORS.get(token, token)


def _legacy_pt_style(style: str) -> str:
    """Translate a legacy prompt_toolkit inline style into Rich's vocabulary."""
    out: list[str] = []
    for token in style.split():
        if token.startswith("bg:"):
            color = _legacy_pt_color(token[3:])
            if color:
                out.extend(("on", color))
        elif token.startswith("fg:"):
            color = _legacy_pt_color(token[3:])
            if color:
                out.append(color)
        elif token == "on":
            # Already Rich syntax; leave it for the following color token.
            out.append(token)
        elif token in _LEGACY_PT_ATTRS:
            out.append(_LEGACY_PT_ATTRS[token])
        else:
            out.append(_legacy_pt_color(token))
    return " ".join(out)


# Attribute names Rich understands (rich/style.py::STYLE_ATTRIBUTES). Used to tell a
# pt-only spelling apart from a name both vocabularies share ("bold", "underline", ...).
_RICH_ATTRS = {
    "dim",
    "bold",
    "italic",
    "underline",
    "blink",
    "blink2",
    "reverse",
    "conceal",
    "strike",
    "underline2",
    "frame",
    "encircle",
    "overline",
}


def _looks_legacy_pt_style(style: str) -> bool:
    """Heuristic: does this style string use prompt_toolkit's vocabulary?

    Two markers: the "ansi*" color names and the "bg:"/"fg:" prefixes do not exist in
    Rich, and the "no*"/"hidden" attribute spellings are pt-only (Rich writes "not
    <attr>" and calls hidden "conceal"; it raises on these tokens rather than ignoring
    them, so missing the detection would crash the commit).
    """
    for token in style.split():
        if token.startswith(("ansi", "bg:", "fg:")):
            return True
        if token in _LEGACY_PT_ATTRS and token not in _RICH_ATTRS:
            return True
    return False


def _terminal_columns() -> int:
    """Current terminal width, with a fallback for replay before the prompt app starts."""
    app = get_app_or_none()
    if app is not None:
        return app.output.get_size().columns
    try:
        return get_app_session().output.get_size().columns
    except Exception:
        return shutil.get_terminal_size((80, 24)).columns


def _pad_full_width(fragments: Iterable[StyleAndText], columns: int) -> list[StyleAndText]:
    """Pad each *visual line* to ``columns`` display cells.

    Width is accumulated across all fragments (a line may be split into several
    styled pieces), and padding is emitted only when the line ends at ``\n``. Tabs are
    expanded before measuring because ``get_cwidth("\t")`` is 0 while terminals expand
    tabs to real columns. Over-wide lines are left unpadded; Rich is configured with a
    4096-column offscreen width, so it does not wrap them and the terminal handles the
    overflow (wrapped continuation lines intentionally get no synthetic background).
    Padding inherits the last non-empty style on the line, so a prompt background
    becomes one continuous bar.

    Padding happens at commit time only; stored text/fragments remain unpadded.
    """
    if columns <= 0:
        return list(fragments)

    padded: list[StyleAndText] = []
    line_width = 0
    line_has_text = False
    line_style = ""

    def emit(style: str, text: str) -> None:
        if not text:
            return
        if padded and padded[-1][0] == style:
            padded[-1] = (style, padded[-1][1] + text)
        else:
            padded.append((style, text))

    for style, text in fragments:
        text = text.expandtabs(8)
        start = 0
        for index, char in enumerate(text):
            if char != "\n":
                continue
            segment = text[start:index]
            if segment:
                emit(style, segment)
                line_width += get_cwidth(segment)
                line_has_text = True
                if style:
                    line_style = style
            if line_has_text and line_style and line_width < columns:
                emit(line_style, " " * (columns - line_width))
            emit(style, "\n")
            line_width = 0
            line_has_text = False
            line_style = ""
            start = index + 1

        tail = text[start:]
        if tail:
            emit(style, tail)
            line_width += get_cwidth(tail)
            line_has_text = True
            if style:
                line_style = style

    if line_has_text and line_style and line_width < columns:
        emit(line_style, " " * (columns - line_width))
    return padded


def _chunk_fragments(chunk: OutputChunk, columns: int | None = None) -> list[StyleAndText]:
    """OutputChunk -> (style string, text) fragments.

    Styles are Rich vocabulary by default. Fragments stored by older versions use
    prompt_toolkit vocabulary and are translated through the legacy shim. ``columns``
    is forwarded to the markdown renderer as its wrap width.
    """
    if chunk.fragments:
        fragments = list(chunk.fragments)
        if any(_looks_legacy_pt_style(style) for style, _ in fragments):
            return [(_legacy_pt_style(style), text) for style, text in fragments]
        return fragments
    if chunk.format in ("rich", "markdown"):
        return chunk_fragments(chunk, width=columns)
    return [("", chunk.text)]


def _chunk_to_ansi(chunk: OutputChunk, columns: int | None = None) -> str:
    """OutputChunk -> ANSI string.

    Fragments are rebuilt into a Rich Text and rendered to ANSI; styles already use
    Rich's vocabulary, so no per-fragment translation is needed (legacy
    prompt_toolkit styles are normalized in ``_chunk_fragments``).

    ``columns`` (current terminal width by default) is the render width: it is the
    wrap target for markdown chunks, which Rich hard-wraps, and the padding target for
    ``full_width`` chunks. Resolving it here -- at commit time -- is what keeps rendered
    width equal to the terminal's.
    """
    width = columns
    if width is None and (chunk.full_width or chunk.format == "markdown"):
        width = _terminal_columns()
    fragments = _chunk_fragments(chunk, width)
    if chunk.full_width and width is not None:
        fragments = _pad_full_width(fragments, width)

    text = Text()
    for style, fragment in fragments:
        text.append(fragment, style=style)
    with _ansi_console.capture() as capture:
        _ansi_console.print(text, end="")
    return capture.get()


def commit_chunk(chunk: OutputChunk, columns: int | None = None) -> None:
    """Write an OutputChunk into the terminal scrollback above the prompt.

    Uses ``sys.stdout.write``: ``patch_stdout`` replaces stdout with a StdoutProxy that
    draws above the prompt through run_in_terminal on its own flush thread -- the only
    thread-safe commit path that does not fight prompt_async over input.

    Do not use pt's ``print_formatted_text``: while an app is running it goes through
    ``loop.call_soon_threadsafe(run_in_terminal(...))`` and the caller cannot wait, so in
    a loop of "commit, then immediately await the next prompt_async" it races the
    prompt's input detach/attach and all subsequent input stops working.
    """
    sys.stdout.write(_chunk_to_ansi(chunk, columns=columns))
    sys.stdout.flush()
