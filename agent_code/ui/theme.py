"""REPL theme: the bottom status bar text and the prompt_toolkit stylesheet.

Small and cohesive on purpose -- colors and the status line live together, apart from
the layout-tree surgery in ``layout.py``, so a theme tweak never touches layout code.
"""

from __future__ import annotations

from prompt_toolkit.styles import Style

from ..runtime import RuntimeState


def bottom_toolbar(state: RuntimeState) -> str:
    """Bottom status bar: current mode + model."""
    mode = {
        "default": "default",
        "acceptEdits": "accept edits",
        "plan": "plan",
        "fullAccess": "full access",
    }.get(state.permission_mode, state.permission_mode)
    return f" {mode} · {state.model} "


def build_style() -> Style:
    """REPL stylesheet: spinner / pending preview / status bar / Frame border."""
    return Style.from_dict(
        {
            "spinner": "bold ansibrightcyan",
            "preview": "ansiyellow",
            "bottom-toolbar": "bg:#222222 ansiwhite",
            # Frame border: muted slate blue, understated but distinct from scrollback content.
            "frame.border": "#556677",
        }
    )
