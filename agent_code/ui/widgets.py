"""Textual widgets for the agent REPL: prompt input, live panel, status bar.

The committed conversation is a ``BlockLog`` (see ``blocks.py``): a VerticalScroll of
static text blocks and foldable thinking/tool blocks. The in-progress area maps to a
``LivePanel`` (spinner / streaming tail / pending preview), alongside a ``PromptInput``
(multiline input, Enter submits, Ctrl+J inserts a newline) and a ``StatusBar``.
Completion is handled by the app through a separate ``OptionList``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.events import Blur, Focus, Key, Paste, Resize
from textual.message import Message
from textual.widgets import Static, TextArea

from ..runtime import RuntimeState
from .gitinfo import GitInfo, read_git
from .paste import PasteCache, is_oversized_paste
from .render import render_status
from .theme import PALETTE


class PromptInput(TextArea):
    """Multiline input line. Enter submits; Ctrl+J / Alt+Enter insert a newline.

    ``ctrl+c`` is re-bound here (priority=True) to abort instead of TextArea's default
    "copy", so a running turn can always be interrupted from the input. Mouse copy is
    still available via Super+C (the terminal's native copy, not this binding).

    When the app installs a ``completion_handler``, the arrow keys / Tab / Escape are
    offered to it first while a completion menu is open, so the input keeps focus and
    typing keeps refining the menu.
    """

    BINDINGS = [
        Binding("ctrl+c", "abort_input", "abort turn", priority=True, show=False),
    ]

    class Submitted(Message):
        """The user pressed Enter with non-empty input."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class Abort(Message):
        """The user pressed Ctrl+C inside the input box."""

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self.completion_handler: Callable[[str], bool] | None = None
        self.prompt_key_handler: Callable[[str], bool] | None = None
        self.paste_cache: PasteCache | None = None
        self._mode_class: str | None = None
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft: str = ""

    def record_history(self, text: str) -> None:
        """Record a submitted prompt in the session history and reset recall state."""
        cleaned = text.strip()
        if cleaned and (not self._history or self._history[-1] != cleaned):
            self._history.append(cleaned)
        self._history_index = None
        self._draft = ""

    def _load_history_entry(self, text: str) -> None:
        self.load_text(text)
        lines = text.split("\n")
        self.move_cursor((len(lines) - 1, len(lines[-1])))

    def set_permission_mode(self, mode: str) -> None:
        """Tint the border after the active permission mode (Pi's editor affordance)."""
        classes = {
            "default": "mode-default",
            "acceptEdits": "mode-edits",
            "plan": "mode-plan",
            "fullAccess": "mode-full",
        }
        new_class = classes.get(mode, "mode-default")
        if self._mode_class == new_class:
            return
        if self._mode_class is not None:
            self.remove_class(self._mode_class)
        self.add_class(new_class)
        self._mode_class = new_class

    async def _on_key(self, event: Key) -> None:
        # TextArea's own _on_key inserts "\n" on Enter and lets Ctrl+J fall through.
        # Override both to separate "submit" from "newline".
        key = event.key
        if self.prompt_key_handler is not None and self.prompt_key_handler(key):
            event.stop()
            event.prevent_default()
            return
        if self.completion_handler is not None and key in (
            "up",
            "down",
            "tab",
            "escape",
            "enter",
        ):
            # While the completion menu is open, Enter accepts the highlighted
            # suggestion instead of submitting the half-typed command.
            if self.completion_handler(key):
                event.stop()
                event.prevent_default()
                return
        if key == "up" and self._history and self.cursor_location[0] == 0:
            event.stop()
            event.prevent_default()
            if self._history_index is None:
                self._draft = self.text
                self._history_index = len(self._history) - 1
            elif self._history_index > 0:
                self._history_index -= 1
            self._load_history_entry(self._history[self._history_index])
            return
        if (
            key == "down"
            and self._history_index is not None
            and self.cursor_location[0] >= self.document.line_count - 1
        ):
            event.stop()
            event.prevent_default()
            if self._history_index < len(self._history) - 1:
                self._history_index += 1
                self._load_history_entry(self._history[self._history_index])
            else:
                self._history_index = None
                self._load_history_entry(self._draft)
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if key in ("ctrl+j", "alt+enter", "shift+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)

    async def _on_paste(self, event: Paste) -> None:
        if self.paste_cache is not None and is_oversized_paste(event.text):
            event.stop()
            event.prevent_default()
            if self.read_only:
                return
            token = self.paste_cache.register(event.text)
            if result := self._replace_via_keyboard(token, *self.selection):
                self.move_cursor(result.end_location)
                self.focus()
            return
        await super()._on_paste(event)

    def _on_focus(self, event: Focus) -> None:
        super()._on_focus(event)
        _sync_terminal_cursor(self, True)

    def _on_blur(self, event: Blur) -> None:
        super()._on_blur(event)
        _sync_terminal_cursor(self, False)

    def action_abort_input(self) -> None:
        self.post_message(self.Abort())


def _sync_terminal_cursor(widget: TextArea, visible: bool) -> None:
    """Ask the app to show/hide the terminal's own cursor (used for IME anchoring)."""
    set_cursor = getattr(widget.app, "set_terminal_cursor", None)
    if set_cursor is not None:
        set_cursor(visible)


class LivePanel(Static):
    """In-progress content: spinner, streaming tail, pending preview."""

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(Text(), markup=False, name=name, id=id, classes=classes, disabled=disabled)


class QuestionLine(Static):
    """The one-line pending question (confirm/choice), empty when idle."""

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(Text(), markup=False, name=name, id=id, classes=classes, disabled=disabled)


class StatusBar(Static):
    """Bottom status bar: mode · cwd · git · context usage · model."""

    # A git probe runs two short-lived processes; throttle it instead of doing it on
    # every streamed batch.
    GIT_TTL = 2.0

    def __init__(
        self,
        state: RuntimeState,
        cwd: Path | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            render_status(state, cwd=cwd),
            markup=False,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self._state = state
        self._cwd = cwd
        self._git = GitInfo()
        self._git_at = 0.0

    def on_mount(self) -> None:
        self.call_after_refresh(self.refresh_status)

    def refresh_status(self) -> None:
        now = time.monotonic()
        if now - self._git_at >= self.GIT_TTL:
            self._git = read_git(self._cwd)
            self._git_at = now
        self.update(
            render_status(
                self._state,
                cwd=self._cwd,
                git=self._git,
                width=self.content_size.width,
            )
        )

    def on_resize(self, _event: Resize) -> None:
        self.refresh_status()


class HintBar(Static):
    """One dim line of key hints, Pi-style, under the prompt."""

    HINTS: tuple[tuple[str, str], ...] = (
        ("esc", "interrupt"),
        ("ctrl+c", "quit"),
        ("ctrl+o", "expand"),
        ("/", "commands"),
        ("@", "files"),
        ("shift+tab", "mode"),
    )

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            self._render_hints(),
            markup=False,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )

    @classmethod
    def _render_hints(cls) -> Text:
        text = Text()
        for index, (key, description) in enumerate(cls.HINTS):
            if index:
                text.append(" · ", style="dim")
            text.append(key, style=f"bold {PALETTE.warning}")
            text.append(f" {description}", style="dim")
        return text
