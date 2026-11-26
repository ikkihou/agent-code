"""Startup info box with a small ASCII xiaolongbao logo.

The bao, startup info, and hint are rendered together inside one rounded box. The
chunk's ``text`` stays ANSI-free so transcripts and plain consumers are unaffected.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text

from ..output import OutputChunk, StyleAndText

# ---------------------------------------------------------------------------
# Logo art
# ---------------------------------------------------------------------------

_BAO = (
    "  .-^-.",
    " /     \\",
    " \\_____/",
)

# ---------------------------------------------------------------------------
# Palette (Rich inline vocabulary, same as the rest of the UI)
# ---------------------------------------------------------------------------

_STYLE_ACCENT = "bold #4fd6ff"
_STYLE_BORDER = "#556677"
_STYLE_LABEL = "#8899aa"
_STYLE_VALUE = "default"
_STYLE_HINT = "#8899aa"
_STYLE_NOISE = "dim"


def _columns() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def _row(parts: list[StyleAndText]) -> list[StyleAndText]:
    return [part for part in parts if part[1]]


def _row_width(parts: list[StyleAndText]) -> int:
    return sum(cell_len(text) for _, text in parts)


def _clip(parts: list[StyleAndText], width: int) -> list[StyleAndText]:
    """Truncate a row to ``width`` cells, marking the cut with an ellipsis."""
    if _row_width(parts) <= width:
        return parts
    kept: list[StyleAndText] = []
    used = 0
    for style, text in parts:
        room = width - used
        if room <= 0:
            break
        if cell_len(text) <= room:
            kept.append((style, text))
            used += cell_len(text)
            continue
        # Clip through Rich rather than slicing code points: a cut CJK glyph would
        # still occupy two cells and break the right border. Rich appends the ellipsis.
        piece = Text(text)
        piece.truncate(room, overflow="ellipsis")
        kept.append((style, piece.plain))
        break
    return kept


def _pad(parts: list[StyleAndText], width: int) -> list[StyleAndText]:
    """Right-pad one interior line to ``width`` display cells."""
    used = _row_width(parts)
    return [*parts, ("", " " * (width - used))] if used < width else parts


def _box(rows: list[list[StyleAndText]], columns: int) -> list[StyleAndText]:
    """Wrap pre-styled content rows in a rounded box clamped to ``columns``."""
    inner = min(max(_row_width(row) for row in rows), max(columns - 4, 1))
    horizontal = "─" * (inner + 2)
    fragments: list[StyleAndText] = [(_STYLE_BORDER, f"╭{horizontal}╮\n")]
    for row in rows:
        fragments.append((_STYLE_BORDER, "│ "))
        fragments.extend(_pad(_clip(_row(row), inner), inner))
        fragments.append((_STYLE_BORDER, " │\n"))
    fragments.append((_STYLE_BORDER, f"╰{horizontal}╯\n"))
    return fragments


def build_banner(cwd: Path, provider: str, model: str, base_url: str | None) -> OutputChunk:
    """Render the bao logo beside startup info and hint in a rounded box."""
    columns = _columns()

    info: list[list[StyleAndText]] = [
        [(_STYLE_LABEL, "cwd: "), (_STYLE_VALUE, str(cwd))],
        [
            (_STYLE_LABEL, "provider: "),
            (_STYLE_VALUE, provider),
            (_STYLE_NOISE, " · "),
            (_STYLE_LABEL, "model: "),
            (_STYLE_VALUE, model),
        ],
    ]
    if base_url:
        info.append([(_STYLE_LABEL, "base_url: "), (_STYLE_VALUE, base_url)])
    hint: list[StyleAndText] = [
        (_STYLE_HINT, "/help"),
        (_STYLE_NOISE, " for help · "),
        (_STYLE_HINT, "/model"),
        (_STYLE_NOISE, " to switch · "),
        (_STYLE_HINT, "/exit"),
        (_STYLE_NOISE, " to quit"),
    ]

    content_rows = [*info, hint]
    logo_width = max(cell_len(line) for line in _BAO)
    rows: list[list[StyleAndText]] = []
    for index in range(max(len(_BAO), len(content_rows))):
        bao = _BAO[index] if index < len(_BAO) else " " * logo_width
        content = content_rows[index] if index < len(content_rows) else []
        rows.append([(_STYLE_ACCENT, bao.ljust(logo_width) + "  "), *content])

    fragments: list[StyleAndText] = []
    fragments.extend(_box(rows, columns))

    text = "".join(piece for _, piece in fragments)
    return OutputChunk(text=text, fragments=tuple(fragments))
