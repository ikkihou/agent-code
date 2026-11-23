"""Completion adapter for the Textual prompt.

The matching logic (slash command ranking, @file mention fuzzy subsequence search,
TTL-cached directory scans, SkipPolicy honouring) already lives in ``ui/completer.py``
and is well-tested; this module only drives those completers with the current input
line and flattens their ``Suggestion`` objects into the shape the Textual widget renders.

A single ``CompletionSource`` instance is meant to be kept alive for the whole REPL
session: the mention completer caches its directory scan for a second, so rebuilding it
per keystroke would defeat the cache and rescan the tree on every character.
"""

from __future__ import annotations

from pathlib import Path

from ..slash import SlashRegistry
from ..ui.completer import FileMentionCompleter, SlashCompleter, Suggestion


class CompletionSource:
    """Wraps the two completers and returns plain suggestions for a line of input."""

    def __init__(self, registry: SlashRegistry | None = None, cwd: Path | None = None) -> None:
        self._slash = SlashCompleter(registry)
        self._mention = FileMentionCompleter(cwd) if cwd is not None else None

    def _slash_suggestions(self, line: str) -> list[Suggestion]:
        return list(self._slash.suggestions(line))

    def _mention_suggestions(self, line: str) -> list[Suggestion]:
        if self._mention is None:
            return []
        return list(self._mention.suggestions(line))

    def suggestions(self, line: str) -> tuple[list[Suggestion], int]:
        """Suggestions for the given input line, plus the length of the word to replace.

        The replace length is measured from the start of the active word (the leading
        ``/`` for slash commands, the ``@`` for mentions) to the end of ``line``; the
        caller rewrites that suffix of the input line on acceptance.
        """
        if line.startswith("/") and not any(ch.isspace() for ch in line):
            return self._slash_suggestions(line), len(line)
        if self._mention is not None:
            mentions = self._mention_suggestions(line)
            if mentions:
                index = line.rfind("@")
                return mentions, len(line) - index
        return [], 0
