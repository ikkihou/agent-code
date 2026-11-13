"""已完成 wire 事件 → OutputChunk 的翻译（着色在这里决定）。"""

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
    """把一个已完成 wire 事件翻译成 OutputChunk 列表（着色在这里决定）。"""
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
    # 白字深灰底反白高亮（source 头当前不再渲染，参数仅作兼容保留）。
    # scrollback 模式下不做逐行背景补位（原 BufferControl 的专属技巧）。
    style = "ansiwhite bg:#444444"
    fragments: tuple[StyleAndText, ...] = (("", "\n\n"), (style, body), ("", "\n\n"))
    return OutputChunk("\n\n" + body + "\n\n", fragments=fragments)
