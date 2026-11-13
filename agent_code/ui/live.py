"""Live area state: the worker produces events, the UI consumes them and renders them
into the prompt's message area."""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText

from ..wire import CommandPreview, DiffPreview, WireEvent

# Streaming tail lines shown in the Live area -- the whole paragraph is committed to
# scrollback when the turn ends.
STREAM_TAIL_LINES = 3
# Spinner animation frames.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class LiveArea:
    """Render state for "in progress" content: spinner line + streaming tail + active or
    pending tool.

    Holds state and plain-text lines only; coloring is assembled on the fly in
    ``formatted()``. All writes happen on the UI event loop (the worker posts through a
    queue), so no locking is needed.

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

    def feed_stream(self, text: str) -> list[str]:
        """Feed streaming text; return the paragraphs closed in this batch, ready to be
        committed to scrollback.

        Paragraphs are separated by blank lines: a blank line means the previous
        paragraph is complete. Each returned item is one paragraph (with its trailing
        newlines) that the caller can commit as-is.
        """
        committed: list[str] = []
        self.stream_pending += text
        *lines, self.stream_pending = self.stream_pending.split("\n")
        for line in lines:
            line = line.removesuffix("\r")
            if line.strip():
                self.open_paragraph.append(line)
            elif self.open_paragraph:
                # Blank line = paragraph closed, commit the whole paragraph.
                committed.append("\n".join(self.open_paragraph) + "\n\n")
                self.open_paragraph = []
        return committed

    def flush_paragraph(self) -> str:
        """At turn end, take the current open paragraph (including a trailing partial
        line) and clear it."""
        lines = list(self.open_paragraph)
        if self.stream_pending:
            lines.append(self.stream_pending)
            self.stream_pending = ""
        self.open_paragraph = []
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    def reset_stream(self) -> None:
        self.stream_pending = ""
        self.open_paragraph = []

    def formatted(self, columns: int, now: float) -> FormattedText:
        """Render the Live area as the prompt message's formatted text (uncommitted
        content only)."""
        out: list[tuple[str, str]] = []
        if self.turn_active:
            spinner = SPINNER_FRAMES[self.spinner_frame % len(SPINNER_FRAMES)]
            elapsed = max(0, int(now - self.turn_started_at))
            line = f"{spinner} "
            line += self.active_tool if self.active_tool else "thinking"
            line += f" · {elapsed}s"
            out.append(("class:spinner", line + "\n"))
        for preview_line in self.pending_preview:
            out.append(("class:preview", preview_line[:columns] + "\n"))
        # Show only the last few lines of the open paragraph -- closed content is
        # already in scrollback.
        for tail in self.open_paragraph[-STREAM_TAIL_LINES:]:
            out.append(("", tail[:columns] + "\n"))
        if self.stream_pending:
            out.append(("", self.stream_pending[:columns]))
        return FormattedText(out)


def _preview_lines(event: WireEvent) -> list[str] | None:
    """Confirmation-class events -> Live area preview lines (text truncated)."""
    if isinstance(event, DiffPreview):
        return [f"⏸ diff {event.path} — waiting for approval"]
    if isinstance(event, CommandPreview):
        return [f"⏸ bash {event.command[:60]} — waiting for approval"]
    return None
