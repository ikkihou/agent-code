"""Canonical representation of agent output plus style translation.

OutputChunk is the single output unit shared by every frontend (one-shot direct
printing / REPL panel / session transcript persistence): plain text plus optional
style fragments. Fragments use prompt_toolkit inline style strings
("bold yellow", "bg:#444444") directly -- the output path never goes through ANSI:
Rich only parses markup text into a styled Text, and each frontend renders the
resulting fragments itself.
"""

# here put the import lib
from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Callable, Literal

from prompt_toolkit.styles.style import parse_color
from rich.color import Color
from rich.console import Console

OutputFormat = Literal["plain", "rich", "markdown"]

# (style string, text) fragments; the style string uses prompt_toolkit's inline
# style vocabulary, e.g. "bold yellow".
StyleAndText = tuple[str, str]

# The single output sink: receives one OutputChunk per complete piece of output
# (text carries its own trailing newline), and lets the frontend decide how to
# display it -- one-shot mode prints to the terminal, the REPL renders via its
# scrollback/Live pipeline across threads.
OutputWriter = Callable[["OutputChunk"], None]


@dataclass(frozen=True)
class OutputChunk:
    """One complete piece of output.

    - text is always ANSI-free plain text (persistence, clipboard copies and plain
      consumers all use it);
    - when fragments is non-empty it is the final styling, adopted by the frontend
      as-is with no further parsing;
    - format == "rich" means text is Rich markup (e.g. ``[bold]x[/bold]``) to be
      rendered by the frontend: the REPL converts it to fragments, one-shot hands
      it to Rich;
    - full_width means the styled content should be padded to the terminal width at
      render/commit time. The stored text/fragments stay unpadded so transcripts do
      not bake in one terminal's width.
    """

    text: str
    format: OutputFormat = "plain"
    fragments: tuple[StyleAndText, ...] = ()
    full_width: bool = False


# ---------------------------------------------------------------------------
# Rich markup -> style fragments
# ---------------------------------------------------------------------------

_offscreen: Console | None = None


def _get_offscreen_console() -> Console:
    """Process-wide shared offscreen Console: only used to parse markup into
    Text/segments and never writes files (render/render_str do no file IO). The
    width is raised to 4096 so long lines are not wrapped at the fragment layer --
    wrapping is the frontend's job (the REPL panel wraps at its own width, one-shot
    relies on the terminal's soft wrap)."""
    global _offscreen
    if _offscreen is None:
        _offscreen = Console(
            file=StringIO(),
            force_terminal=True,
            color_system="truecolor",
            width=4096,
        )
    return _offscreen


# Attribute tokens prompt_toolkit accepts (styles/style.py::_parse_style_str).
_PT_ATTR_TOKENS = {
    "bold", "nobold", "italic", "noitalic", "underline", "nounderline",
    "strike", "nostrike", "blink", "noblink", "reverse", "noreverse",
    "hidden", "nohidden", "dim", "nodim",
}


def _pt_color(token: str) -> str:
    """rich color token -> pt color token.

    Named colors (red/green/...) and hex (#444444) share the same notation on both
    sides, so they pass through. rich's ``color(N)`` (256-color) is unknown to pt
    and is converted to the equivalent hex. The 0-15 standard colors depend on the
    terminal palette and have no hex equivalent, so they return an empty string
    (dropped).
    """
    if token.startswith("color(") and token.endswith(")"):
        try:
            triplet = Color.parse(token).triplet
        except Exception:
            return ""
        if triplet is None:
            return ""
        return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"
    return token


def _pt_style(style: object) -> str:
    """rich Style -> prompt_toolkit inline style string.

    The two vocabularies mostly overlap (bold/dim/red/...), with three differences:
    rich writes backgrounds as ``on <color>`` while pt writes ``bg:<color>``,
    ``color(N)`` needs a hex conversion, and rich writes negated attributes as
    ``not bold`` while pt writes ``nobold`` -- dropping the "not" would INVERT the
    meaning (explicitly-disabled rich attributes such as the markdown code theme's
    ``underline=False`` would turn into pt ``underline``). Unrecognized tokens are
    still dropped -- missing styling beats crashing the render.
    """
    if style is None:
        return ""
    tokens: list[str] = []
    pending_bg = False
    pending_not = False
    for token in str(style).split():
        if token == "on":
            pending_bg = True
            continue
        if token == "not":
            pending_not = True
            continue
        if pending_bg:
            pending_bg = False
            translated = "bg:" + _pt_color(token)
        else:
            if token in _PT_ATTR_TOKENS:
                translated = "no" + token if pending_not else token
            else:
                translated = _pt_color(token)
        pending_not = False
        # Validate that the color token is one pt knows (hex / named / ansi name).
        if translated in _PT_ATTR_TOKENS or translated.startswith("bg:"):
            tokens.append(translated)
        else:
            try:
                parse_color(translated)
            except ValueError:
                continue
            tokens.append(translated)
    return " ".join(tokens)


def rich_markup_fragments(markup_text: str) -> list[StyleAndText]:
    """Rich markup text -> style fragment list (no ANSI involved)."""
    console = _get_offscreen_console()
    text = console.render_str(markup_text, markup=True, highlight=False)
    return [
        (_pt_style(segment.style), segment.text)
        for segment in console.render(text, console.options)
        if segment.text
    ]


# ---------------------------------------------------------------------------
# chunk -> fragment normalization entry point
# ---------------------------------------------------------------------------

def chunk_fragments(chunk: OutputChunk) -> list[StyleAndText]:
    """Normalize any chunk into style fragments -- the single conversion entry point
    for transcript / lexer."""
    if chunk.fragments:
        return list(chunk.fragments)
    if chunk.format == "rich":
        return rich_markup_fragments(chunk.text)
    if chunk.format == "markdown":
        return markdown_fragments(chunk.text)
    if chunk.text:
        return [("", chunk.text)]
    return []


# ---------------------------------------------------------------------------
# Markdown -> style fragments
# ---------------------------------------------------------------------------

_MD_CONSOLE_WIDTH = 100


def markdown_fragments(text: str) -> list[StyleAndText]:
    """Render markdown into pt-vocabulary style fragments (no ANSI round-trip).

    Uses rich.markdown.Markdown rendered on an offscreen console: headings, emphasis,
    tables and fenced code all come out as styled segments, converted through the
    same ``_pt_style`` vocabulary translation as ``rich_markup_fragments``. Width is
    fixed so transcript replay does not re-wrap per terminal -- the terminal wraps
    over-wide lines itself, as elsewhere in the commit path.
    """
    from rich.markdown import Markdown

    console = Console(
        file=StringIO(),
        width=_MD_CONSOLE_WIDTH,
        tab_size=4,
        highlight=False,
        legacy_windows=False,
    )
    segments = console.render(Markdown(text), console.options)
    out: list[StyleAndText] = []
    for segment in segments:
        if not segment.text:
            continue
        out.append((_pt_style(segment.style), segment.text))
    merged: list[StyleAndText] = []
    for style, part in out:
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + part)
        else:
            merged.append((style, part))
    return merged
