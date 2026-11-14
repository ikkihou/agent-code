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
        for command in self._registry.commands():
            # Case-insensitive contains matching keeps the menu useful while the user
            # is in an intermediate state (e.g. /elp -> /help) instead of going empty.
            if prefix in command.name.casefold():
                yield Completion(
                    f"/{command.name}",
                    start_position=-len(text_before_cursor),
                    display=f"/{command.name}",
                    display_meta=command.description,
                )
