"""Input completers: slash commands and @file mentions (frontend-agnostic).

``SlashCompleter`` lists available commands when the input line starts with ``/``;
``FileMentionCompleter`` completes file paths after an ``@`` mention, modelled on
kimi-cli's ``LocalFileMentionCompleter``: subsequence fuzzy matching, a lightweight
top-level listing for the first keystroke, an ``@``-guard that keeps emails from
triggering, and a short-circuit once the fragment already names an existing file. File
discovery reuses the agent's own ``SkipPolicy`` (skip dirs + .gitignore) with an
early-pruned scandir walk.

The public API is ``suggestions(text_before_cursor) -> list[Suggestion]`` -- no
prompt_toolkit protocol -- so both the Textual frontend and tests consume plain
dataclasses.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..fs_safety import SkipPolicy, load_gitignore, should_skip
from ..slash import SlashRegistry, default_slash_registry

# How long a directory scan stays fresh. Typing "@ma" then "@main" reuses one walk
# instead of rescanning the project per keystroke; a stale cache only costs a
# second of staleness on newly created files, which is acceptable in the REPL.
_FILE_SCAN_TTL = 1.0

# Upper bound on files collected in one scan, so a huge tree cannot stall the UI
# thread when the user types "@" in a directory like node_modules' sibling.
_MAX_MENTION_FILES = 2000

# Completions yielded per keystroke.
_MAX_MENTION_RESULTS = 32

# Characters that, immediately before the "@", keep it from triggering. "@user@host"
# or "cost@100" are not file mentions. (kimi-cli's _TRIGGER_GUARDS.)
_TRIGGER_GUARDS = frozenset((".", "-", "_", "`", "'", '"', ":", "@", "#", "~"))

# Path fragments shorter than this (with no "/") only get top-level entries: a deep
# scan for "@a" is wasted work and floods the menu with irrelevant paths.
_TOP_LEVEL_MAX_QUERY = 3


@dataclass(frozen=True)
class Suggestion:
    """One completion candidate.

    ``insert`` is what replaces the active word; ``display`` is the short menu label;
    ``meta`` is the longer description shown dimmed on the right.
    """

    insert: str
    display: str
    meta: str = ""


def _extract_fragment(text: str) -> str | None:
    """The mention fragment in ``text`` (after the last "@"), or None when not a mention.

    Rules (kimi-cli's): the last "@" starts a fragment unless the character right
    before it is alphanumeric or a guard character (email / identifier protection);
    the fragment itself must not contain whitespace.
    """
    index = text.rfind("@")
    if index == -1:
        return None
    if index > 0:
        prev = text[index - 1]
        if prev.isalnum() or prev in _TRIGGER_GUARDS:
            return None
    fragment = text[index + 1 :]
    if any(ch.isspace() for ch in fragment):
        return None
    return fragment


class SlashCompleter:
    def __init__(self, registry: SlashRegistry | None = None) -> None:
        self._registry = registry or default_slash_registry()

    def suggestions(self, text_before_cursor: str) -> list[Suggestion]:
        if not text_before_cursor.startswith("/") or any(
            char.isspace() for char in text_before_cursor
        ):
            return []

        prefix = text_before_cursor[1:].casefold()
        prefix_hits: list[Suggestion] = []
        contains_hits: list[Suggestion] = []
        for command in self._registry.commands():
            name = command.name.casefold()
            suggestion = Suggestion(
                insert=f"/{command.name}",
                display=f"/{command.name}",
                meta=command.description,
            )
            # Two-tier matching: prefix matches first (what the user most likely
            # meant), contains matches kept as a fallback so a typo mid-word
            # ("/elp" -> /help) still surfaces the command -- just ranked lower.
            if name.startswith(prefix):
                prefix_hits.append(suggestion)
            elif prefix in name:
                contains_hits.append(suggestion)
        return prefix_hits + contains_hits


class FileMentionCompleter:
    """Complete file paths after an ``@`` mention, e.g. ``read @agent_code/ma``.

    Matching is kimi-cli-style **subsequence fuzzy**: "agcomp" finds
    ``agent_code/ui/completer.py`` ("ag"..."comp" in order). Candidates are re-ranked
    after matching (basename-prefix > basename-substring > path-subsequence) so likely
    intents float to the top.

    Two scan depths (kimi-cli's top/deep split): a short fragment without "/" only
    lists top-level entries (one ``iterdir``, a few ms); longer fragments run the full
    pruned walk. Both honor ``SkipPolicy`` -- git-ignored and vendored paths never
    surface.

    Guards: an "@" preceded by an identifier character never triggers (emails), a
    fragment containing whitespace never triggers, and once the fragment already names
    an existing file completion stops.
    """

    def __init__(self, cwd: Path, policy: SkipPolicy | None = None) -> None:
        self._cwd = cwd
        self._policy = policy
        self._cache_at: float = -_FILE_SCAN_TTL
        self._files: list[str] = []
        self._top_cache_at: float = -_FILE_SCAN_TTL
        self._top_files: list[str] = []

    def _policy_or_default(self) -> SkipPolicy:
        if self._policy is None:
            self._policy = SkipPolicy.default(gitignore=load_gitignore(self._cwd))
        return self._policy

    def _scan_top(self) -> list[str]:
        """Immediate children of cwd only (the "@" / "@a" case): one iterdir."""
        now = time.monotonic()
        if now - self._top_cache_at < _FILE_SCAN_TTL:
            return self._top_files
        self._top_cache_at = now

        policy = self._policy_or_default()
        entries: list[str] = []
        try:
            for entry in sorted(self._cwd.iterdir(), key=lambda p: p.name):
                rel = Path(entry.name)
                if should_skip(rel, policy):
                    continue
                entries.append(entry.name + ("/" if entry.is_dir() else ""))
                if len(entries) >= _MAX_MENTION_FILES:
                    break
        except OSError:
            return self._top_files
        self._top_files = entries
        return self._top_files

    def _scan(self) -> list[str]:
        """Relative paths of cwd files/dirs, sorted dirs-first (stable for the menu)."""
        now = time.monotonic()
        if now - self._cache_at < _FILE_SCAN_TTL:
            return self._files
        self._cache_at = now

        policy = self._policy_or_default()
        found: list[str] = []
        stack: list[Path] = [self._cwd]
        while stack and len(found) < _MAX_MENTION_FILES:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    listing = list(entries)
            except OSError:
                continue  # unreadable directory: skip it, keep scanning the rest
            for entry in listing:
                name = (
                    entry.name
                    if directory == self._cwd
                    else (directory.relative_to(self._cwd) / entry.name)
                )
                rel = Path(name)
                if should_skip(rel, policy):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    found.append(rel.as_posix() + "/")
                    stack.append(Path(entry.path))
                else:
                    found.append(rel.as_posix())
                if len(found) >= _MAX_MENTION_FILES:
                    break

        self._files = sorted(found, key=lambda p: (not p.endswith("/"), p.casefold()))
        return self._files

    def _candidates(self, fragment: str) -> list[str]:
        """Candidate paths for one fragment: top-level for short queries, full walk otherwise."""
        if "/" not in fragment and len(fragment) < _TOP_LEVEL_MAX_QUERY:
            return self._scan_top()
        return self._scan()

    @staticmethod
    def _subsequence_ranks(fragment: str, path: str) -> tuple[int, int, int] | None:
        """Rank a path against the fragment, or None when it is not a fuzzy match.

        Returns ``(tier, first_index, depth)``: tier 0 = basename starts with the
        fragment, 1 = all fragment chars in order inside the basename, 2 =
        subsequence anywhere in the path.
        """
        frag = fragment.casefold()
        lower = path.casefold()
        base = path.rstrip("/").rsplit("/", 1)[-1].casefold()
        depth = path.count("/")

        if base.startswith(frag):
            return (0, 0, depth)
        if frag in base:
            return (1, base.index(frag), depth)

        pos = lower.find(frag[0])
        if pos == -1:
            return None
        i = 0
        for char in lower:
            if char == frag[i]:
                i += 1
                if i == len(frag):
                    return (2, pos, depth)
        return None

    def suggestions(self, text_before_cursor: str) -> list[Suggestion]:
        fragment = _extract_fragment(text_before_cursor)
        if fragment is None:
            return []
        # The fragment already names an existing file: the mention is complete, stop.
        candidate = fragment.rstrip("/")
        if candidate and (self._cwd / candidate).is_file():
            return []

        files = self._candidates(fragment)
        ranked: list[tuple[tuple[int, int, int], str]] = []
        for path in files:
            ranks = self._subsequence_ranks(fragment, path)
            if ranks is not None:
                ranked.append((ranks, path))
        ranked.sort(key=lambda item: item[0])

        return [
            Suggestion(insert=path, display=path.rsplit("/", 1)[-1], meta=path)
            for _, path in ranked[:_MAX_MENTION_RESULTS]
        ]
