"""Live 区状态：worker 产事件，UI 消费后渲染到 prompt 的 message 区。"""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText

from ..wire import CommandPreview, DiffPreview, WireEvent

# Live 区显示的流式尾部行数——整段文本在 turn 结束时提交 scrollback。
STREAM_TAIL_LINES = 3
# spinner 动画帧。
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class LiveArea:
    """ "进行中"内容的渲染状态：spinner 行 + 流式尾部 + 活动/待确认工具。

    只存状态与纯文本行；着色在 ``formatted()`` 里现场拼。所有写操作都
    发生在 UI 事件循环内（worker 经队列投递），无需加锁。

    流式内容的提交策略（增量提交，kimi 同款）：按**空行**切块，已闭合的
    段落立刻提交 scrollback，Live 区只保留最后一段未闭合的内容——用户看到
    的是内容持续往上滚进历史，而不是 turn 结束时整段重排。
    """

    def __init__(self) -> None:
        self.turn_active = False
        self.turn_started_at: float = 0.0
        self.stream_pending: str = ""  # 当前未闭合段落的半行
        self.open_paragraph: list[str] = []  # 当前未闭合段落的完整行
        self.pending_preview: list[str] = []  # 待确认预览行（diff/命令）
        self.active_tool: str | None = None  # spinner 行上的工具名
        self.spinner_frame = 0

    def feed_stream(self, text: str) -> list[str]:
        """喂入流式文本；返回本批中已闭合、可立刻提交 scrollback 的段落。

        段落以空行分隔：遇到空行说明上一段已经写完了。返回的列表每项是
        一个段落（含结尾换行），调用方直接提交即可。
        """
        committed: list[str] = []
        self.stream_pending += text
        *lines, self.stream_pending = self.stream_pending.split("\n")
        for line in lines:
            line = line.removesuffix("\r")
            if line.strip():
                self.open_paragraph.append(line)
            elif self.open_paragraph:
                # 空行 = 段落闭合，整段提交。
                committed.append("\n".join(self.open_paragraph) + "\n\n")
                self.open_paragraph = []
        return committed

    def flush_paragraph(self) -> str:
        """turn 结束时把当前未闭合段落（含末行半行）整段取出并清空。"""
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
        """渲染 Live 区为 prompt message 的 formatted text（只含未提交内容）。"""
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
        # 只显示未闭合段落的尾部几行——已闭合内容已经进了 scrollback。
        for tail in self.open_paragraph[-STREAM_TAIL_LINES:]:
            out.append(("", tail[:columns] + "\n"))
        if self.stream_pending:
            out.append(("", self.stream_pending[:columns]))
        return FormattedText(out)


def _preview_lines(event: WireEvent) -> list[str] | None:
    """待确认类事件 → Live 区预览行（原文截断）。"""
    if isinstance(event, DiffPreview):
        return [f"⏸ diff {event.path} — waiting for approval"]
    if isinstance(event, CommandPreview):
        return [f"⏸ bash {event.command[:60]} — waiting for approval"]
    return None
