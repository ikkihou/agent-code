"""Translation of finished wire events into OutputChunks (coloring is decided here)."""

from __future__ import annotations

from prompt_toolkit.formatted_text import StyleAndTextTuples

from ..output import OutputChunk
from ..prompt_ui import render_diff
from ..wire import (
    CommandPreview,
    DiffPreview,
    Line,
    PromptEcho,
    ToolCallCompleted,
    WireEvent,
)

StyleAndText = tuple[str, str]


def event_to_chunks(event: WireEvent) -> list[OutputChunk]:
    """Translate one finished wire event into a list of OutputChunks (coloring decided here)."""
    if isinstance(event, Line):
        return [OutputChunk(event.text + "\n", format="rich" if event.markup else "plain")]
    if isinstance(event, PromptEcho):
        return [render_user_prompt(event.text)]
    if isinstance(event, ToolCallCompleted):
        return [
            OutputChunk(
                f"✓ {event.name}" + (" (error)" if event.is_error else "") + "\n",
                format="rich",
            )
        ]
    if isinstance(event, DiffPreview):
        return [
            OutputChunk(f"\nDiff for {event.path}:", format="rich"),
            OutputChunk(render_diff(event.old, event.new, event.path) + "\n", format="rich"),
        ]
    if isinstance(event, CommandPreview):
        return [OutputChunk(f"\nCommand: {event.command}\n", format="rich")]
    return []


def render_user_prompt(text: str, *, source: str = "user") -> OutputChunk:
    normalized = text.rstrip()
    if not normalized.strip():
        return OutputChunk("")
    body = "\n".join(f"> {line}" for line in normalized.splitlines())
    # Bold white on dark grey, padded to the terminal width by the scrollback commit
    # path. Fragments use Rich's inline style vocabulary, which the commit path hands
    # to rich.text.Text as-is. The prompt echo ends with a single newline: the Live
    # area adds the blank separator line before its spinner, so the visual gap is
    # exactly one line. The source header is no longer rendered; the parameter is
    # kept for compatibility only.
    style = "bold white on #444444"
    fragments: tuple[StyleAndText, ...] = (("", "\n\n"), (style, body), ("", "\n"))
    return OutputChunk("\n\n" + body + "\n", fragments=fragments, full_width=True)
