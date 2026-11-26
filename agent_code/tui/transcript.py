"""Pure transcript state machine: the wire event stream -> OutputChunks.

This is the frontend-agnostic half of the REPL, shared by the Textual app (and any
future frontend). It owns the ``LiveArea`` (streaming tail / spinner / pending preview)
and the tool-burst fold buffer, and turns one drained event batch into the chunks to
commit, in scrollback order.

The ordering rule is the point of the state machine: streamed prose must land in
scrollback before the tool call that followed it, and a burst of finished tool calls
folds into a single summary line rather than one line per call.
"""

from __future__ import annotations

import time

from ..output import OutputChunk
from ..ui.events import event_to_chunks, tool_fold_chunk, tool_outcome
from ..ui.live import LiveArea, preview_lines
from ..wire import (
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
    WireEvent,
)


class TranscriptSink:
    """Consumes wire events and produces the chunks a frontend should commit."""

    def __init__(self) -> None:
        self.live = LiveArea()
        # Finished tool calls not yet committed. They fold into ONE summary line so a
        # long tool chain cannot push the conversation out of the log; the burst ends
        # when prose resumes, another finished event arrives, or the turn ends.
        self.tool_fold: list[tuple[str, str]] = []

    def handle_batch(self, batch: list[WireEvent]) -> list[OutputChunk]:
        """Fold one drained batch into the chunks to commit (in scrollback order).

        Pure state transition over the Live area and the tool-burst buffer: no terminal
        IO, so the ordering rules are testable without an event loop.
        """
        commit: list[OutputChunk] = []
        for event in batch:
            if isinstance(event, TextDelta):
                self._flush_tool_fold(commit)
                for paragraph in self.live.feed_stream(event.text):
                    commit.append(OutputChunk(paragraph, format="markdown"))
                continue
            if isinstance(event, ToolCallStarted):
                self._flush_prose(commit)
                self.live.active_tool = event.name
                continue
            if isinstance(event, ToolCallCompleted):
                self.live.active_tool = None
                self.tool_fold.append((event.name, tool_outcome(event)))
                continue
            if isinstance(event, TurnStarted):
                self._flush_prose(commit, close_fence=True)
                self.live.turn_active = True
                self.live.turn_started_at = time.monotonic()
                self.live.reset_stream()
                self.live.active_tool = None
                continue
            if isinstance(event, TurnEnded):
                self._flush_prose(commit, trailing_blank=False, close_fence=True)
                self._flush_tool_fold(commit)
                self.live.turn_active = False
                self.live.active_tool = None
                self.live.pending_preview = []
                continue

            preview = preview_lines(event)
            if preview is not None:
                self._flush_prose(commit)
                self.live.pending_preview = preview
                continue
            self._flush_prose(commit)
            self._flush_tool_fold(commit)
            commit.extend(event_to_chunks(event))
        return commit

    def _flush_prose(
        self,
        commit: list[OutputChunk],
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

    def _flush_tool_fold(self, commit: list[OutputChunk]) -> None:
        """Close the current tool burst: emit its single summary line."""
        if not self.tool_fold:
            return
        chunk = tool_fold_chunk(self.tool_fold)
        self.tool_fold = []
        if chunk is not None:
            commit.append(chunk)
