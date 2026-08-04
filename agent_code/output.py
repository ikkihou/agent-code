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
