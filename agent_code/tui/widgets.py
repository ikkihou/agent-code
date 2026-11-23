"""Textual widgets for the agent REPL: output log, prompt input, status bar.

The scrollback+Live design maps onto Textual as: ``OutputLog`` (committed history, the
former native scrollback) + a Live panel (spinner / streaming tail / pending preview)
+ a ``PromptInput`` (multiline input, Enter submits, Ctrl+J inserts a newline) + a
``StatusBar``. Completion is handled by the app through a separate ``OptionList``.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.segment import Segment
from rich.text import Text
from textual.binding import Binding
from textual.events import Blur, Focus, Key
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog, Static, TextArea

from ..output import OutputChunk
from ..runtime import RuntimeState
from .render import chunk_to_text, render_status

# Past this many rows the committed log drops the oldest lines, so a very long session
# cannot grow the widget buffer without bound.
_LOG_MAX_LINES = 10_000


class OutputLog(RichLog):
    """Committed conversation history (the former native scrollback).

    ``RichLog`` ships no text-selection support: it neither reports content offsets to
    the screen (so a mouse drag can never be mapped back to a line/column) nor paints
    the selection. Both gaps are closed here, which is what makes mouse selection and
    the ``ctrl+c`` copy work on the committed history.
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            markup=False,
            wrap=True,
            highlight=False,
            auto_scroll=True,
            max_lines=_LOG_MAX_LINES,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )

    def write_chunk(self, chunk: OutputChunk) -> None:
        """Commit one OutputChunk as a styled Rich renderable."""
        self.write(chunk_to_text(chunk, width=self._content_width()))

    def _content_width(self) -> int:
        width = self.size.width
        return width if width > 0 else 80

    # ---- text selection -----------------------------------------------------

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Plain text under the selection.

        ``RichLog`` already stores one wrapped ``Strip`` per visual line, so the index
        of a strip is the ``y`` coordinate reported by :meth:`render_line`. Trailing
        padding (added to every strip to reach the render width) is stripped so copying
        does not drag a tail of spaces along.
        """
        text = "\n".join(strip.text.rstrip() for strip in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        # The rendered output depends on the selection, but RichLog caches one Strip
        # per line, so the cache has to go when the selection changes.
        self._line_cache.clear()
        self.refresh()

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        content_y = scroll_y + y
        strip = self._render_line(
            content_y, scroll_x, self.scrollable_content_region.width
        )
        strip = strip.apply_style(self.rich_style)
        strip = self._paint_selection(strip, content_y)
        # The offset metadata is how the compositor maps a mouse position back to a
        # (line, column) inside the log; without it the screen falls back to selecting
        # the whole widget, which RichLog cannot report.
        return strip.apply_offsets(scroll_x, content_y)

    def _paint_selection(self, strip: Strip, content_y: int) -> Strip:
        """Highlight the selected span of one rendered line."""
        selection = self.text_selection
        if selection is None:
            return strip
        span = selection.get_span(content_y)
        if span is None:
            return strip
        start, end = span
        if end == -1:
            end = strip.cell_length
        start = max(0, start)
        end = min(strip.cell_length, end)
        if end <= start:
            return strip
        style = self.screen.get_component_rich_style("screen--selection")
        # ``post_style``, not ``Strip.apply_style``: the line already carries its own
        # color/background, which must not win over the selection highlight.
        selected = strip.crop(start, end)
        highlighted = Strip(
            list(Segment.apply_style(selected, post_style=style)), selected.cell_length
        )
        return Strip.join([strip.crop(0, start), highlighted, strip.crop(end)])


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
        self.border_title = "input"
        self.completion_handler: Callable[[str], bool] | None = None

    async def _on_key(self, event: Key) -> None:
        # TextArea's own _on_key inserts "\n" on Enter and lets Ctrl+J fall through.
        # Override both to separate "submit" from "newline".
        key = event.key
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
        super().__init__(
            Text(), markup=False, name=name, id=id, classes=classes, disabled=disabled
        )


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
        super().__init__(
            Text(), markup=False, name=name, id=id, classes=classes, disabled=disabled
        )


class StatusBar(Static):
    """Bottom status bar: permission mode · model · context usage."""

    def __init__(
        self,
        state: RuntimeState,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            render_status(state),
            markup=False,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self._state = state

    def refresh_status(self) -> None:
        self.update(render_status(self._state))
