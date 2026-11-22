"""REPL theme: the bottom status bar text and the prompt_toolkit stylesheet.

Small and cohesive on purpose -- colors and the status line live together, apart from
the layout-tree surgery in ``layout.py``, so a theme tweak never touches layout code.
"""

from __future__ import annotations

from prompt_toolkit.styles import Style

from ..context import ratio_bar
from ..output import StyleAndText
from ..runtime import RuntimeState


def _fmt_k(tokens: int) -> str:
    """Format a token count in k units: 131072 -> "131k", 950 -> "0.9k"."""
    if tokens >= 100_000 or tokens % 1000 == 0:
        return f"{tokens // 1000}k"
    return f"{tokens / 1000:.1f}k"


def bottom_toolbar(state: RuntimeState) -> list[StyleAndText]:
    """Bottom status bar: current mode + model + context usage.

    Returns Rich-vocabulary fragments (``StyleAndText``); the caller wraps them in
    ``FormattedText`` at the prompt_toolkit boundary. The context segment shows the
    live token estimate from ``RuntimeState.context_budget`` (kept across turns by
    the REPL); without a budget (one-shot mode) the segment is omitted entirely
    rather than rendering a misleading zero. At or above the budget's compact
    threshold the segment switches to the ``ctx.hot`` style as a warning.
    """
    mode = {
        "default": "default",
        "acceptEdits": "accept edits",
        "plan": "plan",
        "fullAccess": "full access",
    }.get(state.permission_mode, state.permission_mode)
    # Mode segment: tinted by how much power the mode grants, so a glance tells you
    # whether writes/auto-approval are on.
    mode_style = {
        "default": "class:bottom-toolbar class:mode.default",
        "acceptEdits": "class:bottom-toolbar class:mode.edits",
        "plan": "class:bottom-toolbar class:mode.plan",
        "fullAccess": "class:bottom-toolbar class:mode.full",
    }.get(state.permission_mode, "class:bottom-toolbar class:mode.default")
    fragments: list[StyleAndText] = [(mode_style, f" {mode}")]
    # Model segment: neutral cyan, distinct from both mode and context.
    fragments.append(("class:bottom-toolbar class:seg.model", f" · {state.model}"))
    budget = state.context_budget
    if budget is not None:
        # Compact bar (12 cells) so the toolbar stays one short line. At or above the
        # compact threshold the whole segment gets the warning style.
        style = "class:bottom-toolbar class:seg.ctx"
        if budget.should_compact():
            # Each class needs its own `class:` prefix: a bare `ctx.hot` token after
            # a space is parsed as an inline color and makes the whole fragment
            # style resolution raise (ValueError), dropping the toolbar styling.
            style = "class:bottom-toolbar class:ctx.hot"
        bar = ratio_bar(budget.usage_ratio, width=12)
        ctx = f" · {bar} {_fmt_k(budget.estimated_tokens)}/{_fmt_k(budget.window_tokens)} tokens"
        fragments.append((style, ctx))
    fragments.append(("class:bottom-toolbar", " "))
    return fragments


def build_style() -> Style:
    """REPL stylesheet: spinner / pending preview / status bar / Frame border."""
    return Style.from_dict(
        {
            "spinner": "bold ansibrightcyan",
            "preview": "ansiyellow",
            # pt's built-in defaults give `bottom-toolbar` `reverse` (SGR 7); reverse
            # swaps fg/bg and swallows the explicit colors below, leaving a plain
            # white-on-black-inverse bar. `noreverse` cancels it so the explicit
            # fg/bg (and the ctx.hot override) actually render.
            "bottom-toolbar": "noreverse bg:#222222 ansiwhite",
            # Mode segment tints, keyed by how much power the mode grants: cyan for
            # default, green for acceptEdits, amber for plan, red for fullAccess.
            "mode.default": "noreverse bg:#222222 ansicyan",
            "mode.edits": "noreverse bg:#222222 ansibrightgreen",
            "mode.plan": "noreverse bg:#222222 ansibrightyellow",
            "mode.full": "noreverse bg:#222222 ansibrightred",
            # Model segment: neutral magenta, distinct from mode and context.
            "seg.model": "noreverse bg:#222222 ansibrightmagenta",
            # Context segment: cool slate blue below the compact threshold.
            "seg.ctx": "noreverse bg:#222222 ansibrightblue",
            # Context-usage segment at/above the compact threshold: warning amber.
            "ctx.hot": "noreverse bg:#222222 ansibrightyellow",
            # Frame border: muted slate blue, understated but distinct from scrollback content.
            "frame.border": "#556677",
        }
    )
