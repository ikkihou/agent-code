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
from textual.selection import Selection
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from ..output import OutputChunk, ThinkingItem, ToolCallItem, TranscriptItem
from .events import format_tool_call
from .render import chunk_to_text


def _thinking_title(duration: float) -> str:
    """Pi-style collapsed thinking label: ``✻ Thought for 184ms``."""
    if duration <= 0:
        return "✻ Thought"
    if duration < 1:
        return f"✻ Thought for {max(1, round(duration * 1000))}ms"
    return f"✻ Thought for {duration:.1f}s"


def _extract_selection(selection: Selection, lines: list[str]) -> str | None:
    """Bounds-safe extraction of a selection over already-wrapped visual lines.

    ``Selection.extract`` slices the raw (unwrapped) text with visual-line offsets,
    which raises ``IndexError`` as soon as a paragraph wraps to more rows than it has
    logical lines. Clamp every index instead and treat a ``None`` end as "end of this
    block" (Textual's convention for the first widget of a multi-widget selection).
    """
    count = len(lines)
    if count == 0:
        return None

    start = selection.start
    end = selection.end
    sy, sx = (start.y, start.x) if start is not None else (0, 0)
    if end is None:
        ey, ex = count - 1, len(lines[-1])
    else:
        ey, ex = end.y, end.x

    if (ey, ex) < (sy, sx):
        sy, sx, ey, ex = ey, ex, sy, sx
    if sy >= count:
        return None
    sy = max(0, sy)
    ey = min(count - 1, max(0, ey))

    if sy == ey:
        return lines[sy][sx:ex]
    parts = [lines[sy][sx:], *lines[sy + 1 : ey], lines[ey][:ex]]
    return "\n".join(parts)


class _SelectableStatic(Static):
    """Static whose selection maps to the rendered (wrapped) lines, not logical lines."""

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        height = self.size.height
        if height <= 0:
            return None
        lines = [self.render_line(y).text.rstrip() for y in range(height)]
        text = _extract_selection(selection, lines)
        return None if text is None else (text, "\n")


class TextBlock(_SelectableStatic):
    """One committed text chunk, rendered as static (selectable) text."""

    def __init__(self, chunk: OutputChunk, width: int) -> None:
        super().__init__(chunk_to_text(chunk, width=width), markup=False)


class ThinkingBlock(Collapsible):
    """A single extended-thinking block, collapsed by default.

    The title carries the model's thinking duration when known. The block is styled
    without a card border/background (see ``default.tcss``) so reasoning reads as quiet
    inline prose rather than a boxed component.
    """

    def __init__(self, item: ThinkingItem) -> None:
        super().__init__(
            _SelectableStatic(Text(item.text), markup=False),
            title=_thinking_title(item.duration),
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
            _SelectableStatic(detail, markup=False),
            title=format_tool_call(
                item.name,
                item.args_preview,
                item.status,
                item.args_full,
            ),
            collapsed=True,
            classes=f"tool {item.status}",
        )


class BlockLog(VerticalScroll):
    """Ordered committed conversation: static text runs + foldable thinking/tool blocks."""

    can_focus = True

    # Upper bound on mounted block widgets. Very long sessions would otherwise keep
    # thousands of widgets (layout cost / memory); the oldest blocks are dropped, like
    # the old RichLog max_lines cap. Configurable per instance (tests lower it).
    MAX_BLOCKS: int = 2000

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._block_count = 0
        # A TextBlock renders the chunk's trailing newline as an empty row, so the next
        # block needs no extra margin after prose. Thinking/tool blocks end on their
        # title line and do need one.
        self._prev_was_text = False

    def content_width(self) -> int:
        width = self.scrollable_content_region.width
        if width > 0:
            return width
        return self.size.width if self.size.width > 0 else 80

    def append_chunk(self, chunk: OutputChunk) -> None:
        # Prompt echoes already start with their own blank lines, and prose blocks end
        # with one; only foldable/content blocks after a fold need a leading gap.
        gap = (
            self._block_count > 0
            and not self._prev_was_text
            and not chunk.text.startswith("\n")
        )
        self._append(TextBlock(chunk, self.content_width()), gap=gap)
        self._prev_was_text = True

    def append_thinking(self, item: ThinkingItem) -> None:
        gap = self._block_count > 0 and not self._prev_was_text
        self._append(ThinkingBlock(item), gap=gap)
        self._prev_was_text = False

    def append_tool(self, item: ToolCallItem) -> None:
        gap = self._block_count > 0 and not self._prev_was_text
        self._append(ToolCallBlock(item), gap=gap)
        self._prev_was_text = False

    def append_item(self, item: TranscriptItem) -> None:
        """Mount any render item (used by both live commits and resume replay)."""
        if isinstance(item, OutputChunk):
            self.append_chunk(item)
        elif isinstance(item, ThinkingItem):
            self.append_thinking(item)
        else:
            self.append_tool(item)

    def _append(self, widget: Widget, *, gap: bool) -> None:
        # Only top margins are used, so consecutive blocks get exactly one blank row
        # between them (the later block contributes it).
        if gap and self._block_count > 0:
            widget.add_class("gap")
        self._block_count += 1
        self.mount(widget)
        # Trim on the next refresh (mount is deferred, so ``children`` is not yet
        # up to date), then keep the view pinned to the newest block.
        self.call_after_refresh(self._trim)
        self.call_after_refresh(self.scroll_end, animate=False)

    def _trim(self) -> None:
        excess = self._block_count - self.MAX_BLOCKS
        if excess <= 0:
            return
        removable = list(self.children)[:excess]
        for child in removable:
            child.remove()
        self._block_count -= len(removable)

    def plain_text(self) -> str:
        """Concatenated plain text of the committed blocks (tests / fallback)."""
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, TextBlock):
                parts.append(str(child.content))
            elif isinstance(child, Collapsible):
                parts.append(str(child.title))
        return "".join(parts)
