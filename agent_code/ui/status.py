"""Bottom status-bar text (permission mode · cwd · git · context · model).

The fragment styles carry the REPL's class tokens (``class:mode.*`` / ``class:seg.*`` /
``class:ctx.*``); the frontend maps those tokens to concrete colors and the widget
paints the row background.

Layout follows Pi's footer: identity on the left, context usage in the middle and the
model right-aligned when the terminal is wide enough.
"""

from __future__ import annotations

from pathlib import Path

from ..context import ratio_bar
from ..output import StyleAndText
from ..runtime import RuntimeState
from .gitinfo import GitInfo
from .textfit import (
    clip_fragments,
    display_path,
    middle_truncate,
    visible_width,
)

_MODE_LABELS = {
    "default": "default",
    "acceptEdits": "accept edits",
    "plan": "plan",
    "fullAccess": "full access",
}

_MODE_TOKENS = {
    "default": "mode.default",
    "acceptEdits": "mode.edits",
    "plan": "mode.plan",
    "fullAccess": "mode.full",
}

_CWD_MAX_CELLS = 40


def _fmt_k(tokens: int) -> str:
    """Format a token count compactly: 131072 -> "131K", 950 -> "0.9K"."""
    if tokens >= 100_000 or tokens % 1000 == 0:
        return f"{tokens // 1000}K"
    return f"{tokens / 1000:.1f}K"


def _style(token: str) -> str:
    return f"class:bottom-toolbar class:{token}"


def _cwd_fragments(cwd: Path) -> list[StyleAndText]:
    text = middle_truncate(display_path(cwd), _CWD_MAX_CELLS)
    cut = max(text.rfind("/"), text.rfind("\\"))
    if cut < 0:
        return [(_style("seg.project"), text)]
    prefix, name = text[: cut + 1], text[cut + 1 :]
    fragments: list[StyleAndText] = [(_style("seg.cwd"), prefix)]
    if name:
        fragments.append((_style("seg.project"), name))
    return fragments


def _context_fragments(state: RuntimeState) -> list[StyleAndText]:
    budget = state.context_budget
    if budget is None:
        return []
    ratio = budget.usage_ratio
    if ratio >= 0.9:
        token = "ctx.critical"
    elif budget.should_compact() or ratio >= 0.7:
        token = "ctx.hot"
    else:
        token = "seg.ctx"
    bar = ratio_bar(ratio, width=10)
    return [(_style(token), f"  {bar} ({_fmt_k(budget.window_tokens)})")]


def _model_fragments(state: RuntimeState) -> list[StyleAndText]:
    fragments: list[StyleAndText] = []
    if state.provider:
        fragments.append((_style("seg.provider"), state.provider))
        fragments.append((_style("seg.sep"), "/"))
    fragments.append((_style("seg.model"), state.model))
    return fragments


def _align(
    left: list[StyleAndText], right: list[StyleAndText], width: int
) -> list[StyleAndText]:
    if width <= 0:
        return [*left, (_style("seg.gap"), "  "), *right]

    left_width = visible_width(left)
    right_width = visible_width(right)
    if left_width >= width:
        return clip_fragments(left, width)

    gap = width - left_width - right_width
    if gap >= 1:
        return [*left, (_style("seg.gap"), " " * gap), *right]

    room = width - left_width - 1
    if room <= 0:
        return left
    return [*left, (_style("seg.gap"), " "), *clip_fragments(right, room)]


def bottom_toolbar(
    state: RuntimeState,
    *,
    cwd: Path | None = None,
    git: GitInfo | None = None,
    width: int = 0,
) -> list[StyleAndText]:
    """Bottom status bar: mode, cwd, git, context usage and right-aligned model."""
    mode_key = state.permission_mode if state.permission_mode in _MODE_LABELS else "default"
    mode = _MODE_LABELS[mode_key]
    mode_token = _MODE_TOKENS[mode_key]

    left: list[StyleAndText] = [(_style(mode_token), f" {mode} ")]
    if cwd is not None:
        left.append((_style("seg.sep"), " "))
        left.extend(_cwd_fragments(cwd))
    if git is not None and git.branch:
        left.append((_style("seg.branch"), f"  git:{git.branch}"))
        if git.dirty:
            left.append((_style("seg.branch.dirty"), "*"))
    left.extend(_context_fragments(state))

    return _align(left, _model_fragments(state), width)
