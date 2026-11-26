"""Block widgets for the committed conversation.

The committed log used to be a single append-only ``RichLog``. To let thinking and
tool calls be individually foldable *in place* (interleaved with static prose), the
log is now a ``VerticalScroll`` of ordered block widgets:

- ``TextBlock``  -- one committed text chunk, rendered as static selectable text;
- ``ThinkingBlock`` -- one extended-thinking block, collapsed by default;
- ``ToolCallBlock`` -- one finished tool call, collapsed by default.

Each thinking / tool block is its own ``Collapsible``, so a click on its header toggles
it without wrapping the surrounding prose (which stays static and in order).
"""

from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from ..output import OutputChunk, ThinkingItem, ToolCallItem, TranscriptItem
from .events import format_tool_fold
from .render import chunk_to_text


class TextBlock(Static):
    """One committed text chunk, rendered as static (selectable) text."""

    def __init__(self, chunk: OutputChunk, width: int) -> None:
        super().__init__(chunk_to_text(chunk, width=width), markup=False)


class ThinkingBlock(Collapsible):
    """A single extended-thinking block, collapsed by default (click to expand)."""

    def __init__(self, text: str) -> None:
        super().__init__(
            Static(Text(text), markup=False),
            title="thinking",
            collapsed=True,
            classes="thinking",
        )


class ToolCallBlock(Collapsible):
    """A single finished tool call, collapsed by default (click to expand detail)."""

    def __init__(self, item: ToolCallItem) -> None:
        detail = Text()
        detail.append("args: ")
        detail.append(item.args_full or item.args_preview or "(none)")
        if item.result:
            detail.append("\n\nresult:\n")
            detail.append(item.result)
        super().__init__(
            Static(detail, markup=False),
            title=format_tool_fold([(item.name, item.status)]),
            collapsed=True,
            classes="tool",
        )


class BlockLog(VerticalScroll):
    """Ordered committed conversation: static text runs + foldable thinking/tool blocks."""

    can_focus = True

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)

    def content_width(self) -> int:
        width = self.scrollable_content_region.width
        if width > 0:
            return width
        return self.size.width if self.size.width > 0 else 80

    def append_chunk(self, chunk: OutputChunk) -> None:
        self._append(TextBlock(chunk, self.content_width()))

    def append_thinking(self, text: str) -> None:
        self._append(ThinkingBlock(text))

    def append_tool(self, item: ToolCallItem) -> None:
        self._append(ToolCallBlock(item))

    def append_item(self, item: TranscriptItem) -> None:
        """Mount any render item (used by both live commits and resume replay)."""
        if isinstance(item, OutputChunk):
            self.append_chunk(item)
        elif isinstance(item, ThinkingItem):
            self.append_thinking(item.text)
        else:
            self.append_tool(item)

    def _append(self, widget: Widget) -> None:
        self.mount(widget)
        # Keep the view pinned to the newest block, matching the old auto_scroll log.
        self.call_after_refresh(self.scroll_end, animate=False)

    def plain_text(self) -> str:
        """Concatenated plain text of the committed blocks (tests / fallback)."""
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, TextBlock):
                parts.append(str(child.content))
            elif isinstance(child, Collapsible):
                parts.append(str(child.title))
        return "".join(parts)
