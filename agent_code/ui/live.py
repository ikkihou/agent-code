"""Live area state: the worker produces events, the UI consumes them and renders them
into the dedicated Live window above the input frame.

The Live window sits directly under the native scrollback tail, so the spinner belongs
at its top: the submitted prompt is the last committed scrollback line and the spinner
appears immediately below it in the repaintable region. A blank separator line is part
of the Live area, not the prompt echo, so the gap is exactly one line."""

from __future__ import annotations

import re

from rich.cells import cell_len

from ..output import StyleAndText
from ..wire import CommandPreview, DiffPreview, WireEvent

# Streaming tail lines shown in the Live area -- the whole paragraph is committed to
# scrollback when the turn ends.
STREAM_TAIL_LINES = 3
# Spinner animation frames.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# A fenced code block delimiter (``` or ~~~, 3+, optional indent/info string).
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def _fit_width(text: str, columns: int) -> str:
    """Truncate ``text`` to at most ``columns`` terminal cells.

    Character slicing is wrong for wide text: 40 CJK characters are 80 cells, so the
    Live line would overflow a 40-column window and wrap it into extra rows, pushing
    the input down. Measure display cells instead.
    """
    if columns <= 0:
        return ""
    if cell_len(text) <= columns:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        width = cell_len(char)
        if used + width > columns:
            break
        kept.append(char)
        used += width
    return "".join(kept)


class LiveArea:
    """Render state for "in progress" content: spinner, streaming tail, pending preview
    and the active tool.

    Holds state and plain-text lines only; coloring is assembled on the fly in
    ``formatted()``. All writes happen on the UI event loop (the worker posts through a
    queue), so no locking is needed. The spinner follows a leading blank separator
    line, which supplies the gap below the submitted prompt.

    Commit strategy for streaming content (incremental, same as kimi): split on **blank
    lines**; a closed paragraph is committed to scrollback immediately and the Live area
    keeps only the last open paragraph -- the user sees content scrolling up into
    history continuously instead of the whole block being re-laid-out at turn end.
    """

    def __init__(self) -> None:
        self.turn_active = False
        self.turn_started_at: float = 0.0
        self.stream_pending: str = ""  # partial line of the current open paragraph
        self.open_paragraph: list[str] = []  # complete lines of the current open paragraph
        self.pending_preview: list[str] = []  # pending preview lines (diff/command)
        self.active_tool: str | None = None  # tool name shown on the spinner line
        self.spinner_frame = 0
        self._in_code_fence = False  # inside an unclosed ``` block

    @staticmethod
    def _is_fence(line: str) -> bool:
        return bool(_FENCE_RE.match(line))

    def feed_stream(self, text: str) -> list[str]:
        """Feed streaming text; return the markdown blocks closed in this batch, ready to
        be committed to scrollback.

        Paragraphs are separated by blank lines -- but blank lines inside a fenced code
        block (```) do NOT close the block, so fence state is tracked across batches and
        an unterminated fence keeps accumulating until its closer arrives. Each returned
        item is one block (with its trailing newlines) that the caller can commit as-is.
        """
        committed: list[str] = []
        self.stream_pending += text
        *lines, self.stream_pending = self.stream_pending.split("\n")
        for line in lines:
            line = line.removesuffix("\r")
            in_fence = self._in_code_fence
            if line.strip():
                self.open_paragraph.append(line)
                if self._is_fence(line):
                    self._in_code_fence = not in_fence
            elif in_fence:
                # Blank line inside a code fence: still part of the block -- unless the
                # block was flushed mid-turn and this is its leading blank, which would
                # only add an empty line at the top of the continuation.
                if self.open_paragraph:
                    self.open_paragraph.append(line)
            elif self.open_paragraph:
                # Blank line = paragraph closed, commit the whole block.
                committed.append("\n".join(self.open_paragraph) + "\n\n")
                self.open_paragraph = []
        return committed

    def flush_paragraph(self, *, close_fence: bool = True) -> str:
        """Take the current open block (including a trailing partial line) and clear it.

        ``close_fence=False`` keeps the fence state: a mid-turn flush (prose followed by
        a tool call) must not make a following blank line split a still-open code block.
        """
        lines = list(self.open_paragraph)
        if self.stream_pending:
            lines.append(self.stream_pending)
            self.stream_pending = ""
        self.open_paragraph = []
        if close_fence:
            self._in_code_fence = False
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    def reset_stream(self) -> None:
        self.stream_pending = ""
        self.open_paragraph = []

    def formatted(self, columns: int, now: float) -> list[StyleAndText]:
        """Render the Live window's formatted text (uncommitted content only).

        The first fragment is a blank line, then the spinner. This blank is the exact
        gap between the submitted prompt (scrollback) and the spinner (Live); the prompt
        echo itself ends with a single newline, not a blank line.
        """
        out: list[StyleAndText] = []
        if self.turn_active:
            # The prompt echo ends with one newline; this leading newline is the single
            # blank separator line between that prompt and the spinner.
            out.append(("", "\n"))
            spinner = SPINNER_FRAMES[self.spinner_frame % len(SPINNER_FRAMES)]
            elapsed = max(0, int(now - self.turn_started_at))
            line = f"{spinner} "
            line += self.active_tool if self.active_tool else "thinking"
            line += f" · {elapsed}s"
            out.append(("class:spinner", line + "\n"))
        for preview_line in self.pending_preview:
            out.append(("class:preview", _fit_width(preview_line, columns) + "\n"))
        # Show only the last few lines of the open paragraph -- closed content is
        # already in scrollback.
        for tail in self.open_paragraph[-STREAM_TAIL_LINES:]:
            out.append(("", _fit_width(tail, columns) + "\n"))
        if self.stream_pending:
            out.append(("", _fit_width(self.stream_pending, columns)))
        return out


def preview_lines(event: WireEvent) -> list[str] | None:
    """Confirmation-class events -> Live area preview lines (text truncated)."""
    if isinstance(event, DiffPreview):
        return [f"⏸ diff {event.path} — waiting for approval"]
    if isinstance(event, CommandPreview):
        return [f"⏸ bash {event.command[:60]} — waiting for approval"]
    return None
