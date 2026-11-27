"""Block widgets for the committed conversation.

The committed log used to be a single append-only ``RichLog``. To let thinking and
tool calls be individually foldable *in place* (interleaved with static prose), the
log is now a ``VerticalScroll`` of ordered block widgets:

- ``TextBlock``  -- one committed text chunk, rendered as static selectable text;
- ``StepBlock``  -- one foldable group of consecutive thinking and tool-call blocks
  between text chunks, collapsed by default with a count-summary title;
- ``ThinkingBlock`` -- one extended-thinking block (nested inside ``StepBlock``),
  collapsed by default;
- ``ToolCallBlock`` -- one finished tool call (nested inside ``StepBlock``),
  collapsed by default.

Each step group and inner thinking / tool block is its own ``Collapsible``, so a click
on its header toggles it without wrapping the surrounding prose (which stays static and
in order).
"""

from __future__ import annotations

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Resize
from textual.selection import Selection
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from ..output import OutputChunk, ThinkingItem, ToolCallItem, TranscriptItem
from ..prompt_ui import render_diff
from .events import format_tool_call
from .render import _preview_line_style, chunk_to_text
from .theme import PALETTE
from .transcript import format_step_title

# Blank columns left on each side of every committed block. The margin itself comes from
# the ``#log > .text/.step/.thinking/.tool`` rule in ``styles/default.tcss``; the two
# must stay in sync because ``content_width()`` subtracts this before wrapping prose and
# padding full-width prompt bars.
BLOCK_GUTTER = 2


def _thinking_title(duration: float) -> str:
    """Collapsed thinking label: ``✻ Thought for 184ms``."""
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
        super().__init__(chunk_to_text(chunk, width=width), markup=False, classes="text")
        self._chunk = chunk
        self._rendered_width = width

    def rewrap(self, width: int) -> None:
        """Re-render markdown / full-width prompt bars when the log width changes."""
        if width <= 0 or width == self._rendered_width:
            return
        self._rendered_width = width
        if self._chunk.format == "markdown" or self._chunk.full_width:
            self.update(chunk_to_text(self._chunk, width=width))


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
            classes="thinking -collapsed",
        )


def _format_tool_detail(item: ToolCallItem) -> Text:
    """Format expanded tool-call detail (pretty JSON args, colored edit diffs, result)."""
    detail = Text()
    raw_args = item.args_full or item.args_preview
    parsed: object = None
    if raw_args:
        try:
            parsed = json.loads(raw_args)
        except TypeError, ValueError:
            parsed = None

    if (
        item.name == "file_edit"
        and isinstance(parsed, dict)
        and isinstance(parsed.get("old_string"), str)
        and isinstance(parsed.get("new_string"), str)
    ):
        path = str(parsed.get("file_path") or "file")
        diff_text = render_diff(
            parsed["old_string"],
            parsed["new_string"],
            path,
            markup=False,
        )
        detail.append("diff:\n", style=PALETTE.muted)
        for line in diff_text.splitlines():
            detail.append(line + "\n", style=_preview_line_style(line))
    else:
        detail.append("args: ", style=PALETTE.muted)
        if isinstance(parsed, dict) and parsed:
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            detail.append(pretty)
        else:
            detail.append(raw_args or "(none)")

    if item.result:
        if len(detail) and not detail.plain.endswith("\n"):
            detail.append("\n")
        detail.append("\nresult:\n", style=PALETTE.muted)
        result_style = PALETTE.error if item.status == "error" else ""
        detail.append(item.result, style=result_style)
    return detail


class ToolCallBlock(Collapsible):
    """A single finished tool call, collapsed by default (click to expand detail)."""

    def __init__(self, item: ToolCallItem) -> None:
        super().__init__(
            _SelectableStatic(_format_tool_detail(item), markup=False),
            title=format_tool_call(
                item.name,
                item.args_preview,
                item.status,
                item.args_full,
            ),
            collapsed=True,
            classes=f"tool {item.status} -collapsed",
        )


class StepBlock(Collapsible):
    """A foldable group of thinking and tool-call blocks produced between text chunks.

    Mounted as soon as the first thinking or tool-call item arrives and updated in place
    as subsequent items settle before the next text chunk. Collapsed by default with a
    count-summary title (e.g. ``✻ 1 thinking, 2 tools``); expanding reveals the nested
    ``ThinkingBlock`` and ``ToolCallBlock`` widgets in order.
    """

    def __init__(self, first: ThinkingItem | ToolCallItem | None = None) -> None:
        self.thinking_count: int = 0
        self.tool_count: int = 0
        self.step_widgets: list[ThinkingBlock | ToolCallBlock] = []
        self._contents_container: Collapsible.Contents | None = None
        super().__init__(
            title=format_step_title(0, 0),
            collapsed=True,
            classes="step -collapsed",
        )
        if first is not None:
            self.append_step_item(first)

    def compose(self) -> ComposeResult:
        yield self._title
        container = self.Contents()
        self._contents_container = container
        with container:
            yield from self._contents_list

    def append_step_item(
        self, item: ThinkingItem | ToolCallItem
    ) -> ThinkingBlock | ToolCallBlock:
        """Append one thinking or tool-call block and update the summary title."""
        if isinstance(item, ThinkingItem):
            widget: ThinkingBlock | ToolCallBlock = ThinkingBlock(item)
            self.thinking_count += 1
        else:
            widget = ToolCallBlock(item)
            self.tool_count += 1
        self.step_widgets.append(widget)
        title = format_step_title(self.thinking_count, self.tool_count)
        self.title = title
        self._title.label = title
        if self._contents_container is not None and self._contents_container.is_attached:
            if self._contents_container._pending_children:
                self._contents_container.compose_add_child(widget)
            else:
                self._contents_container.mount(widget)
        else:
            self._contents_list.append(widget)
        return widget


class BlockLog(VerticalScroll):
    """Ordered committed conversation: static text runs + foldable step blocks."""

    can_focus = True

    # Upper bound on mounted block widgets. Very long sessions would otherwise keep
    # thousands of widgets (layout cost / memory); the oldest blocks are dropped, like
    # the old RichLog max_lines cap. Configurable per instance (tests lower it).
    MAX_BLOCKS: int = 2000

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._block_count = 0
        # A TextBlock renders the chunk's trailing newline as an empty row, so the next
        # block needs no extra margin after prose. Foldable step blocks end on their
        # title line and do need one.
        self._prev_was_text = False
        # Active step block collecting consecutive thinking/tool items until the next
        # text chunk arrives.
        self._current_step: StepBlock | None = None
        # Auto-follow new content while the user is at the bottom; pause when the user
        # scrolls up to read history, and resume once they scroll back to the end.
        self._auto_scroll = True
        self._last_width = 0

    def content_width(self) -> int:
        width = self.scrollable_content_region.width
        if width <= 0:
            width = self.size.width if self.size.width > 0 else 80
        # Committed blocks are inset by BLOCK_GUTTER on each side, so the wrap/pad target
        # is the inner width rather than the whole scrollable region.
        return max(1, width - 2 * BLOCK_GUTTER)

    def append_chunk(self, chunk: OutputChunk) -> None:
        # Text closes any active step group so subsequent thinking/tool items open a new
        # foldable widget after this prose chunk.
        self._current_step = None
        # Prompt echoes already start with their own blank lines, and prose blocks end
        # with one; only foldable/content blocks after a fold need a leading gap.
        gap = self._block_count > 0 and not self._prev_was_text and not chunk.text.startswith("\n")
        self._append(TextBlock(chunk, self.content_width()), gap=gap)
        self._prev_was_text = True

    def _append_step_item(self, item: ThinkingItem | ToolCallItem) -> None:
        if self._current_step is not None:
            self._current_step.append_step_item(item)
            self.call_after_refresh(self._auto_scroll_end)
            return
        gap = self._block_count > 0 and not self._prev_was_text
        step = StepBlock(item)
        self._current_step = step
        self._append(step, gap=gap)
        self._prev_was_text = False

    def append_thinking(self, item: ThinkingItem) -> None:
        self._append_step_item(item)

    def append_tool(self, item: ToolCallItem) -> None:
        self._append_step_item(item)

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
        # up to date), then keep the view pinned to the newest block unless the user
        # has scrolled up to inspect earlier output.
        self.call_after_refresh(self._trim)
        self.call_after_refresh(self._auto_scroll_end)

    def _auto_scroll_end(self) -> None:
        if self._auto_scroll:
            self.scroll_end(animate=False)

    def release_anchor(self) -> None:
        super().release_anchor()
        if self.scroll_target_y < self.max_scroll_y:
            self._auto_scroll = False

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if round(new_value) >= self.max_scroll_y:
            self._auto_scroll = True
        elif round(new_value) < round(old_value):
            self._auto_scroll = False

    def on_resize(self, _event: Resize) -> None:
        self._rewrap_blocks()
        self.call_after_refresh(self._rewrap_blocks)

    def _rewrap_blocks(self) -> None:
        width = self.content_width()
        if width <= 0 or width == self._last_width:
            return
        self._last_width = width
        for child in self.children:
            if isinstance(child, TextBlock):
                child.rewrap(width)

    def _trim(self) -> None:
        excess = self._block_count - self.MAX_BLOCKS
        if excess <= 0:
            return
        removable = list(self.children)[:excess]
        for child in removable:
            if child is self._current_step:
                self._current_step = None
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

    def toggle_latest_fold(self) -> bool:
        """Toggle the most recent foldable block; return True if found."""
        for child in reversed(self.children):
            if isinstance(child, StepBlock):
                new_collapsed = not child.collapsed
                child.collapsed = new_collapsed
                for sub in child.step_widgets:
                    sub.collapsed = new_collapsed
                return True
            if isinstance(child, Collapsible):
                child.collapsed = not child.collapsed
                return True
        return False
