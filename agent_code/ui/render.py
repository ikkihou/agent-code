"""Rendering of the agent's display units (OutputChunk / LiveArea) into Rich ``Text``.

The Textual frontend draws into widgets, so the commit path produces Rich ``Text``
renderables instead of ANSI. ``output.py`` owns the fragment pipeline (chunk -> style
fragments, markdown rendering, full-width padding); this module wraps those fragments
in a ``rich.text.Text`` and maps the REPL's status/live class names to concrete colors.
"""

from __future__ import annotations

from rich.text import Text

from ..output import OutputChunk, StyleAndText, chunk_fragments, pad_full_width
from ..runtime import RuntimeState
from .live import LiveArea
from .status import bottom_toolbar

# Spinner / pending-preview colors, expressed in Rich inline vocabulary.
SPINNER_STYLE = "bold #4fd6ff"
PREVIEW_STYLE = "#e5c07b"


def chunk_to_text(chunk: OutputChunk, width: int | None = None) -> Text:
    """OutputChunk -> Rich Text, reusing the shared fragment/padding pipeline.

    ``width`` is the markdown wrap target and the ``full_width`` padding target; callers
    pass the log widget's current content width.
    """
    fragments = chunk_fragments(chunk, width=width)
    if chunk.full_width and width is not None:
        fragments = pad_full_width(fragments, width)
    text = Text()
    for style, piece in fragments:
        text.append(piece, style=style)
    return text


def render_live(live: LiveArea, columns: int, now: float) -> Text:
    """LiveArea -> Rich Text for the in-progress panel (spinner / preview / streaming tail).

    Reuses ``LiveArea.formatted`` (the tested fragment producer) and just maps the
    ``class:spinner`` / ``class:preview`` markers to Rich styles.
    """
    fragments: list[StyleAndText] = live.formatted(columns, now)
    text = Text()
    for style, piece in fragments:
        if "spinner" in style:
            text.append(piece, style=SPINNER_STYLE)
        elif "preview" in style:
            text.append(piece, style=PREVIEW_STYLE)
        else:
            text.append(piece)
    return text


# Status-bar segment colors, keyed by the class token that ``bottom_toolbar`` emits
# (mirrors ``ui/theme.py::build_style``). The bar background comes from the widget's
# CSS (``#status`` -> ``$surface``), so fragments only set the foreground; leaving the
# background unset lets the widget paint the whole row uniformly, including the empty
# space after the last segment.
_STATUS_COLORS: dict[str, str] = {
    "mode.default": "#4fd6ff",
    "mode.edits": "#7ee787",
    "mode.plan": "#e5c07b",
    "mode.full": "#ff6b6b",
    "seg.model": "#c678dd",
    "seg.ctx": "#61afef",
    "ctx.hot": "#e5c07b",
}
_STATUS_FG = "#e5e5e5"


def render_status(state: RuntimeState) -> Text:
    """Bottom status bar (mode · model · context) as Rich Text."""
    fragments: list[StyleAndText] = bottom_toolbar(state)
    text = Text()
    for style, piece in fragments:
        color = _STATUS_FG
        for token, token_color in _STATUS_COLORS.items():
            if token in style:
                color = token_color
                break
        text.append(piece, style=color)
    return text
