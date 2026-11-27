"""Rendering of the agent's display units (OutputChunk / LiveArea) into Rich ``Text``.

The Textual frontend draws into widgets, so the commit path produces Rich ``Text``
renderables instead of ANSI. ``output.py`` owns the fragment pipeline (chunk -> style
fragments, markdown rendering, full-width padding); this module wraps those fragments
in a ``rich.text.Text`` and maps the REPL's status/live class names to concrete colors.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from ..output import OutputChunk, StyleAndText, chunk_fragments, pad_full_width
from ..runtime import RuntimeState
from .gitinfo import GitInfo
from .live import LiveArea
from .status import bottom_toolbar
from .theme import PALETTE

# Spinner / pending-preview colors, expressed in Rich inline vocabulary.
SPINNER_STYLE = f"bold {PALETTE.accent}"
PREVIEW_STYLE = PALETTE.warning


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


def _preview_line_style(line: str) -> str:
    """Color unified diff lines inside a confirmation preview; keep amber for other previews."""
    if line.startswith("---") or line.startswith("+++"):
        return f"bold {PALETTE.fg}"
    if line.startswith("+"):
        return PALETTE.success
    if line.startswith("-"):
        return PALETTE.error
    if line.startswith("@@"):
        return PALETTE.accent
    return PREVIEW_STYLE


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
            text.append(piece, style=_preview_line_style(piece))
        else:
            text.append(piece)
    return text


# Status-bar segment colors, keyed by the class token that ``bottom_toolbar`` emits.
# The bar background comes from the widget's CSS (``#status`` -> ``$surface``), so
# fragments only set the foreground; leaving the background unset lets the widget paint
# the whole row uniformly, including the empty space after the last segment. Mode badges
# are the exception: they paint their own filled pill, so they set both.
_STATUS_COLORS: dict[str, str] = {
    "mode.default": f"bold {PALETTE.on_accent} on {PALETTE.accent}",
    "mode.edits": f"bold {PALETTE.on_accent} on {PALETTE.success}",
    "mode.plan": f"bold {PALETTE.on_accent} on {PALETTE.warning}",
    "mode.full": f"bold {PALETTE.on_accent} on {PALETTE.error}",
    "seg.branch.dirty": PALETTE.warning,
    "seg.branch": PALETTE.success,
    "seg.project": f"bold {PALETTE.accent}",
    "seg.cwd": PALETTE.faint,
    "seg.model": PALETTE.accent_alt,
    "seg.provider": PALETTE.muted,
    "seg.ctx": PALETTE.info,
    "ctx.hot": PALETTE.warning,
    "ctx.critical": PALETTE.error,
}
_STATUS_FG = PALETTE.fg


def render_status(
    state: RuntimeState,
    *,
    cwd: Path | None = None,
    git: GitInfo | None = None,
    width: int = 0,
) -> Text:
    """Bottom status bar as Rich Text.

    ``width`` enables right-aligning the model; the widget passes its laid-out width.
    """
    fragments: list[StyleAndText] = bottom_toolbar(state, cwd=cwd, git=git, width=width)
    text = Text()
    for style, piece in fragments:
        color = _STATUS_FG
        for token, token_color in _STATUS_COLORS.items():
            if token in style:
                color = token_color
                break
        text.append(piece, style=color)
    return text
