"""
File      :   interactive.py
Date      :   2026-07-22 15:28:36
Author    :   baoyihui
Contact   :   yihui.bao@apopgeei.com

scrollback + Live 式 REPL（借鉴 kimi-cli 的终端所有权模型）：

- pt PromptSession（prompt_async）是终端唯一所有者：输入行 + message 区
  （Live：spinner / 流式尾部 / 待确认预览）+ bottom_toolbar 状态栏。
- 已完成的内容经 ``sys.stdout.write``（在 patch_stdout 内）落到原生终端
  scrollback——原生滚动/选区/复制，pt 不再管理历史区，也没有全量重设
  Document 的成本（正是输出面板性能档案里"正解方向"的落地）。
- wire 事件：worker 线程 send → call_soon_threadsafe 桥 → UI 协程消费。
  已完成事件立刻提交 scrollback；流式 TextDelta 只保留尾部几行显示在
  Live 区，turn 结束时整段提交。
"""

# here put the import lib
from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
import traceback
from io import StringIO
from typing import Any, Callable

from prompt_toolkit.application.current import get_app_or_none, get_app_session
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, FormattedText, StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.text import Text

from .output import OutputChunk, chunk_fragments
from .prompt_ui import render_diff
from .runtime import PromptFallback, PromptRequest, RuntimeState
from .slash import SlashContext, SlashRegistry, default_slash_registry, dispatch_slash
from .wire import (
    CommandPreview,
    DiffPreview,
    Line,
    PromptEcho,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
    Wire,
    WireEvent,
)

PendingPrompt = dict[str, Any]

# Live 区显示的流式尾部行数——整段文本在 turn 结束时提交 scrollback。
STREAM_TAIL_LINES = 3
# spinner 动画帧。
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class SlashCompleter(Completer):
    def __init__(self, registry: SlashRegistry | None = None) -> None:
        self._registry = registry or default_slash_registry()

    def get_completions(self, document: Document, complete_event: Any) -> Any:
        text_before_cursor = document.text_before_cursor
        if not text_before_cursor.startswith("/") or any(
            char.isspace() for char in text_before_cursor
        ):
            return

        prefix = text_before_cursor[1:]
        for command in self._registry.commands():
            if command.name.startswith(prefix):
                yield Completion(
                    f"/{command.name}",
                    start_position=-len(text_before_cursor),
                    display=f"/{command.name}",
                    display_meta=command.description,
                )


# ---------------------------------------------------------------------------
# 输出片段 → 终端 scrollback
# ---------------------------------------------------------------------------

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
    if chunk.format == "ansi":
        # 旧版 transcript 记录回放：ANSI 字符串解析回片段。
        return ANSI(chunk.text).__pt_formatted_text__()
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


# ---------------------------------------------------------------------------
# Live 区状态：worker 产事件，UI 消费后渲染到 prompt 的 message 区
# ---------------------------------------------------------------------------


class LiveArea:
    """"进行中"内容的渲染状态：spinner 行 + 流式尾部 + 活动/待确认工具。

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
            spinner = _SPINNER_FRAMES[self.spinner_frame % len(_SPINNER_FRAMES)]
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


# ---------------------------------------------------------------------------
# 完成内容 → scrollback 的翻译（worker 事件 → chunk）
# ---------------------------------------------------------------------------


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


StyleAndText = tuple[str, str]


def parse_confirm_answer(text: str, default: bool = False) -> bool | None:
    normalized = text.strip().lower()
    if not normalized:
        return default
    if normalized in {"y", "yes"}:
        return True
    if normalized in {"n", "no"}:
        return False
    return None


def parse_choice_index(text: str, option_count: int, default: int = 0) -> int | None:
    normalized = text.strip()
    if not normalized:
        return default
    try:
        idx = int(normalized)
    except ValueError:
        return None
    if 0 <= idx <= option_count:
        return idx
    return None


def render_prompt_request(request: PromptRequest) -> str:
    request_type = request.get("type")
    body = str(request.get("body") or "")
    parts: list[str] = []
    if body:
        parts.append(body.rstrip())

    if request_type == "choice":
        question = str(request.get("question") or "")
        labels = request.get("labels") or []
        parts.append(f"? {question}")
        parts.extend(f"  {i}. {label}" for i, label in enumerate(labels, 1))
        parts.append("  0. Skip / Other")
    else:
        message = str(request.get("message") or "Confirm?")
        default = bool(request.get("default", False))
        suffix = "[Y/n]" if default else "[y/N]"
        parts.append(f"? {message} {suffix}")

    return "\n".join(parts).rstrip() + "\n"


def prompt_for_request(request: PromptRequest) -> str:
    if request.get("type") == "choice":
        default = int(request.get("default", 0))
        return f"Choice [{default}]: "
    default = bool(request.get("default", False))
    return "Confirm [Y/n]: " if default else "Confirm [y/N]: "


# ---------------------------------------------------------------------------
# 状态栏
# ---------------------------------------------------------------------------


def bottom_toolbar(state: RuntimeState) -> str:
    """底部状态栏——当前模式 + 模型。"""
    mode = {"default": "default", "acceptEdits": "accept edits", "plan": "plan"}.get(
        state.permission_mode, state.permission_mode
    )
    active = next((t.active_form for t in state.todo_store if t.status == "in_progress"), "")
    todo = f" · {active}" if active else ""
    return f" {mode} · {state.model}{todo} "


# ---------------------------------------------------------------------------
# REPL 主体
# ---------------------------------------------------------------------------


class InteractiveShell:
    """scrollback+Live 式 REPL。

    线程模型：主线程跑 asyncio 事件循环（prompt_async + 事件消费协程）；
    worker 线程跑 run_agent / cron 处理，经 wire（sink → call_soon_
    threadsafe 桥）与 ``_job_queue`` 通信。
    """

    def __init__(
        self,
        state: RuntimeState,
        run_turn: Callable[[str, Wire], None],
        make_slash_context: Callable[[], SlashContext],
        initial_output: str = "",
        initial_transcript: list[OutputChunk] | None = None,
        on_transcript: Callable[[OutputChunk], None] | None = None,
        drain_pending: Callable[[], list[str]] | None = None,
        slash_registry: SlashRegistry | None = None,
    ) -> None:
        self._state = state
        self._run_turn = run_turn
        self._make_slash_context = make_slash_context
        self._on_transcript = on_transcript
        self._drain_pending = drain_pending
        self._slash_registry = slash_registry

        self._job_queue: queue.Queue[str] = queue.Queue()
        self._busy = threading.Event()
        self._turn_wire: Wire | None = None

        # UI 协程消费的事件队列（worker 经 call_soon_threadsafe 投递）。
        self._event_queue: asyncio.Queue[WireEvent] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: PromptSession[str] | None = None
        self._live = LiveArea()
        self._live_dirty = asyncio.Event()
        # pending 提问（confirm/choice）状态机：UI 线程写，worker 等待。
        self._pending_prompt: PendingPrompt | None = None
        self._exit_requested = False
        # 初始 transcript（resume 重放）：启动后先提交到 scrollback。
        self._initial_chunks: list[OutputChunk] = list(initial_transcript or [])
        if initial_output:
            self._initial_chunks.insert(0, OutputChunk(initial_output))

    # ---- worker 线程侧：turn 执行与 cron ------------------------------------

    def _work_loop(self) -> None:
        while True:
            if self._drain_pending is None:
                text = self._job_queue.get()
            else:
                try:
                    text = self._job_queue.get(timeout=1.0)
                except queue.Empty:
                    text = None

            if text == "__EXIT__":
                break

            if text is not None:
                self._state.abort_event.clear()
                self._busy.set()
                try:
                    wire = Wire()
                    self._turn_wire = wire
                    wire.sink = self._worker_sink
                    self._worker_sink([TurnStarted()])
                    self._run_turn(text, wire)
                except Exception as e:  # provider/工具异常别让 worker 静默死掉
                    # 带上 traceback：只有 str(e) 时无法定位是哪条路径抛的
                    # （编码类错误尤其明显——报错文案里看不出调用栈）。
                    detail = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                    self._worker_sink([Line(f"[error] {e}\n{detail}", markup=True)])
                finally:
                    self._worker_sink([TurnEnded()])
                    self._turn_wire = None
                    self._busy.clear()

            while not self._state.input_queue.empty():
                self._job_queue.put(self._state.input_queue.get())

            # cron 到点 prompt：与用户输入一样走 slash 分发或作为新一轮 turn。
            # worker 在 get(timeout=1.0) 上空闲轮询，cron 不依赖用户输入触发。
            if self._drain_pending is not None:
                for prompt in self._drain_pending():
                    self._handle_cron_prompt(prompt)

    def _worker_sink(self, batch: list[WireEvent]) -> None:
        """worker 线程的 wire sink：把事件批投递到 UI 事件循环。

        同步 sink 模式：run_agent 每次 send 都 flush 一个批到这里。
        """
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._enqueue_events, batch)

    def _enqueue_events(self, batch: list[WireEvent]) -> None:
        for event in batch:
            self._event_queue.put_nowait(event)
        self._live_dirty.set()

    def _handle_cron_prompt(self, text: str) -> None:
        """worker 线程处理 cron 到点 prompt：slash 就地分发，普通 prompt 作为新一轮 turn。"""
        text = text.strip()
        if not text or text == "/exit":
            return  # cron 到点 prompt 不触发应用退出
        if text.startswith("/"):
            result = dispatch_slash(text, self._make_slash_context())
            if result.handled and result.message:
                self._worker_sink([Line(str(result.message), markup=result.markup)])
            if result.should_query:
                self._worker_sink([PromptEcho(result.prompt)])
                self._job_queue.put(result.prompt)
            return
        self._worker_sink([PromptEcho(text)])
        self._job_queue.put(text)

    # ---- UI 协程：事件消费 + Live 渲染 + scrollback 提交 ---------------------

    def _ui_style(self) -> Style:
        return Style.from_dict(
            {
                "spinner": "bold ansibrightcyan",
                "preview": "ansiyellow",
                "bottom-toolbar": "bg:#222222 ansiwhite",
            }
        )

    async def _consume_events(self) -> None:
        """UI 事件消费协程：已完成事件提交 scrollback，流式事件更新 Live 区。"""
        while True:
            await self._live_dirty.wait()
            self._live_dirty.clear()
            # 一次取空：已完成事件合并提交，减少 scrollback 重绘次数。
            batch: list[WireEvent] = []
            while not self._event_queue.empty():
                batch.append(self._event_queue.get_nowait())
            if not batch:
                continue

            commit: list[OutputChunk] = []
            for event in batch:
                if isinstance(event, TextDelta):
                    # 增量提交：已闭合的段落立刻进 scrollback，Live 区只留尾段。
                    for paragraph in self._live.feed_stream(event.text):
                        commit.append(OutputChunk(paragraph))
                    continue
                if isinstance(event, TurnStarted):
                    self._live.turn_active = True
                    self._live.turn_started_at = time.monotonic()
                    self._live.reset_stream()
                    self._live.active_tool = None
                    continue
                if isinstance(event, TurnEnded):
                    # 收尾：未闭合段落整段提交（通常只有最后几行）。
                    tail = self._live.flush_paragraph()
                    self._live.turn_active = False
                    self._live.active_tool = None
                    self._live.pending_preview = []
                    if tail.strip():
                        commit.append(OutputChunk(tail))
                    continue
                if isinstance(event, ToolCallStarted):
                    self._live.active_tool = event.name
                    continue
                if isinstance(event, ToolCallCompleted):
                    self._live.active_tool = None
                preview = _preview_lines(event)
                if preview is not None:
                    self._live.pending_preview = preview
                    continue
                # 其余都是"已完成"事实 → scrollback。
                commit.extend(event_to_chunks(event))

            if commit:
                self._commit_chunks(commit)
            self._invalidate()

    def _invalidate(self) -> None:
        app = get_app_or_none()
        if app is not None:
            app.invalidate()

    async def _animate_spinner(self) -> None:
        """turn 活跃期间周期性刷新 spinner 与计时。"""
        while True:
            await asyncio.sleep(0.12)
            if self._live.turn_active:
                self._live.spinner_frame += 1
                self._invalidate()

    # ---- 提问（asker 路径）---------------------------------------------------

    async def _ask_in_app(self, request: PromptRequest) -> Any:
        """在 UI 协程内发起提问：问题与提示符都挂进 Live 区，等 worker 回答。

        不写 stdout：StdoutProxy 的 run_in_terminal 会 detach 输入，与正在
        运行的 prompt_async 抢输入流（实测会把后续输入吃掉、prompt 永不
        返回）。问题正文放 Live 区（prompt message），同理可见且零竞争。
        """
        future: asyncio.Future[Any] = self._loop.create_future()  # type: ignore[union-attr]
        self._pending_prompt = {"request": request, "future": future}
        self._live.pending_preview = render_prompt_request(request).rstrip().splitlines()
        self._invalidate()
        try:
            return await future
        finally:
            self._live.pending_preview = []
            self._invalidate()

    def _terminal_asker(self, func: PromptFallback | PromptRequest) -> Any:
        """worker 线程里的提问入口：结构化请求走 app 内提问，否则退回 run_in_terminal。"""
        if isinstance(func, dict):
            return asyncio.run_coroutine_threadsafe(self._ask_in_app(func), self._loop).result()
        # run_in_terminal 本身返回 coroutine，无需再包一层 async 函数。
        from prompt_toolkit.application import run_in_terminal

        return asyncio.run_coroutine_threadsafe(run_in_terminal(func), self._loop).result()

    # ---- 输入提交（UI 线程）---------------------------------------------------

    def _submit_text(self, text: str) -> None:
        """统一输入入口：有 pending 提问时先喂给提问状态机，否则作为新输入提交。"""
        text = text.strip()
        if self._pending_prompt is not None:
            entry = self._pending_prompt
            request = entry["request"]
            future: asyncio.Future[Any] = entry["future"]
            request_type = request.get("type")

            if request_type == "choice":
                labels = request.get("labels") or []
                idx = parse_choice_index(text, len(labels), int(request.get("default", 0)))
                if idx is None:
                    self._commit_now("Invalid choice. Enter a listed number.\n")
                    return
                self._pending_prompt = None
                result = str(idx)
                if not future.done():
                    future.set_result(result)
                self._commit_now("Choice: skip\n" if idx == 0 else f"Choice: {labels[idx - 1]}\n")
                return

            answer = parse_confirm_answer(text, bool(request.get("default", False)))
            if answer is None:
                self._commit_now("Invalid answer. Enter y or n.\n")
                return
            self._pending_prompt = None
            if not future.done():
                future.set_result(answer)
            self._commit_now(f"Answer: {'yes' if answer else 'no'}\n")
            return

        if not text:
            return

        if text == "/exit":
            self._exit_requested = True
            raise EOFError  # 跳出 _submit_text，由 prompt 循环捕获并退出

        if text.startswith("/"):
            self._route_slash_command(text)
            return

        self._echo_user_prompt(text)
        if self._busy.is_set():
            self._state.input_queue.put(text)
            self._commit_now("[queued] turn 结束后处理\n")
        else:
            self._job_queue.put(text)

    def _route_slash_command(self, text: str) -> bool:
        """slash 分发：命中则就地提交结果，should_query 的展开 prompt 入队。返回是否已处理。"""
        result = dispatch_slash(text, self._make_slash_context())
        if not result.handled:
            return False
        if result.message:
            chunk = OutputChunk(
                str(result.message) + "\n", format="rich" if result.markup else "plain"
            )
            self._commit_chunks([chunk])
        if result.should_query:
            self._echo_user_prompt(result.prompt)
            self._job_queue.put(result.prompt)
        return True

    def _echo_user_prompt(self, text: str) -> None:
        self._commit_chunks([render_user_prompt(text)])

    def _commit_now(self, text: str) -> None:
        """（UI 线程）提交一行纯文本到 scrollback。"""
        self._commit_chunks([OutputChunk(text)])

    def _commit_chunks(self, chunks: list[OutputChunk]) -> None:
        """（UI 线程）提交 chunks：scrollback + session 转录边车。"""
        for chunk in chunks:
            if self._on_transcript is not None:
                self._on_transcript(chunk)
            _commit_chunk(chunk)

    # ---- 主入口 ---------------------------------------------------------------

    async def _run_async(self) -> None:
        app_session = get_app_session()
        output = app_session.output
        style = self._ui_style()
        self._loop = asyncio.get_running_loop()

        # resume 重放 / header：prompt 启动前直接写终端（无 app，直打）。
        for chunk in self._initial_chunks:
            _commit_chunk(chunk)

        session = PromptSession[str](
            message=self._render_message,
            bottom_toolbar=lambda: FormattedText(
                [("class:bottom-toolbar", bottom_toolbar(self._state))]
            ),
            completer=SlashCompleter(self._slash_registry),
            complete_while_typing=True,
            style=style,
            key_bindings=self._build_key_bindings(),
            # 显式绑定当前 AppSession 的输入输出：不传的话 PromptSession 会
            # 按默认控件造一套 Win32 控制台输入，pipe/测试替身全部失效。
            input=app_session.input,
            output=output,
        )
        self._session = session

        worker = threading.Thread(target=self._work_loop, daemon=True)
        worker.start()
        self._state.asker = self._terminal_asker

        consume_task = asyncio.create_task(self._consume_events())
        spinner_task = asyncio.create_task(self._animate_spinner())

        try:
            # patch_stdout：兜底接住任何绕过 wire 的 print（工具/hook 泄漏）。
            with patch_stdout(raw=False):
                while True:
                    try:
                        text = await session.prompt_async()
                        self._submit_text(text)
                    except (EOFError, KeyboardInterrupt):
                        break
        finally:
            consume_task.cancel()
            spinner_task.cancel()
            self._state.asker = None

    # ---- prompt message（Live 区）渲染 ----------------------------------------

    def _render_message(self) -> FormattedText:
        columns = 80
        app = get_app_or_none()
        if app is not None:
            columns = app.output.get_size().columns
        fragments: list[tuple[str, str]] = []
        # pending 提问：输入提示符挂在 Live 区顶部（问题正文也在 Live 区，
        # 见 _ask_in_app——不写 stdout，避免与 prompt 抢输入）。
        if self._pending_prompt is not None:
            request = self._pending_prompt["request"]
            fragments.append(("class:preview", prompt_for_request(request) + "\n"))
        fragments.extend(self._live.formatted(columns, time.monotonic()))
        return FormattedText(fragments)

    # ---- 按键 ------------------------------------------------------------------

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("escape")
        def _(event: Any) -> None:
            # Provider 会观察该信号并主动关闭正在读取的 HTTP stream。
            self._state.abort_event.set()

        @kb.add("s-tab")
        def _(event: Any) -> None:
            self._state.cycle_permission_mode()
            event.app.invalidate()

        @kb.add("c-c")
        def _(event: Any) -> None:
            if self._busy.is_set():
                # turn 进行中：Ctrl+C 等同 ESC（中止 turn），不退出。
                self._state.abort_event.set()
            else:
                event.app.exit(exception=EOFError)

        return kb

    def run(self) -> None:
        asyncio.run(self._run_async())
        self._job_queue.put("__EXIT__")


def run_interactive_shell(
    state: RuntimeState,
    run_turn: Callable[[str, Wire], None],
    make_slash_context: Callable[[], SlashContext],
    initial_output: str = "",
    initial_transcript: list[OutputChunk] | None = None,
    on_transcript: Callable[[OutputChunk], None] | None = None,
    drain_pending: Callable[[], list[str]] | None = None,
    slash_registry: SlashRegistry | None = None,
) -> None:
    InteractiveShell(
        state,
        run_turn,
        make_slash_context,
        initial_output,
        initial_transcript,
        on_transcript,
        drain_pending,
        slash_registry,
    ).run()
