"""Bottom status-bar text (permission mode · model · context usage).

The fragment styles carry the REPL's class tokens (``class:mode.*`` / ``class:seg.*`` /
``class:ctx.hot``); the frontend maps those tokens to concrete colors.
"""

from __future__ import annotations

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

    Returns Rich-vocabulary fragments whose styles are class tokens the frontend maps
    to colors. The context segment shows the live token estimate from
    ``RuntimeState.context_budget`` (kept across turns by the REPL); without a budget
    the segment is omitted rather than rendering a misleading zero. At or above the
    budget's compact threshold the segment switches to ``ctx.hot``.
    """
    mode = {
        "default": "default",
        "acceptEdits": "accept edits",
        "plan": "plan",
        "fullAccess": "full access",
    }.get(state.permission_mode, state.permission_mode)
    mode_style = {
        "default": "class:bottom-toolbar class:mode.default",
        "acceptEdits": "class:bottom-toolbar class:mode.edits",
        "plan": "class:bottom-toolbar class:mode.plan",
        "fullAccess": "class:bottom-toolbar class:mode.full",
    }.get(state.permission_mode, "class:bottom-toolbar class:mode.default")
    fragments: list[StyleAndText] = [(mode_style, f" {mode}")]
    fragments.append(("class:bottom-toolbar class:seg.model", f" · {state.model}"))
    budget = state.context_budget
    if budget is not None:
        style = "class:bottom-toolbar class:seg.ctx"
        if budget.should_compact():
            style = "class:bottom-toolbar class:ctx.hot"
        bar = ratio_bar(budget.usage_ratio, width=12)
        ctx = f" · {bar} {_fmt_k(budget.estimated_tokens)}/{_fmt_k(budget.window_tokens)} tokens"
        fragments.append((style, ctx))
    fragments.append(("class:bottom-toolbar", " "))
    return fragments
