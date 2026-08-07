from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Any, Callable, Literal

from rich.console import Console

OutputFormat = Literal["plain", "ansi"]


@dataclass(frozen=True)
class OutputChunk:
    text: str
    format: OutputFormat = "plain"


# agent 输出的统一出口：接收纯文本或渲染好的 ANSI chunk，由调用方决定如何显示
# （一次性模式直接打终端，REPL 模式跨线程喂给 prompt_toolkit UI 面板）。
OutputWriter = Callable[[str | OutputChunk], None]


def render_console_chunk(
    *objects: Any,
    styled: bool,
    width: int = 120,
    **kwargs: Any,
) -> OutputChunk:
    buffer = StringIO()
    if styled:
        Console(
            file=buffer,
            force_terminal=True,
            color_system="standard",
            width=width,
        ).print(*objects, **kwargs)
        return OutputChunk(buffer.getvalue(), format="ansi")

    Console(file=buffer, no_color=True, width=width).print(*objects, **kwargs)
    return OutputChunk(buffer.getvalue(), format="plain")
