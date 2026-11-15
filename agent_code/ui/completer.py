"""Slash command completer: lists available commands when the input line starts with `/`."""

from __future__ import annotations

from typing import Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from ..slash import SlashRegistry, default_slash_registry


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
