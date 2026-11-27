"""Single source of truth for the agent REPL's colors.

Two consumers, one definition:

* Python renderers (``render.py``, ``banner.py``, ``events.py``, ``widgets.py``,
  ``app.py``) import :data:`PALETTE` and build Rich inline style strings from its
  values.
* The stylesheet (``styles/default.tcss``) reads the same colors as CSS variables,
  supplied by :data:`AGENT_THEME` when ``AgentApp`` registers the theme. The ``.tcss``
  file therefore contains no hex literals at all.

The ramp is deliberately narrow: one slate family for chrome, one sky accent for
identity, a soft violet that only ever marks the model name, and three desaturated
semantics. Adding a color means adding a field here -- not inlining a hex at the call
site, which is how the old UI accumulated five barely-different blues and four
competing accents.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from textual.theme import Theme

THEME_NAME = "agent-code"


@dataclass(frozen=True)
class Palette:
    """The palette ramp. Field names double as CSS variable names in ``.tcss``."""

    # Chrome: background, surfaces and hairlines, one hue family.
    bg: str = "#11141a"
    surface: str = "#161a21"
    panel: str = "#1b2028"
    line: str = "#242b35"
    line_strong: str = "#333d4b"

    # Text: three weights, brightest to quietest.
    fg: str = "#c9d1dc"
    muted: str = "#7b8694"
    faint: str = "#59626e"

    # Identity accent plus the one supporting hue (model name only).
    accent: str = "#79b8d8"
    accent_alt: str = "#a394cc"

    # Semantics: muted enough to sit beside the accent without vibrating.
    success: str = "#83b98d"
    warning: str = "#d2ae72"
    error: str = "#d97b73"
    info: str = "#8ea7c4"

    # Filled-badge text and the selection highlight.
    on_accent: str = "#0d1014"
    selection: str = "#243447"
    echo: str = "#2c3542"

    def css_variables(self) -> dict[str, str]:
        """Custom CSS variables for the colors Textual's own theme slots don't cover.

        ``$foreground``/``$background``/``$surface``/``$panel``/``$primary``/
        ``$secondary``/``$accent``/``$success``/``$warning``/``$error`` come straight
        from the ``Theme`` constructor; everything here is extra.
        """
        variables = asdict(self)
        for key in ("bg", "fg", "accent", "accent_alt", "success", "warning", "error"):
            variables.pop(key, None)
        # ``line_strong`` -> ``$line-strong``: CSS variable names use hyphens.
        return {key.replace("_", "-"): value for key, value in variables.items()}


PALETTE = Palette()

AGENT_THEME = Theme(
    name=THEME_NAME,
    primary=PALETTE.accent,
    secondary=PALETTE.accent_alt,
    accent=PALETTE.accent,
    foreground=PALETTE.fg,
    background=PALETTE.bg,
    surface=PALETTE.surface,
    panel=PALETTE.panel,
    success=PALETTE.success,
    warning=PALETTE.warning,
    error=PALETTE.error,
    dark=True,
    variables=PALETTE.css_variables(),
)
