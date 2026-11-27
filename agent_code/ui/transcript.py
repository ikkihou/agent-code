"""Pure transcript state machine: the wire event stream -> ordered render items.

This is the frontend-agnostic half of the REPL, shared by the Textual app (and any
future frontend). It owns the ``LiveArea`` (streaming tail / spinner / pending preview)
and turns one drained event batch into an ordered list of render items:

- ``OutputChunk`` for static text / notifications (paragraph-committed prose),
- ``ThinkingItem`` for one contiguous extended-thinking block,
- ``ToolCallItem`` for one finished tool call.

The ordering rule is the point of the state machine: streamed prose must land before
the tool call that followed it, and thinking must land before the answer it precedes.
Consecutive ``ThinkingItem`` and ``ToolCallItem`` items emitted between two prose
``OutputChunk``s form one foldable step group in the TUI, titled with
``format_step_title``.
"""

from __future__ import annotations

import time

from ..output import OutputChunk, ThinkingItem, ToolCallItem, TranscriptItem
from ..wire import (
    TextDelta,
    ThinkingDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
    WireEvent,
)
from .events import event_to_chunks, tool_outcome
from .live import LiveArea, preview_lines


def format_step_title(thinking_count: int, tool_count: int) -> str:
    """Collapsed step-group label summarizing thinking and tool-call counts."""
    parts: list[str] = []
    if thinking_count > 0:
        parts.append(f"{thinking_count} thinking")
    if tool_count > 0:
        noun = "tool" if tool_count == 1 else "tools"
        parts.append(f"{tool_count} {noun}")
    if not parts:
        return "✻ 0 steps"
    return "✻ " + ", ".join(parts)


class TranscriptSink:
    """Consumes wire events and produces the items a frontend should commit."""

    def __init__(self) -> None:
        self.live = LiveArea()
        # Consecutive thinking deltas form one block; the buffer flushes when any other
        # event arrives (the next text block, a tool call, or the turn boundary).
        self.thinking: list[str] = []
        # Producer timestamps (time.monotonic) of the current thinking block; used to
        # report its duration. Stays 0.0 when the producer does not stamp.
        self.thinking_started_at: float = 0.0
        self.thinking_last_at: float = 0.0
        # call_id -> (name, args_preview, args_full) captured at ToolCallStarted, so the
        # finished item can carry the arguments even when parallel calls settle out of
        # order.
        self.active_tools: dict[str, tuple[str, str, str]] = {}

    def handle_batch(self, batch: list[WireEvent]) -> list[TranscriptItem]:
        """Fold one drained batch into the items to commit (in scrollback order).

        Pure state transition over the Live area and the thinking/tool buffers: no
        terminal IO, so the ordering rules are testable without an event loop.
        """
        commit: list[TranscriptItem] = []
        for event in batch:
            if isinstance(event, ThinkingDelta):
                # Thinking opens a block: flush any prose that preceded it so the
                # reasoning lands before the answer that follows.
                if not self.thinking:
                    self._flush_prose(commit)
                if event.at:
                    if not self.thinking_started_at:
                        self.thinking_started_at = event.at
                    self.thinking_last_at = event.at
                self.thinking.append(event.text)
                continue
            if isinstance(event, TextDelta):
                self._flush_thinking(commit)
                for paragraph in self.live.feed_stream(event.text):
                    commit.append(OutputChunk(paragraph, format="markdown"))
                continue
            if isinstance(event, ToolCallStarted):
                self._flush_thinking(commit)
                self._flush_prose(commit)
                self.active_tools[event.call_id] = (
                    event.name,
                    event.args_preview,
                    event.args_full,
                )
                self._sync_active_tool()
                continue
            if isinstance(event, ToolCallCompleted):
                self._flush_thinking(commit)
                self._flush_prose(commit)
                name, args_preview, args_full = self.active_tools.pop(
                    event.call_id, (event.name, "", "")
                )
                self._sync_active_tool()
                commit.append(
                    ToolCallItem(
                        name=name,
                        args_preview=args_preview,
                        status=tool_outcome(event),
                        args_full=args_full,
                        result=event.result,
                    )
                )
                continue
            if isinstance(event, TurnStarted):
                self._flush_thinking(commit)
                self._flush_prose(commit, close_fence=True)
                self.live.turn_active = True
                self.live.turn_started_at = time.monotonic()
                self.live.reset_stream()
                self.live.active_tool = None
                self.active_tools.clear()
                continue
            if isinstance(event, TurnEnded):
                self._flush_thinking(commit)
                self._flush_prose(commit, trailing_blank=False, close_fence=True)
                self.live.turn_active = False
                self.live.active_tool = None
                self.live.pending_preview = []
                self.active_tools.clear()
                continue

            preview = preview_lines(event)
            if preview is not None:
                self._flush_thinking(commit)
                self._flush_prose(commit)
                self.live.pending_preview = preview
                continue
            self._flush_thinking(commit)
            self._flush_prose(commit)
            commit.extend(event_to_chunks(event))
        return commit

    def _sync_active_tool(self) -> None:
        """Keep ``live.active_tool`` aligned with all currently in-flight tool calls."""
        if not self.active_tools:
            self.live.active_tool = None
            return
        names = [entry[0] for entry in self.active_tools.values()]
        if len(names) == 1:
            self.live.active_tool = names[0]
        else:
            self.live.active_tool = f"{names[0]} (+{len(names) - 1} more)"

    def _flush_thinking(self, commit: list[TranscriptItem]) -> None:
        """Close the current thinking block, if any, so it keeps its place."""
        if not self.thinking:
            return
        text = "".join(self.thinking)
        duration = (
            max(0.0, self.thinking_last_at - self.thinking_started_at)
            if self.thinking_started_at and self.thinking_last_at
            else 0.0
        )
        self.thinking = []
        self.thinking_started_at = 0.0
        self.thinking_last_at = 0.0
        if text.strip():
            commit.append(ThinkingItem(text, duration=duration))

    def _flush_prose(
        self,
        commit: list[TranscriptItem],
        *,
        trailing_blank: bool = True,
        close_fence: bool = False,
    ) -> None:
        """Move the open streamed paragraph into ``commit`` so it keeps its place."""
        tail = self.live.flush_paragraph(close_fence=close_fence)
        if not tail.strip():
            return
        if trailing_blank:
            commit.append(OutputChunk(tail.rstrip("\n") + "\n\n", format="markdown"))
        else:
            commit.append(OutputChunk(tail, format="markdown"))
