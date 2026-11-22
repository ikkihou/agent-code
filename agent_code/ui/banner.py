"""Startup banner: block-letter logo + a rounded info box.

Claude-Code-flavoured opener. Two logo sizes -- the full 6-line ANSI Shadow art needs
79 columns, so narrow terminals fall back to a 2-line block-letter logo; a wrapped
logo reads as a rendering bug, and a wrapped box loses its right border.

The banner is one plain ``OutputChunk`` carrying explicit ``fragments``: the logo gets
a per-column gradient (computed here rather than in Rich markup -- markup would need
one tag per color band per line), the box borders are dim, the labels tinted. The
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

# ANSI Shadow, butt-joined (the glyphs carry their own inter-letter spacing).
# 79 columns: "AGENT" 44 + 2-space word gap + "CODE" 33.
_LOGO_WIDE = (
    " █████╗  ██████╗ ███████╗███╗   ██╗████████╗   ██████╗ ██████╗ ██████╗ ███████╗",
    "██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝  ██╔════╝██╔═══██╗██╔══██╗██╔════╝",
    "███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║     ██║     ██║   ██║██║  ██║█████╗  ",
    "██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║     ██║     ██║   ██║██║  ██║██╔══╝  ",
    "██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║     ╚██████╗╚██████╔╝██████╔╝███████╗",
    "╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝      ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝",
)

# 2-line block letters (3 columns each, 1 column gap): 38 columns, fits any terminal.
_LOGO_COMPACT = (
    "▄▀█ █▀▀ █▀▀ █▄░█ ▀█▀   █▀▀ █▀█ █▀▄ █▀▀",
    "█▀█ █▄█ ██▄ █░▀█ ░█░   █▄▄ █▄█ █▄▀ ██▄",
)

# Logo plus a little breathing room on the right (never let it touch the edge).
_LOGO_WIDE_MIN_COLUMNS = cell_len(_LOGO_WIDE[0]) + 2
# Below this the box cannot hold a useful path line; fall back to bare lines.
_BOX_MIN_COLUMNS = 46

# Horizontal gradient endpoints for the logo (cyan -> violet, matching the frame accent).
_LOGO_FROM = (0x4F, 0xD6, 0xFF)
_LOGO_TO = (0xA7, 0x8B, 0xFA)

# ---------------------------------------------------------------------------
# Palette (Rich inline vocabulary, same as the rest of the UI)
# ---------------------------------------------------------------------------

_STYLE_WELCOME = "bold"
_STYLE_ACCENT = "bold #4fd6ff"
_STYLE_BORDER = "#556677"
_STYLE_LABEL = "#8899aa"
_STYLE_VALUE = "default"
_STYLE_HINT = "#8899aa"
_STYLE_NOISE = "dim"


def _hex_color(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _lerp(start: tuple[int, int, int], end: tuple[int, int, int], t: float) -> str:
    """Interpolate two RGB tuples at ``t`` in [0, 1] and format as a hex color."""
    t = min(max(t, 0.0), 1.0)
    return _hex_color(
        (
            round(start[0] + (end[0] - start[0]) * t),
            round(start[1] + (end[1] - start[1]) * t),
            round(start[2] + (end[2] - start[2]) * t),
        )
    )


def _logo_fragments(lines: tuple[str, ...], bands: int = 24) -> list[StyleAndText]:
    """Logo art -> fragments with a left-to-right color gradient.

    Each line is cut into ``bands`` color steps and every step becomes one fragment:
    per-character spans would emit hundreds of tags for a gradient no eye can resolve.
    """
    width = max(cell_len(line) for line in lines) or 1
    step = max(1, width // bands)
    fragments: list[StyleAndText] = []
    for line in lines:
        for start in range(0, len(line), step):
            style = f"bold {_lerp(_LOGO_FROM, _LOGO_TO, start / max(width - 1, 1))}"
            fragments.append((style, line[start : start + step]))
        fragments.append(("", "\n"))
    return fragments


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
    inner = max(_row_width(row) for row in rows)
    inner = min(inner, max(columns - 4, 20))  # 2 borders + 1 space of padding each side
    horizontal = "─" * (inner + 2)
    fragments: list[StyleAndText] = [(_STYLE_BORDER, f"╭{horizontal}╮\n")]
    for row in rows:
        fragments.append((_STYLE_BORDER, "│ "))
        fragments.extend(_pad(_clip(_row(row), inner), inner))
        fragments.append((_STYLE_BORDER, " │\n"))
    fragments.append((_STYLE_BORDER, f"╰{horizontal}╯\n"))
    return fragments


def build_banner(cwd: Path, provider: str, model: str, base_url: str | None) -> OutputChunk:
    """The startup banner: gradient logo above a rounded welcome/info box."""
    columns = _columns()
    logo = _LOGO_WIDE if columns >= _LOGO_WIDE_MIN_COLUMNS else _LOGO_COMPACT

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

    fragments: list[StyleAndText] = [("", "\n")]
    fragments.extend(_logo_fragments(logo))
    fragments.append(("", "\n"))
    if columns >= _BOX_MIN_COLUMNS:
        rows: list[list[StyleAndText]] = [
            [(_STYLE_ACCENT, "✻ "), (_STYLE_WELCOME, "Welcome to Agent Code!")],
            [],
            *info,
            [],
            hint,
        ]
        fragments.extend(_box(rows, columns))
    else:
        # No room for a box: bare lines carry the same information, clipped per line.
        lines: list[list[StyleAndText]] = [
            [(_STYLE_ACCENT, "✻ "), (_STYLE_WELCOME, "Welcome to Agent Code!")],
            *info,
            hint,
        ]
        for row in lines:
            fragments.extend(_clip(_row(row), columns))
            fragments.append(("", "\n"))
    fragments.append(("", "\n"))

    text = "".join(piece for _, piece in fragments)
    return OutputChunk(text=text, fragments=tuple(fragments))
