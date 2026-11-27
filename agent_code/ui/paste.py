"""Bracketed-paste placeholdering: large pastes become ``[Pasted text #N +K lines]``.

A multi-hundred-line paste dropped into the input frame instantly floods the Live
area (the whole buffer re-renders every frame) and then scrollback (the prompt echo
commits it). kimi-cli solves this by replacing oversized pastes with a placeholder
token backed by an in-memory cache; the full text is restored only where it matters
-- the prompt the model actually sees -- while everything the user sees stays one
short line.

Flow: ``PromptInput._on_paste`` intercepts Textual's ``Paste`` event, detects an
oversized paste and inserts a placeholder token into the input document; ``expand``
(called from ``AgentApp._submit_text``) restores the original text before the prompt
enters the agent loop. The prompt echo therefore shows the placeholder line, and the
cache holds the real content for the submission.

Everything here is UI-thread state; the cache deliberately does not survive a
restart -- a paste only matters within the submission that carries it.
"""

from __future__ import annotations

# A paste with more than this many lines becomes a placeholder. Small pastes (a few
# lines, a stack trace) stay inline: they read fine in the frame and the model sees
# them verbatim without a cache round-trip.
PASTE_PLACEHOLDER_LINES = 30

# Hard cap on what the cache keeps (chars). A paste larger than this is truncated
# on register -- a paste bigger than this is useless to the model verbatim anyway
# (tool observations cap at far less), and unbounded memory for one keystroke is
# not acceptable.
_MAX_PASTE_CHARS = 200_000


def is_oversized_paste(text: str) -> bool:
    """True when a bracketed paste is big enough to placeholder."""
    return text.count("\n") + 1 > PASTE_PLACEHOLDER_LINES


class PasteCache:
    """Remembers oversized pastes so placeholders can be expanded back at submit time."""

    def __init__(self) -> None:
        self._entries: dict[int, tuple[str, str]] = {}  # id -> (token, content)
        self._next_id = 1

    def register(self, text: str) -> str:
        """Store a paste and return its placeholder token.

        The token embeds the line count so the placeholder itself is informative
        (``[Pasted text #1 +250 lines]``). Text past the cap is truncated, with the
        truncation noted inside the stored content.
        """
        content = text
        if len(text) > _MAX_PASTE_CHARS:
            dropped = len(text) - _MAX_PASTE_CHARS
            content = text[:_MAX_PASTE_CHARS] + f"\n[... truncated, {dropped} chars dropped]"
        entry_id = self._next_id
        self._next_id += 1
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        token = f"[Pasted text #{entry_id} +{lines} lines]"
        self._entries[entry_id] = (token, content)
        return token

    def expand(self, text: str) -> str:
        """Replace every placeholder token in ``text`` with its stored content.

        Tokens the cache does not know (e.g. the user typed one by hand) pass through
        unchanged. Entries are kept after expansion: one submission may be expanded
        more than once (echo, queue), and re-expansion of a known id is idempotent
        only if the token is still present -- after the first pass it is not, so a
        repeat call is a no-op.
        """
        result = text
        for token, content in self._entries.values():
            if token in result:
                result = result.replace(token, content)
        return result
