"""Canonical representation of agent output plus style rendering.

OutputChunk is the single static-text output unit shared by every frontend (one-shot
direct printing / REPL scrollback+Live / session transcript persistence): plain text
plus optional style fragments. ``TranscriptItem`` is the ordered render union -- a
static ``OutputChunk`` plus ``ThinkingItem`` / ``ToolCallItem`` -- shared by the
frontends and the structured transcript sidecar (so a resume can rebuild the
collapsible thinking/tool blocks). Fragments use Rich inline style strings
("bold white on #444444") directly -- the output path never goes through ANSI:
Rich only parses markup/markdown into styled segments, and each frontend renders
the resulting fragments itself.
"""

# here put the import lib
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from io import StringIO
from typing import Callable, Literal

from rich.cells import cell_len
from rich.console import Console

OutputFormat = Literal["plain", "rich", "markdown"]

# (style string, text) fragments; the style string uses Rich's inline style
# vocabulary, e.g. "bold white on #444444".
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


@dataclass(frozen=True)
class ThinkingItem:
    """One contiguous extended-thinking block (a frontend renders it collapsed).

    ``duration`` is how long the model spent thinking (seconds), measured from the
    producer's delta timestamps; 0.0 when unknown.
    """

    text: str
    duration: float = 0.0


@dataclass(frozen=True)
class ToolCallItem:
    """One finished tool call (a frontend renders it collapsed, expandable to detail).

    ``args_preview`` is the short single-line preview used in the title; ``args_full``
    and ``result`` are the bounded detail shown when the block is expanded.
    """

    name: str
    args_preview: str
    status: str  # "ok" | "error" | "declined" | "skipped"
    args_full: str = ""
    result: str = ""


# A render item: static text, a thinking block, or a finished tool call. Shared by the
# frontends and by the transcript sidecar (which persists the structure so a resume can
# rebuild the collapsible blocks).
TranscriptItem = OutputChunk | ThinkingItem | ToolCallItem


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


def _rich_fragment_style(style: object) -> str:
    """rich Style object -> Rich inline style string (the fragment vocabulary).

    ``str(Style)`` round-trips through ``Style.parse`` for every form Rich emits
    (including "not bold" negations, "color(99)" and link styles), so the object's
    string form is used verbatim. ``None`` and the "none" spelling both mean "no
    styling" and become an empty string.
    """
    if style is None:
        return ""
    text = str(style)
    return "" if text == "none" else text


def rich_markup_fragments(markup_text: str) -> list[StyleAndText]:
    """Rich markup text -> style fragment list (no ANSI involved)."""
    console = _get_offscreen_console()
    text = console.render_str(markup_text, markup=True, highlight=False)
    return [
        (_rich_fragment_style(segment.style), segment.text)
        for segment in console.render(text, console.options)
        if segment.text
    ]


# ---------------------------------------------------------------------------
# chunk -> fragment normalization
# ---------------------------------------------------------------------------


def chunk_fragments(chunk: OutputChunk, width: int | None = None) -> list[StyleAndText]:
    """Normalize any chunk into style fragments -- the single conversion entry point
    for transcript / commit path. Fragment styles follow Rich's inline vocabulary.

    ``width`` is the wrap target for markdown chunks: the frontend passes the terminal
    width it is about to render into, so prose wraps where the terminal wraps. Callers
    without a terminal (tests, transcript inspection) leave it out.
    """
    if chunk.fragments:
        return list(chunk.fragments)
    if chunk.format == "rich":
        return rich_markup_fragments(chunk.text)
    if chunk.format == "markdown":
        return markdown_fragments(chunk.text, width=width)
    if chunk.text:
        return [("", chunk.text)]
    return []


def pad_full_width(fragments: Iterable[StyleAndText], columns: int) -> list[StyleAndText]:
    """Pad each *visual line* to ``columns`` display cells.

    Width is accumulated across all fragments (a line may be split into several
    styled pieces), and padding is emitted only when the line ends at ``\n``. Tabs are
    expanded before measuring because ``cell_len("\t")`` is 1 while terminals expand
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
                line_width += cell_len(segment)
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
            line_width += cell_len(tail)
            line_has_text = True
            if style:
                line_style = style

    if line_has_text and line_style and line_width < columns:
        emit(line_style, " " * (columns - line_width))
    return padded


# ---------------------------------------------------------------------------
# Markdown -> style fragments
# ---------------------------------------------------------------------------

# Fallback wrap width for markdown when no terminal width is known (tests, transcript
# tooling). The REPL commit path always passes the live terminal width instead: the
# renderer hard-wraps, so a width baked in here would make prose stop short of (or
# overshoot) the terminal's right edge.
_MD_DEFAULT_WIDTH = 100


def markdown_fragments(text: str, width: int | None = None) -> list[StyleAndText]:
    """Render markdown into Rich-vocabulary style fragments (no ANSI round-trip).

    Uses rich.markdown.Markdown rendered on an offscreen console: headings, emphasis,
    tables and fenced code all come out as styled segments whose ``str(Style)`` form
    is used verbatim as the fragment style.

    ``width`` is the column count to wrap at -- pass the terminal width at commit time
    so paragraphs wrap where the terminal actually wraps. Rich hard-wraps (paragraph
    fill, table sizing, code fences), so this width cannot be fixed for every
    terminal; transcripts store the markdown source, never the rendered fragments, so
    a resume re-renders at the width of the terminal doing the replay.
    """
    from rich.markdown import Markdown

    wrap_width = width if width is not None and width > 0 else _MD_DEFAULT_WIDTH
    console = Console(
        file=StringIO(),
        width=wrap_width,
        tab_size=4,
        highlight=False,
        legacy_windows=False,
    )
    segments = console.render(Markdown(text), console.options)
    out: list[StyleAndText] = []
    for segment in segments:
        if not segment.text:
            continue
        out.append((_rich_fragment_style(segment.style), segment.text))
    merged: list[StyleAndText] = []
    for style, part in out:
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + part)
        else:
            merged.append((style, part))
    return merged
