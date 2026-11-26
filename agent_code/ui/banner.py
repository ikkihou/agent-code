"""Startup header with a small ASCII xiaolongbao logo.

Pi-style compact header: the mascot in the left gutter, then the wordmark, the
working directory, key hints and an optional base URL. No box -- the header reads as
plain text so it blends into the transcript and costs no side borders.

The chunk's ``text`` stays ANSI-free so transcripts and plain consumers are unaffected;
styling lives in ``fragments``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from rich.cells import cell_len

from .. import __version__
from ..output import OutputChunk, StyleAndText
from .textfit import clip_fragments, display_path, middle_truncate

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
_STYLE_PROJECT = "bold #4fd6ff"
_STYLE_MODEL = "bold #c678dd"
_STYLE_LABEL = "#8899aa"
_STYLE_VALUE = "default"
_STYLE_HINT_KEY = "bold #e5c07b"
_STYLE_HINT = "#8899aa"
_STYLE_NOISE = "dim"


def _columns() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def build_banner(cwd: Path, provider: str, model: str, base_url: str | None) -> OutputChunk:
    """Render the mascot beside startup info and key hints (fixed, clipped rows)."""
    columns = _columns()
    logo_width = max(cell_len(line) for line in _BAO)
    content_width = max(1, columns - logo_width - 2)

    path = middle_truncate(display_path(cwd), max(8, content_width))
    cut = max(path.rfind("/"), path.rfind("\\"))
    path_fragments: list[StyleAndText]
    if cut < 0:
        path_fragments = [(_STYLE_PROJECT, path)]
    else:
        path_fragments = [
            (_STYLE_LABEL, path[: cut + 1]),
            (_STYLE_PROJECT, path[cut + 1 :]),
        ]
    head: list[StyleAndText] = [
        (_STYLE_ACCENT, "agent-code"),
        (_STYLE_NOISE, "  "),
        (_STYLE_LABEL, f"v{__version__}"),
        (_STYLE_NOISE, "  ·  "),
        (_STYLE_LABEL, provider),
        (_STYLE_NOISE, "/"),
        (_STYLE_MODEL, model),
    ]
    hints: list[StyleAndText] = [
        (_STYLE_HINT_KEY, "/help"),
        (_STYLE_HINT, " commands"),
        (_STYLE_NOISE, " · "),
        (_STYLE_HINT_KEY, "/model"),
        (_STYLE_HINT, " switch"),
        (_STYLE_NOISE, " · "),
        (_STYLE_HINT_KEY, "/exit"),
        (_STYLE_HINT, " quit"),
        (_STYLE_NOISE, " · "),
        (_STYLE_HINT_KEY, "shift+tab"),
        (_STYLE_HINT, " mode"),
    ]

    rows: list[list[StyleAndText]] = [
        head,
        path_fragments,
        hints,
    ]
    if base_url:
        rows.append(
            [
                (_STYLE_LABEL, "base_url "),
                (_STYLE_VALUE, base_url),
            ]
        )

    fragments: list[StyleAndText] = []
    for index, row in enumerate(rows):
        if index < len(_BAO):
            fragments.append((_STYLE_ACCENT, _BAO[index] + "  "))
        else:
            fragments.append(("", " " * (logo_width + 2)))
        fragments.extend(clip_fragments(row, content_width))
        fragments.append(("", "\n"))
    fragments.append(("", "\n"))

    text = "".join(piece for _, piece in fragments)
    return OutputChunk(text=text, fragments=tuple(fragments))
