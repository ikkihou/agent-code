"""agent 输出的规范表示与样式转换。

OutputChunk 是所有前端（one-shot 直打 / REPL 面板 / session 转录持久化）共享的
唯一输出单元：纯文本 + 可选样式片段。样式片段直接使用 prompt_toolkit 的内联
样式串（"bold yellow"、"bg:#444444"）——live 路径不再经过 ANSI：Rich 只负责
把 markup 文本解析成带样式信息的 Text，翻译成片段后由各前端自行渲染。
"ansi" 格式仅为回放旧版 transcript 记录保留。
"""

# here put the import lib
from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Callable, Literal

from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles.style import parse_color
from rich.color import Color
from rich.console import Console

OutputFormat = Literal["plain", "ansi", "rich"]

# (样式串, 文本) 片段；样式串用 prompt_toolkit 内联样式词表，如 "bold yellow"。
StyleAndText = tuple[str, str]

# agent 输出的统一出口：接收完整行的 OutputChunk（流式文本已由 agent.py 的
# _LineBufferedStreamRenderer 攒成整行，且 text 自带行尾换行），由前端决定
# 如何显示——一次性模式直接打终端，REPL 模式跨线程喂给 prompt_toolkit 面板。
OutputWriter = Callable[["OutputChunk"], None]


@dataclass(frozen=True)
class OutputChunk:
    """一次完整输出。

    - text 永远是无 ANSI 纯文本（持久化、剪贴板复制、纯文本消费方都用它）；
    - fragments 非空时即为最终样式，前端直接采用，不再做任何解析；
    - format == "rich" 表示 text 是 Rich markup（如 ``[bold]x[/bold]``），
      由前端渲染：REPL 转片段、one-shot 交给 Rich；
    - format == "ansi" 表示 text 内嵌 ANSI 转义，仅旧 transcript 回放出现。
    """

    text: str
    format: OutputFormat = "plain"
    fragments: tuple[StyleAndText, ...] = ()


# ---------------------------------------------------------------------------
# Rich markup → 样式片段
# ---------------------------------------------------------------------------

_offscreen: Console | None = None


def _get_offscreen_console() -> Console:
    """进程内共享的离屏 Console：只用来把 markup 解析成 Text/segments，
    从不真正写文件（render/render_str 不做文件 IO）。宽度放大到 4096 让
    长行不在片段层折行——折行交给前端（REPL 面板按自身宽度 wrap，
    one-shot 由终端软折行）。"""
    global _offscreen
    if _offscreen is None:
        _offscreen = Console(
            file=StringIO(),
            force_terminal=True,
            color_system="truecolor",
            width=4096,
        )
    return _offscreen


# prompt_toolkit 接受的属性 token（styles/style.py::_parse_style_str 的词表）。
_PT_ATTR_TOKENS = {
    "bold", "nobold", "italic", "noitalic", "underline", "nounderline",
    "strike", "nostrike", "blink", "noblink", "reverse", "noreverse",
    "hidden", "nohidden", "dim", "nodim",
}


def _pt_color(token: str) -> str:
    """rich 颜色 token → pt 颜色 token。

    命名色（red/green/...）和 hex（#444444）两边记法相同，直接透传；
    rich 的 ``color(N)``（256 色）pt 不认识，转成等值 hex。0-15 的标准色
    依赖终端配色、无等值 hex，返回空串（丢弃）。
    """
    if token.startswith("color(") and token.endswith(")"):
        try:
            triplet = Color.parse(token).triplet
        except Exception:
            return ""
        if triplet is None:
            return ""
        return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"
    return token


def _pt_style(style: object) -> str:
    """rich Style → prompt_toolkit 内联样式串。

    两边词表大部分同源（bold/dim/red/...），差异有二：背景色 rich 写
    ``on <color>``、pt 写 ``bg:<color>``；``color(N)`` 需转 hex。pt 不认识
    的 token（如 rich 关闭态的 "not"）直接丢弃——样式缺失远好过渲染崩掉。
    """
    if style is None:
        return ""
    tokens: list[str] = []
    pending_bg = False
    for token in str(style).split():
        if token == "on":
            pending_bg = True
            continue
        if pending_bg:
            pending_bg = False
            translated = "bg:" + _pt_color(token)
        elif token in _PT_ATTR_TOKENS:
            translated = token
        else:
            translated = _pt_color(token)
        # 校验颜色 token 必须是 pt 认识的（hex / 命名色 / ansi 名）。
        if translated in _PT_ATTR_TOKENS or translated.startswith("bg:"):
            tokens.append(translated)
        else:
            try:
                parse_color(translated)
            except ValueError:
                continue
            tokens.append(translated)
    return " ".join(tokens)


def rich_markup_fragments(markup_text: str) -> list[StyleAndText]:
    """Rich markup 文本 → 样式片段列表（不经过 ANSI）。"""
    console = _get_offscreen_console()
    text = console.render_str(markup_text, markup=True, highlight=False)
    return [
        (_pt_style(segment.style), segment.text)
        for segment in console.render(text, console.options)
        if segment.text
    ]


# ---------------------------------------------------------------------------
# chunk → 片段的归一化入口
# ---------------------------------------------------------------------------

def chunk_fragments(chunk: OutputChunk) -> list[StyleAndText]:
    """把任意 chunk 归一化成样式片段——transcript / lexer 的唯一转换入口。"""
    if chunk.fragments:
        return list(chunk.fragments)
    if chunk.format == "rich":
        return rich_markup_fragments(chunk.text)
    if chunk.format == "ansi":
        # 旧版 transcript 记录回放：ANSI 字符串解析回片段。
        return [(style, text) for style, text, *_ in ANSI(chunk.text).__pt_formatted_text__()]
    if chunk.text:
        return [("", chunk.text)]
    return []
