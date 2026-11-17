"""Input completers: slash commands and @file mentions.

``SlashCompleter`` lists available commands when the input line starts with ``/``;
``FileMentionCompleter`` completes file paths after an ``@`` mention (kimi-cli's
``LocalFileMentionCompleter``). The REPL merges both with ``merge_completers`` --
each one decides for itself whether the text at the cursor falls in its trigger.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from ..fs_safety import SkipPolicy, load_gitignore, should_skip
from ..slash import SlashRegistry, default_slash_registry

# How long a directory scan stays fresh. Typing "@ma" then "@main" reuses one walk
# instead of rescanning the project per keystroke; a stale cache only costs a
# second of staleness on newly created files, which is acceptable in the REPL.
_FILE_SCAN_TTL = 1.0

# Upper bound on files collected in one scan, so a huge tree cannot stall the UI
# thread when the user types "@" in a directory like node_modules' sibling.
_MAX_MENTION_FILES = 2000

# Completions yielded per keystroke (prefix matches + substring matches together).
_MAX_MENTION_RESULTS = 32


class SlashCompleter(Completer):
    def __init__(self, registry: SlashRegistry | None = None) -> None:
        self._registry = registry or default_slash_registry()

    def get_completions(self, document: Document, complete_event: Any) -> Any:
        text_before_cursor = document.text_before_cursor
        if not text_before_cursor.startswith("/") or any(
            char.isspace() for char in text_before_cursor
        ):
            return

        prefix = text_before_cursor[1:].casefold()
        prefix_hits: list[Completion] = []
        contains_hits: list[Completion] = []
        for command in self._registry.commands():
            name = command.name.casefold()
            completion = Completion(
                f"/{command.name}",
                start_position=-len(text_before_cursor),
                display=f"/{command.name}",
                display_meta=command.description,
            )
            # Two-tier matching: prefix matches first (what the user most likely
            # meant), contains matches kept as a fallback so a typo mid-word
            # ("/elp" -> /help) still surfaces the command -- just ranked lower.
            if name.startswith(prefix):
                prefix_hits.append(completion)
            elif prefix in name:
                contains_hits.append(completion)
        yield from prefix_hits
        yield from contains_hits


class FileMentionCompleter(Completer):
    """Complete file paths after an ``@`` mention, e.g. ``read @agent_code/ma``.

    Trigger: the word at the cursor starts with ``@`` (mid-line mentions count).
    Completion text inserts the relative path (directories keep a trailing ``/``);
    the display shows only the basename, meta shows the full relative path.

    The scan respects ``should_skip`` (skip dirs + .gitignore), so git-ignored and
    vendored paths never surface as mentions -- the same visibility the agent's own
    read tools have. Scans are TTL-cached because completion runs on the UI thread
    for every keystroke of the mention.
    """

    def __init__(self, cwd: Path, policy: SkipPolicy | None = None) -> None:
        self._cwd = cwd
        self._policy = policy
        self._cache_at: float = -_FILE_SCAN_TTL
        self._files: list[str] = []

    def _policy_or_default(self) -> SkipPolicy:
        if self._policy is None:
            self._policy = SkipPolicy.default(gitignore=load_gitignore(self._cwd))
        return self._policy

    def _scan(self) -> list[str]:
        """Relative paths of cwd files/dirs, sorted dirs-first (stable for the menu)."""
        now = time.monotonic()
        if now - self._cache_at < _FILE_SCAN_TTL:
            return self._files
        self._cache_at = now

        policy = self._policy_or_default()
        found: list[str] = []
        try:
            for path in self._cwd.rglob("*"):
                rel = path.relative_to(self._cwd)
                if should_skip(rel, policy):
                    continue
                name = rel.as_posix() + ("/" if path.is_dir() else "")
                found.append(name)
                if len(found) >= _MAX_MENTION_FILES:
                    break
        except OSError:
            return self._files  # keep the previous snapshot on a transient FS error
        self._files = sorted(found, key=lambda p: (not p.endswith("/"), p.casefold()))
        return self._files

    def get_completions(self, document: Document, complete_event: Any) -> Any:
        # The mention token is the last whitespace-delimited word before the cursor;
        # this completer engages only when it starts with "@". (get_word_before_cursor
        # would do, but the raw split also handles "@" alone as its own word.)
        words = document.text_before_cursor.split()
        token = words[-1] if words else ""
        if not token.startswith("@"):
            return

        query = token[1:].replace("\\", "/").casefold()
        files = self._scan()
        if "/" in query:
            # A path with a separator is already specific: match against the whole
            # relative path so "@ui/com" can complete to "agent_code/ui/completer.py".
            candidates = [f for f in files if query in f.casefold()]
        else:
            basename_hits = [f for f in files if query in f.rsplit("/", 1)[-1].casefold()]
            path_hits = [f for f in files if query in f.casefold() and f not in basename_hits]
            # Two-tier ranking: basename matches first (what the user most likely
            # means), whole-path matches after -- mirroring SlashCompleter's tiers.
            candidates = basename_hits + path_hits

        for path in candidates[:_MAX_MENTION_RESULTS]:
            display = path.rsplit("/", 1)[-1]
            yield Completion(
                path,
                start_position=-len(token),
                display=display,
                display_meta=path,
            )
