"""Agent loop ↔ 前端的解耦通道（借鉴 kimi-cli 的 wire 架构）。

run_agent / 工具层只通过 ``Wire.send()`` 发布结构化事件——不碰终端、不知道
前端存在；前端（REPL 输出面板、one-shot 直打、未来的录制重放）是纯事件
消费者：收到事件决定怎么渲染。

事件分两类：

- **内容事件**：``TextDelta``（模型流式原文，队列内相邻 delta 原地合并，
  生产快于消费时不积压）、``Line``（一行完整通知，可带 Rich markup）、
  ``PromptEcho``（用户/cron 输入的回显块）。
- **展示事件**：``ToolCallStarted`` / ``DiffPreview`` / ``CommandPreview``
  ——工具层只描述"发生了什么"，着色、diff 样式、行格式完全由前端决定。

Wire 的消费模型：生产端（worker 线程）send；消费端设置 ``sink``（接收事件
批）。两种节奏：

- 同步消费（one-shot）：只设 ``sink``，send 时立刻 flush 给 sink；
- 异步消费（REPL）：设 ``sink`` + ``notify``，send 只触发 notify（前端把它
  调度到 UI 线程），UI 线程 drain() 后交给 sink——合并缓冲因此生效。
"""

# here put the import lib
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 事件类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextDelta:
    """模型流式文本增量（未经行缓冲的原文，消费端自行攒行）。"""

    text: str


@dataclass(frozen=True)
class Line:
    """一行完整的 agent 通知（continue/final/hook/error 等）。

    markup=True 表示 text 是 Rich markup，由前端各自渲染；
    trace（AgentResult.trace）在 agent 侧另行记录，与事件无关。
    """

    text: str
    markup: bool = False


@dataclass(frozen=True)
class PromptEcho:
    """用户输入（或 cron 到点 prompt）的回显块，前端渲染成高亮 prompt 行。"""

    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """一次工具调用开始。args_preview 是截断后的参数预览（仅展示用）。"""

    name: str
    args_preview: str


@dataclass(frozen=True)
class DiffPreview:
    """一次文件写入/编辑的 diff 预览（原文，前端负责着色渲染）。"""

    path: str
    old: str
    new: str


@dataclass(frozen=True)
class CommandPreview:
    """一条待确认的 bash 命令（原文，前端负责渲染）。"""

    command: str


WireEvent = TextDelta | Line | PromptEcho | ToolCallStarted | DiffPreview | CommandPreview

# 消费端回调：接收一批事件（保证按 send 顺序排列）。
EventSink = Callable[[list[WireEvent]], None]


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------


class Wire:
    """单向事件通道：生产端 send（可多线程），消费端经 sink 收批。

    合并语义：send(TextDelta) 时若队尾尚未消费的也是 TextDelta，则原地拼接
    而不是追加新事件——流式期间 UI 消费不及时也不会积压几百条 delta。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[WireEvent] = []
        # sink：事件批消费端；notify：有新事件时的唤醒钩子（前端把它调度到
        # 自己的线程再 drain）。两者都为空时事件直接丢弃（fire-and-forget）。
        self.sink: EventSink | None = None
        self.notify: Callable[[], None] | None = None

    def send(self, event: WireEvent) -> None:
        with self._lock:
            if (
                isinstance(event, TextDelta)
                and self._pending
                and isinstance(self._pending[-1], TextDelta)
            ):
                self._pending[-1] = TextDelta(self._pending[-1].text + event.text)
            else:
                self._pending.append(event)
        notify = self.notify
        if notify is not None:
            notify()
            return
        sink = self.sink
        if sink is not None:
            # 同步消费：立刻 flush（one-shot / 测试场景）。
            batch = self.drain()
            if batch:
                sink(batch)

    def drain(self) -> list[WireEvent]:
        """原子取走全部待消费事件（保持 send 顺序）。"""
        with self._lock:
            batch, self._pending = self._pending, []
            return batch


# ---------------------------------------------------------------------------
# 流式行缓冲（消费端用）
# ---------------------------------------------------------------------------


class StreamLineBuffer:
    """把 TextDelta 原文攒成完整行的缓冲——由前端持有，不属于 agent。

    agent 现在只发原始 delta；行边界是渲染关注点（prompt_toolkit 不能被
    覆写半行、one-shot 要整行打印），所以挪到消费端。
    """

    def __init__(self, on_line: Callable[[str], None]) -> None:
        self._on_line = on_line
        self.pending: str = ""
        self.started: bool = False

    def feed(self, text: str) -> None:
        if not text:
            return

        self.started = True
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self._on_line(line.removesuffix("\r"))

    def finish(self) -> None:
        if self.pending:
            self._on_line(self.pending)
            self.pending = ""
