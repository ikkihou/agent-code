"""Token-ratio context tracking: usage accounting, budget math and window resolution.

The agent loop does not own a tokenizer. Instead it observes the ``usage`` report that
every Anthropic-compatible response already carries (the authoritative number for the
*previous* request) and falls back to a chars/4 estimate for messages appended since
that report. Compaction is then driven by the ratio of the estimate to the model's
context window, not by a message count.

This module stays free of provider and UI imports so it is trivially testable and can
be imported from the model layer without a cycle.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Compact when the estimate plus the response reserve reaches 80% of the window, and
# compact down to about 55% of the window.
DEFAULT_COMPACT_THRESHOLD = 0.80
DEFAULT_TARGET_RATIO = 0.55
# Room the next response needs; the window is not usable for input up to the brim.
DEFAULT_RESERVE_TOKENS = 4096

# Known context windows. DeepSeek's Anthropic-compatible endpoint currently serves the
# same 128K window as Anthropic's own models.
PROVIDER_WINDOWS: dict[str, int] = {
    "anthropic": 131072,
    "deepseek": 131072,
    "mock": 8192,
}
FALLBACK_WINDOW = 131072


@dataclass(frozen=True)
class TokenUsage:
    """Token counts reported by one completed API response."""

    input_tokens: int
    cache_read_tokens: int = 0
    output_tokens: int = 0

    @property
    def effective_context(self) -> int:
        """Tokens of context the *next* request inherits from this one.

        Anthropic's ``input_tokens`` already includes cache reads, so adding
        ``cache_read_tokens`` unconditionally would double-count. Providers that
        report cache reads separately leave ``input_tokens`` at zero, which is the
        only case where the cache count is used as a fallback.
        """
        if self.input_tokens:
            return self.input_tokens
        return self.cache_read_tokens


class ContextBudget:
    """Tracks context usage against the model window and decides when to compact.

    ``estimated_tokens`` is a heuristic: it is reset from the assembled message list at
    the start of every run, replaced by the authoritative usage report after each
    response, and grown by a chars/4 estimate of messages appended since that report.
    """

    def __init__(
        self,
        window_tokens: int,
        compact_threshold: float = DEFAULT_COMPACT_THRESHOLD,
        target_ratio: float = DEFAULT_TARGET_RATIO,
        reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
    ) -> None:
        self.window_tokens = max(1, int(window_tokens))
        self.compact_threshold = compact_threshold
        self.target_ratio = target_ratio
        self.reserve_tokens = max(0, int(reserve_tokens))
        self.estimated_tokens = 0
        # Last compaction stats, rendered by /context and the wire event.
        self.compactions = 0
        self.last_before_ratio = 0.0
        self.last_after_ratio = 0.0
        self.last_summarized_messages = 0

    def observe(self, usage: TokenUsage | None) -> None:
        """Replace the estimate with the authoritative number from a response.

        A missing usage report is not an error: the previous estimate stays in place
        and the loop keeps working (invariant: usage is always ``None``-safe).

        Replace (not max) is deliberate: after a compaction the next report is lower
        than the pre-compaction estimate, and a floor would immediately re-trigger
        compaction forever. The report is authoritative for the request it describes.
        """
        if usage is None:
            return
        self.estimated_tokens = max(0, usage.effective_context + usage.output_tokens)

    def reset(self, tokens: int) -> None:
        """(Re)initialize the estimate from the messages about to be sent.

        Called at the start of every run: the assembled message list is exactly what
        the next request carries, so assignment (not a max-floor) cannot double-count.
        It also refreshes a budget reused across turns -- compaction rewrites only the
        local list while the session file stays append-only, so the small
        post-compaction estimate must not mask the full history reloaded next turn.
        """
        self.estimated_tokens = max(0, int(tokens))

    def add_estimate(self, tokens: int) -> None:
        """Account for messages appended after the last usage report."""
        self.estimated_tokens = max(0, self.estimated_tokens + int(tokens))

    @property
    def usage_ratio(self) -> float:
        """Latest estimate as a fraction of the context window."""
        return self.estimated_tokens / self.window_tokens

    def should_compact(self) -> bool:
        """True when the next request would leave too little room for its response."""
        return self.estimated_tokens + self.reserve_tokens >= (
            self.compact_threshold * self.window_tokens
        )

    def target_tokens(self) -> int:
        """Token budget to compact down to (<= 55% of the window by default)."""
        return max(1, int(self.target_ratio * self.window_tokens))

    def record_compaction(
        self,
        before_ratio: float,
        after_ratio: float,
        summarized_messages: int,
    ) -> None:
        """Store the outcome of one compaction and lower the estimate to match."""
        self.compactions += 1
        self.last_before_ratio = before_ratio
        self.last_after_ratio = after_ratio
        self.last_summarized_messages = summarized_messages
        self.estimated_tokens = max(1, int(after_ratio * self.window_tokens))


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 token estimate; never zero so each message counts at all."""
    return max(1, len(text) // 4)


def estimate_message_tokens(message: Mapping[str, Any]) -> int:
    """Estimate one wire message's tokens (structured content via JSON)."""
    content = message.get("content")
    if isinstance(content, str):
        return estimate_tokens(content)
    if content is None:
        return 0
    try:
        serialized = json.dumps(content, ensure_ascii=False, default=str)
    except TypeError, ValueError:
        serialized = str(content)
    return estimate_tokens(serialized)


def estimate_messages_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Estimate a whole message list's tokens."""
    return sum(estimate_message_tokens(message) for message in messages)


def resolve_window(
    provider: str,
    env: Mapping[str, str] | None = None,
    override: int = 0,
) -> int:
    """Resolve the context window: CLI override > ``CONTEXT_WINDOW_TOKENS`` > default.

    The resolution is pure: pass ``env`` to test it without touching ``os.environ``.
    An override of 0 (or less) means "not set"; an unparseable env value is ignored.
    """
    if override > 0:
        return override

    environment = os.environ if env is None else env
    raw = environment.get("CONTEXT_WINDOW_TOKENS")
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed

    return PROVIDER_WINDOWS.get(provider, FALLBACK_WINDOW)


def ratio_bar(ratio: float, width: int = 20) -> str:
    """Render a 0..1 ratio as an ASCII progress bar plus a percentage."""
    clamped = min(max(ratio, 0.0), 1.0)
    filled = int(round(clamped * width))
    return "[" + "#" * filled + "-" * (width - filled) + f"] {clamped:.0%}"
