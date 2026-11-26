"""Translation of finished wire events into OutputChunks (coloring is decided here).

Tool results are the exception to "one event, one line": a burst of finished tool
calls folds into a single summary line (``tool_fold_chunk``), so a long run of tools
does not push everything else out of the scrollback. The burst boundary is decided by
the consumer (the REPL commits the fold when prose resumes or the turn ends).
"""

from __future__ import annotations

from rich.markup import escape

from ..output import OutputChunk, StyleAndText
from ..prompt_ui import render_diff
from ..wire import (
    CommandPreview,
    ContextCompacted,
    DiffPreview,
    Line,
    PromptEcho,
    ToolCallCompleted,
    WireEvent,
)

# Tool outcome -> marker glyph. "ok" is the executed-and-succeeded case; the others
# refine ``ToolCallCompleted.outcome`` / ``is_error``.
_TOOL_MARKS = {"ok": "✓", "error": "✗", "declined": "⊘", "skipped": "⊘"}

# Distinct tool names listed in a folded burst before the list is elided. Keeps the
# summary near one terminal line for long tool chains.
_MAX_FOLD_NAMES = 6


def tool_outcome(event: ToolCallCompleted) -> str:
    """Map a settled tool call to its summary status."""
    if event.outcome in ("declined", "skipped"):
        return event.outcome
    return "error" if event.is_error else "ok"


def format_tool_fold(items: list[tuple[str, str]]) -> str:
    """One compact line for a burst of finished tool calls.

    ``items`` is an ordered list of ``(tool_name, status)`` with status in
    ``{"ok", "error", "declined", "skipped"}``. A single call keeps its own line
    (``✓ bash``); a burst becomes ``✓ 7 tools: bash ×4, git_diff ×2, read_file``.
    Repeated names are counted, distinct names capped at ``_MAX_FOLD_NAMES``.
    """
    if not items:
        return ""

    if len(items) == 1:
        name, status = items[0]
        mark = _TOOL_MARKS.get(status, "✓")
        suffix = "" if status == "ok" else f" ({status})"
        return f"{mark} {name}{suffix}"

    counts: dict[tuple[str, str], int] = {}
    order: list[tuple[str, str]] = []
    for key in items:
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += 1

    parts: list[str] = []
    for key in order[:_MAX_FOLD_NAMES]:
        name, status = key
        part = f"{name} ×{counts[key]}" if counts[key] > 1 else name
        if status != "ok":
            part += f" ({status})"
        parts.append(part)
    if len(order) > _MAX_FOLD_NAMES:
        parts.append("…")

    mark = "✗" if any(status != "ok" for _, status in items) else "✓"
    return f"{mark} {len(items)} tools: " + ", ".join(parts)


def tool_fold_chunk(items: list[tuple[str, str]]) -> OutputChunk | None:
    """Fold a burst of finished tool calls into one OutputChunk (None when empty)."""
    line = format_tool_fold(items)
    return OutputChunk(line + "\n") if line else None


def event_to_chunks(event: WireEvent) -> list[OutputChunk]:
    """Translate one finished wire event into a list of OutputChunks (coloring decided here)."""
    if isinstance(event, Line):
        return [OutputChunk(event.text + "\n", format="rich" if event.markup else "plain")]
    if isinstance(event, PromptEcho):
        return [render_user_prompt(event.text)]
    if isinstance(event, ToolCallCompleted):
        # Single-call path (one-shot frontend / a lone call); the REPL folds bursts
        # before they reach this function.
        chunk = tool_fold_chunk([(event.name, tool_outcome(event))])
        return [chunk] if chunk is not None else []
    if isinstance(event, DiffPreview):
        return [
            # Paths are tool arguments: escape them so a bracket in a filename is
            # rendered literally instead of being parsed as Rich markup.
            OutputChunk(f"\nDiff for {escape(event.path)}:", format="rich"),
            OutputChunk(render_diff(event.old, event.new, event.path) + "\n", format="rich"),
        ]
    if isinstance(event, CommandPreview):
        return [OutputChunk(f"\nCommand: {event.command}\n", format="rich")]
    if isinstance(event, ContextCompacted):
        return [OutputChunk(f"[dim]{event.describe()}[/dim]\n", format="rich")]
    return []


def render_user_prompt(text: str) -> OutputChunk:
    normalized = text.rstrip()
    if not normalized.strip():
        return OutputChunk("")
    body = "\n".join(f"> {line}" for line in normalized.splitlines())
    # Bold white on dark grey, padded to the terminal width by the commit path.
    # Fragments use Rich's inline style vocabulary, which the commit path hands to
    # rich.text.Text as-is. The prompt echo ends with a single newline: the Live area
    # adds the blank separator line before its spinner, so the visual gap is exactly
    # one line.
    style = "bold white on #444444"
    fragments: tuple[StyleAndText, ...] = (("", "\n\n"), (style, body), ("", "\n"))
    return OutputChunk("\n\n" + body + "\n", fragments=fragments, full_width=True)
