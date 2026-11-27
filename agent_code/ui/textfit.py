"""Terminal-width helpers shared by the banner, footer and hint widgets.

Rendering is measured in terminal cells, not code points: CJK glyphs, emoji and
combining marks all change the displayed width. The helpers here are ANSI-free and
work on the same ``(style, text)`` fragments the rest of the UI uses.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text

from ..output import StyleAndText


def clip_text(text: str, width: int) -> str:
    """Truncate ``text`` to at most ``width`` cells, marking the cut with an ellipsis."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    piece = Text(text)
    piece.truncate(width, overflow="ellipsis")
    return piece.plain


def clip_fragments(parts: Sequence[StyleAndText], width: int) -> list[StyleAndText]:
    """Clip a fragment row to ``width`` cells (styles preserved per piece)."""
    if width <= 0:
        return []
    kept: list[StyleAndText] = []
    used = 0
    for style, text in parts:
        room = width - used
        if room <= 0:
            break
        piece_width = cell_len(text)
        if piece_width <= room:
            kept.append((style, text))
            used += piece_width
            continue
        clipped = clip_text(text, room)
        kept.append((style, clipped))
        break
    return kept


def visible_width(parts: Sequence[StyleAndText]) -> int:
    """Display width of a fragment row."""
    return sum(cell_len(text) for _, text in parts)


def middle_truncate(text: str, max_cells: int) -> str:
    """Keep the first and last path components: ``D:/Dev/.../agent-code``."""
    if max_cells <= 0:
        return ""
    if cell_len(text) <= max_cells:
        return text
    sep = "\\" if "\\" in text and "/" not in text else "/"
    parts = [part for part in text.split(sep) if part]
    if len(parts) <= 2:
        return clip_text(text, max_cells)
    head, tail = parts[0], parts[-1]
    candidate = f"{head}{sep}…{sep}{tail}"
    if cell_len(candidate) <= max_cells:
        return candidate
    room = max_cells - cell_len(tail) - 2
    if room >= 2:
        return f"{clip_text(head, room)}{sep}…{sep}{tail}"
    return clip_text(tail, max_cells)


def display_path(path: Path) -> str:
    """Render ``path`` with the home directory collapsed to ``~``."""
    try:
        home = Path.home().resolve()
        rel = path.resolve().relative_to(home)
    except OSError, RuntimeError, ValueError:
        return str(path)
    text = rel.as_posix()
    return "~" if text == "." else f"~/{text}"
