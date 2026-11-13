"""已完成内容 → 原生终端 scrollback 的提交路径。

OutputChunk 先经 Rich 渲染成 ANSI，再走 ``sys.stdout.write``（在
patch_stdout 内）落到 prompt 上方的 scrollback——原生滚动/选区/复制，
pt 不再管理历史区。
"""

from __future__ import annotations

import sys
from io import StringIO

from prompt_toolkit.formatted_text import StyleAndTextTuples
from rich.console import Console
from rich.text import Text

from ..output import OutputChunk, chunk_fragments

_ansi_console = Console(
    file=StringIO(),
    force_terminal=True,
    color_system="truecolor",
    highlight=False,
)


def _chunk_fragments(chunk: OutputChunk) -> StyleAndTextTuples:
    """OutputChunk → (样式串, 文本) 片段（样式串走 Rich 词表）。"""
    if chunk.fragments:
        return list(chunk.fragments)
    if chunk.format == "rich":
        return chunk_fragments(chunk)
    return [("", chunk.text)]


def _chunk_to_ansi(chunk: OutputChunk) -> str:
    """OutputChunk → ANSI 字符串。

    TranscriptLexer 那套 pt 片段翻译已随输出面板一起退役；这里把片段还原成
    Rich Text 再渲染成 ANSI——颜色表与 pt 内联样式串同源（命名色/hex 通用）。
    """
    text = Text()
    for style, fragment in _chunk_fragments(chunk):
        text.append(fragment, style=style)
    with _ansi_console.capture() as capture:
        _ansi_console.print(text, end="")
    return capture.get()


def _commit_chunk(chunk: OutputChunk) -> None:
    """把 OutputChunk 打到 prompt 上方的终端 scrollback。

    走 ``sys.stdout.write``：``patch_stdout`` 把 stdout 换成了 StdoutProxy，
    它用自带 flush 线程经 run_in_terminal 绘制到 prompt 上方——这是唯一
    thread-safe 且不会与 prompt_async 抢输入的提交路径。

    不要用 pt 的 ``print_formatted_text``：app 运行时它会走
    ``loop.call_soon_threadsafe(run_in_terminal(...))`` 且调用方无法等待，
    在"提交完立刻进下一轮 prompt_async"的循环里会和 prompt 的输入
    detach/attach 竞争，表现为后续输入全部失效。
    """
    sys.stdout.write(_chunk_to_ansi(chunk))
    sys.stdout.flush()
