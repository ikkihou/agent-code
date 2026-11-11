"""
File      :   interactive.py
Date      :   2026-07-22 15:28:36
Author    :   baoyihui
Contact   :   yihui.bao@apopgeei.com

主线程 = prompt_toolkit Application（输出面板 + 输入行 + 状态栏）；
worker 线程 = run_agent（消费 provider stream + 工具执行）。

REPL 是 wire 的纯事件消费者：agent loop / 工具层只发结构化事件
（TextDelta / Line / PromptEcho / ToolCallStarted / DiffPreview /
CommandPreview），本模块把事件批翻译成 OutputChunk 喂给输出面板——
行缓冲、着色、diff 渲染都是消费端的渲染关注点。
"""

# here put the import lib
from __future__ import annotations

import asyncio
import queue
import threading
import traceback
from typing import Any, Callable, Hashable, cast

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import InMemoryClipboard
from prompt_toolkit.clipboard.base import Clipboard, ClipboardData
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.cursor_shapes import CursorShape, SimpleCursorShapeConfig
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea

from .output import OutputChunk, chunk_fragments
from .prompt_ui import render_diff
from .runtime import PromptFallback, PromptRequest, RuntimeState
from .slash import SlashContext, SlashRegistry, default_slash_registry, dispatch_slash
from .wire import (
    CommandPreview,
    DiffPreview,
    Line,
    PromptEcho,
    StreamLineBuffer,
    TextDelta,
    ToolCallStarted,
    Wire,
    WireEvent,
)

PendingPrompt = dict[str, Any]
StyleAndText = tuple[str, str]


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


def _append_fragments(target: list[StyleAndText], fragments: list[StyleAndText]) -> None:
    for style, text in fragments:
        if not text:
            continue
        if target and target[-1][0] == style:
            last_style, last_text = target[-1]
            target[-1] = (last_style, last_text + text)
        else:
            target.append((style, text))


def append_output_text(transcript: OutputTranscript, output: str | OutputChunk) -> str:
    """把 output 追加进 transcript，返回其纯文本部分。

    返回的 display_text 供调用方做展示同步（如持久化到 session transcript）。
    片段转换统一走 output.chunk_fragments——plain/fragments 直用，rich/ansi
    在那边归一化，这里不再持有 ANSI 解析逻辑。
    """
    chunk = output if isinstance(output, OutputChunk) else OutputChunk(output)
    if not chunk.text:
        return ""
    fragments = chunk_fragments(chunk)
    display_text = "".join(text for _, text in fragments)
    transcript.text += display_text
    transcript.line_count += display_text.count("\n")
    _append_fragments(transcript.fragments, fragments)
    return display_text


class OutputTranscript:
    def __init__(self, initial_text: str = "") -> None:
        self.fragments: list[StyleAndText] = []
        self.text = ""
        self.line_count = 0
        append_output_text(self, initial_text)


def output_follow_position(at_bottom: bool, cursor_position: int, new_text_len: int) -> int:
    """计算追加输出后 output buffer 应放置的光标位置。

    视口在底部（at_bottom=True）时跟随到文本末尾；否则保持原光标不动，
    避免把用户正在阅读的历史视口拽走。超界时钳制到文本末尾。
    """
    if at_bottom:
        return new_text_len
    return min(cursor_position, new_text_len)


def output_at_bottom(window: "Window | None") -> bool:
    """视口当前是否停在输出底部——决定新输出到来时要不要自动跟随。

    用 Window.render_info（上一帧实际渲染结果）判断，而不是用 buffer 的
    逻辑行号：鼠标滚轮 / 快捷键滚动后，只有真正滚回底部才算“在底部”，
    因此用户上滚后视口保持冻结，滚回底部后自动恢复跟随。
    """
    if window is None or window.render_info is None:
        return True
    ri = window.render_info
    if ri.window_height <= 0:
        return True
    return bool(ri.bottom_visible)


def split_fragments_by_lines(
    fragments: list[StyleAndText],
) -> list[list[StyleAndText]]:
    """把扁平片段列表按 ``\\n`` 拆成逐行片段，结构与 ``Document(text).lines`` 对齐。"""
    lines: list[list[StyleAndText]] = [[]]
    for style, text in fragments:
        start = 0
        while True:
            newline = text.find("\n", start)
            if newline == -1:
                if text[start:]:
                    lines[-1].append((style, text[start:]))
                break
            if text[start:newline]:
                lines[-1].append((style, text[start:newline]))
            lines.append([])
            start = newline + 1
    return lines


class TranscriptLexer(Lexer):
    """把 OutputTranscript 的 ANSI 片段按行喂给 BufferControl，保留原有样式。

    额外职责：prompt_toolkit 只给片段实际覆盖的字符上背景色，所以对带灰色背景的
    user_prompt 行在渲染时补尾随空格，把背景铺满整行（不污染 transcript.text）。
    """

    # render_user_prompt 用的背景样式 token（256 色灰 #444444），命中该背景的行才补位。
    _PROMPT_BG = "bg:#444444"

    def __init__(self, transcript: OutputTranscript, output_window: Window | None = None) -> None:
        self._transcript = transcript
        self.output_window = output_window

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        lines = split_fragments_by_lines(self._transcript.fragments)

        def get_line(lineno: int) -> StyleAndTextTuples:
            if not (0 <= lineno < len(lines)):
                return []
            fragments = cast(StyleAndTextTuples, lines[lineno])
            width = self._pane_width()
            if width <= 0:
                return fragments
            if not any(self._PROMPT_BG in style for style, *_ in fragments):
                return fragments
            used = sum(get_cwidth(text) for _, text, *_ in fragments)
            pad = width - 1 - used  # 留 1 格给 BufferControl 自动追加的尾随空格
            if pad <= 0:
                return fragments
            return [*fragments, (self._PROMPT_BG, " " * pad)]

        return get_line

    def _pane_width(self) -> int:
        """输出面板内容宽度：取输出窗口上一帧的精确 window_width。

        渲染信息尚不存在（首帧）时返回 0 即不补位：真实 app 首帧 transcript 为空，
        首个 user_prompt 到用户提交后才出现，那时 render_info 一定已可用。
        不退回终端列数——依赖 get_app() 在测试/未运行时会拿到 dummy 的 80 列，危险。
        """
        window = self.output_window
        if window is None or window.render_info is None:
            return 0
        return max(0, window.render_info.window_width)

    def invalidation_hash(self) -> Hashable:
        # 内容变化由 document.text 的 cache key 覆盖，这里只需返回稳定值。
        return "transcript-lexer"


def render_user_prompt(text: str, *, source: str = "user") -> OutputChunk:
    normalized = text.rstrip()
    if not normalized.strip():
        return OutputChunk("")
    body = "\n".join(f"> {line}" for line in normalized.splitlines())
    # 白字深灰底反白高亮（source 头当前不再渲染，参数仅作兼容保留）。
    # 样式用 pt 内联样式串直接构造，不再经过 ANSI 往返。
    # 背景只覆盖文本本身，整行铺满由 TranscriptLexer 在渲染时补位实现。
    style = "ansiwhite bg:#444444"
    fragments: tuple[StyleAndText, ...] = (
        ("", "\n\n"),
        (style, body),
        ("", "\n\n"),
    )
    return OutputChunk("\n\n" + body + "\n\n", fragments=fragments)


## wire 事件 → 输出面板 chunk 的翻译（REPL 渲染策略）
def event_to_chunks(event: WireEvent) -> list[OutputChunk]:
    """把一个非流式 wire 事件翻译成 OutputChunk 列表。

    TextDelta 不在此列——流式原文由 StreamLineBuffer 攒行后单独成 chunk
    （见 InteractiveShell._drain_wire）。着色与行格式全部在这里决定，
    agent / 工具层只提供事实。
    """
    if isinstance(event, Line):
        return [OutputChunk(event.text + "\n", format="rich" if event.markup else "plain")]
    if isinstance(event, PromptEcho):
        return [render_user_prompt(event.text)]
    if isinstance(event, ToolCallStarted):
        return [OutputChunk(f"tool_call: {event.name} {event.args_preview}\n")]
    if isinstance(event, DiffPreview):
        return [
            OutputChunk(f"\nDiff for {event.path}:", format="rich"),
            OutputChunk(render_diff(event.old, event.new, event.path) + "\n", format="rich"),
        ]
    if isinstance(event, CommandPreview):
        return [OutputChunk(f"\nCommand: {event.command}\n", format="rich")]
    return []


class CronPromptWire:
    """cron prompt 的即发即渲 wire 替身：send 立刻经线程安全路径渲染。

    cron prompt 量小且低频（每条一个 echo + 可能几行结果），不值得开
    完整的 notify/drain 管线——直接逐事件线程安全投递。
    """

    def __init__(self, shell: "InteractiveShell") -> None:
        self._shell = shell

    def send(self, event: WireEvent) -> None:
        self._shell._cron_prompt_send(event)


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


class InteractiveShell:
    """REPL 交互壳：prompt_toolkit Application 构建 + 事件回调 + worker 线程管线。

    原 run_interactive_shell 是一个 ~300 行、11 个嵌套闭包（最深 3 层）的函数；
    拆成类后闭包变方法、闭包捕获的局部状态变实例属性，嵌套深度降到 1。
    事件回调（submit_text / accept_input 等）用到的可变状态全部显式挂在实例上，
    不再靠词法闭包隐式共享，也让它可以被单元测试单独驱动。
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
        self._initial_output = initial_output
        self._initial_transcript = initial_transcript
        self._on_transcript = on_transcript
        self._drain_pending = drain_pending
        self._slash_registry = slash_registry

        self._job_queue: queue.Queue[str] = queue.Queue()
        self._busy = threading.Event()
        self._turn_wire: Wire | None = None  # 当前 turn 的 wire，worker 线程期间持有
        self._stream_lines = StreamLineBuffer(self._append_output_text_only)
        self._drain_scheduled = False  # 已有一次 drain 排在 UI 队列上（合并 notify 用）
        self._input_prompt: list[str] = ["> "]
        self._pending_prompt: PendingPrompt | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app: Application[Any] | None = None
        self._input_area: TextArea | None = None
        # 输出面板构建期填充（见 _build_layout）。
        self._output_transcript = OutputTranscript()
        self._output_buffer = Buffer(read_only=True)
        self._output_window: Window | None = None

    def _render_message(self, message: Any, *, styled: bool) -> OutputChunk:
        """slash 结果消息 → chunk：styled 时把 Rich markup 转成即用片段。"""
        text = str(message)
        if not styled:
            return OutputChunk(text)
        return OutputChunk(text, format="rich")

    def _route_slash_command(self, text: str, render: Callable[[str | OutputChunk], None]) -> bool:
        """slash 分发：命中则就地渲染结果，should_query 的展开 prompt 入队。返回是否已处理。"""
        result = dispatch_slash(text, self._make_slash_context())
        if not result.handled:
            return False
        if result.message:
            render(self._render_message(result.message, styled=result.markup))
        if result.should_query:
            render(render_user_prompt(result.prompt, source="user /slash"))
            self._job_queue.put(result.prompt)
        return True

    def _handle_cron_prompt(self, text: str, wire: Wire) -> None:
        """worker 线程处理 cron 到点 prompt：slash 就地分发，普通 prompt 作为新一轮 turn 入队。"""
        text = text.strip()
        if not text or text == "/exit":
            return  # cron 到点 prompt 不触发应用退出
        if text.startswith("/"):
            self._route_slash_command(text, render=lambda c: wire.send(PromptEcho(str(c))))
            return
        wire.send(PromptEcho(text))
        self._job_queue.put(text)

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
                    # 每个 turn 一个专属 wire：agent 只发事件；流式期间 notify
                    # 把 drain 调度到 UI 线程，turn 结束再兜底排一次（见
                    # _schedule_turn_finish），保证事件与半行都落面板。
                    wire = Wire()
                    self._turn_wire = wire
                    wire.notify = self._schedule_wire_drain
                    self._run_turn(text, wire)
                    self._schedule_turn_finish()
                except Exception as e:  # provider/工具异常别让 worker 静默死掉
                    # 带上 traceback：只有 str(e) 时无法定位是哪条路径抛的
                    # （编码类错误尤其明显——报错文案里看不出调用栈）。
                    detail = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                    self._append_output_safe(f"[error] {e}\n{detail}\n")
                finally:
                    self._turn_wire = None
                    self._busy.clear()

            while not self._state.input_queue.empty():
                self._job_queue.put(self._state.input_queue.get())

            # cron 到点 prompt：与用户输入一样走 slash 分发或作为新一轮 turn。
            # worker 在 get(timeout=1.0) 上空闲轮询，因此 cron 不依赖用户输入
            # 也能触发。
            if self._drain_pending is not None:
                for prompt in self._drain_pending():
                    self._handle_cron_prompt(prompt, CronPromptWire(self))

    # ---- wire 消费（worker 线程产出，UI 线程渲染）--------------------------

    def _drain_wire(self) -> None:
        """UI 线程回调：取走当前 wire 的全部事件并渲染进输出面板。"""
        wire = self._turn_wire
        if wire is None:
            return
        batch = wire.drain()
        if not batch:
            return
        for event in batch:
            if isinstance(event, TextDelta):
                # 流式文本攒完整行才落面板（pt 不能被覆写半行）；
                # pending 半行保留到下一批，绝不 finish 截断。
                self._stream_lines.feed(event.text)
            else:
                for chunk in event_to_chunks(event):
                    self._append_output(chunk)
        self._app.invalidate()

    def _schedule_wire_drain(self) -> None:
        """worker 线程侧的 notify：把 drain 调度到 UI 事件循环。

        call_soon_threadsafe 自带"同一迭代内多次调度只跑一次回调队列"的
        开销下限，流式 token 密集时每 token 一次调用仍偏贵——这里用
        pending 标志把同一轮 UI 迭代内的多次 notify 合并成一次 drain。
        """
        if self._drain_scheduled:
            return
        self._drain_scheduled = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._drain_scheduled_cb)

    def _drain_scheduled_cb(self) -> None:
        """UI 事件循环回调：清掉 pending 标志后执行真正的 drain。"""
        self._drain_scheduled = False
        self._drain_wire()

    def _schedule_turn_finish(self) -> None:
        """turn 结束的兜底渲染：清空剩余事件 + flush 攒着的半行（UI 线程执行）。"""
        self._drain_scheduled = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._turn_finish_cb)

    def _turn_finish_cb(self) -> None:
        self._drain_scheduled = False
        self._drain_wire()
        self._stream_lines.finish()

    def _append_output_text_only(self, line: str) -> None:
        """流式文本行落面板（StreamLineBuffer 回调）。

        在 UI 线程被调用（_drain_wire 的 TextDelta 攒行）。"""
        self._append_output(OutputChunk(line + "\n"))

    def _append_output_safe(self, text: str) -> None:
        """worker 线程兜底写面板（异常路径）：经 call_soon_threadsafe 跳 UI 线程。"""
        self._append_output_safe_chunk(OutputChunk(text))

    def _append_output_safe_chunk(self, chunk: OutputChunk) -> None:
        """worker 线程写一个 chunk 进面板：经 call_soon_threadsafe 跳 UI 线程。"""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._append_output, chunk)

    def _cron_prompt_send(self, event: WireEvent) -> None:
        """cron prompt 的即发即渲通道：PromptEcho 之外的事件直接翻译渲染。"""
        if isinstance(event, PromptEcho):
            self._append_output_safe(render_user_prompt(event.text))
        else:
            for chunk in event_to_chunks(event):
                self._append_output_safe_chunk(chunk)

    def _append_output(self, output: str | OutputChunk) -> None:
        chunk = output if isinstance(output, OutputChunk) else OutputChunk(output)
        if not chunk.text:
            return
        append_output_text(self._output_transcript, chunk)
        # 唯一显示漏斗：把渲染出的 chunk 同步给 session 转录边车，resume 时重放。
        if self._on_transcript is not None:
            self._on_transcript(chunk)
        doc = self._output_buffer.document
        # 自动跟随：视口当前停在底部（以 render_info 的 bottom_visible 为准）
        # 才把光标推到文本末尾；用户上滚阅读历史时 bottom_visible 为 False，
        # 光标保持不动、视口不被新输出拽走。滚回底部后下一次 append 自动恢复跟随。
        at_bottom = output_at_bottom(self._output_window)
        new_text = self._output_transcript.text
        new_cursor = output_follow_position(at_bottom, doc.cursor_position, len(new_text))
        self._output_buffer.set_document(
            Document(new_text, cursor_position=new_cursor),
            bypass_readonly=True,
        )
        self._app.invalidate()

    def _submit_text(self, text: str) -> None:
        """统一输入入口：有 pending 提问时先喂给提问状态机，否则作为新输入提交。"""
        text = text.strip()
        if self._pending_prompt is not None:
            request = self._pending_prompt["request"]
            future = self._pending_prompt["future"]
            request_type = request.get("type")

            if request_type == "choice":
                labels = request.get("labels") or []
                idx = parse_choice_index(
                    text,
                    len(labels),
                    int(request.get("default", 0)),
                )
                if idx is None:
                    self._append_output("Invalid choice. Enter a listed number.\n")
                    return
                result = str(idx)
                if not future.done():
                    future.set_result(result)
                self._pending_prompt = None
                self._input_prompt[0] = "> "
                if idx == 0:
                    self._append_output("Choice: skip\n")
                else:
                    self._append_output(f"Choice: {labels[idx - 1]}\n")
                self._app.invalidate()
                return

            answer = parse_confirm_answer(
                text,
                bool(request.get("default", False)),
            )
            if answer is None:
                self._append_output("Invalid answer. Enter y or n.\n")
                return
            if not future.done():
                future.set_result(answer)
            self._pending_prompt = None
            self._input_prompt[0] = "> "
            self._append_output(f"Answer: {'yes' if answer else 'no'}\n")
            self._app.invalidate()
            return

        if not text:
            return

        if text == "/exit":
            self._app.exit()
            return

        if text.startswith("/"):
            self._route_slash_command(text, render=self._append_output)
            return

        self._append_output(render_user_prompt(text))
        if self._busy.is_set():
            self._state.input_queue.put(text)
            self._append_output("[queued] turn 结束后处理\n")
        else:
            self._job_queue.put(text)

    def _accept_input(self, buffer: Any) -> bool:
        text = buffer.text
        buffer.set_document(Document("", cursor_position=0))
        self._submit_text(text)
        return True

    def _build_layout(self) -> Application[Any]:
        """构建 prompt_toolkit 视图：header + 输出面板 + 输入行 + 状态栏 + 浮层。"""
        header_transcript = OutputTranscript(self._initial_output)
        header_control = FormattedTextControl(lambda: header_transcript.fragments)
        header_window = Window(
            content=header_control,
            wrap_lines=True,
            height=3,
        )
        initial_frame = Frame(header_window)

        # resume 时预填充历史转录：启动第一帧就能看到上一会话的画面，而非等第一次 append。
        for chunk in self._initial_transcript or []:
            append_output_text(self._output_transcript, chunk)
        self._output_buffer.set_document(
            Document(
                self._output_transcript.text,
                cursor_position=len(self._output_transcript.text),
            ),
            bypass_readonly=True,
        )
        output_lexer = TranscriptLexer(self._output_transcript)
        output_control = BufferControl(
            buffer=self._output_buffer,
            lexer=output_lexer,
            focusable=False,
            # 点击 output 区域时把焦点移过来，配合鼠标拖拽做选区，方便复制文本。
            focus_on_click=True,
        )
        self._output_window = Window(
            content=output_control,
            wrap_lines=True,
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )
        # 渲染时 Lexer 需要按面板宽度给 user_prompt 行补位，回填窗口引用。
        output_lexer.output_window = self._output_window
        output_frame = Frame(self._output_window)

        self._input_area = TextArea(
            multiline=False,
            prompt=lambda: self._input_prompt[0],
            wrap_lines=False,
            completer=SlashCompleter(self._slash_registry),
            complete_while_typing=True,
            accept_handler=self._accept_input,
            style="class:input-area",
        )
        input_frame = Frame(
            self._input_area,
            title=" INPUT ",
            style="class:input-frame",
        )
        root = FloatContainer(
            content=HSplit(
                [
                    initial_frame,
                    output_frame,
                    input_frame,
                    Window(
                        FormattedTextControl(lambda: bottom_toolbar(self._state)),
                        height=1,
                        style="reverse",
                    ),
                ]
            ),
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                ),
            ],
        )
        return Application(
            layout=Layout(root, focused_element=self._input_area),
            key_bindings=build_key_bindings(
                self._state,
                self._output_window,
                self._output_buffer,
                output_transcript=self._output_transcript,
                input_area=self._input_area,
                on_copy=lambda n: self._append_output(f"[copied {n} chars to clipboard]\n"),
            ),
            cursor=SimpleCursorShapeConfig(CursorShape.BLINKING_BLOCK),
            full_screen=True,
            # 开启鼠标支持，滚轮即可滚动输出面板（上滚暂停自动跟随，滚回底部恢复）。
            mouse_support=True,
            # Ctrl+C 选中复制要写进系统剪贴板（macOS pbcopy），而不是 prompt_toolkit
            # 默认的应用内内存剪贴板——否则外部粘贴是空的。
            clipboard=_make_system_clipboard(),
        )

    async def _ask_in_app(self, request: PromptRequest) -> Any:
        """在 app 事件循环内发起提问，把结果通过 future 交还给调用线程。"""
        future: asyncio.Future[Any] = self._loop.create_future()
        self._pending_prompt = {"request": request, "future": future}
        self._append_output(render_prompt_request(request))
        self._input_prompt[0] = prompt_for_request(request)
        if self._input_area is not None:
            self._input_area.buffer.set_document(Document("", cursor_position=0))
            self._app.layout.focus(self._input_area)
        self._app.invalidate()
        return await future

    def _terminal_asker(self, func: PromptFallback | PromptRequest) -> Any:
        """worker 线程里的提问入口：结构化请求走 app 内提问，否则退回 run_in_terminal。"""
        if isinstance(func, dict):
            return asyncio.run_coroutine_threadsafe(self._ask_in_app(func), self._loop).result()
        # run_in_terminal 本身返回 coroutine，无需再包一层 async 函数。
        return asyncio.run_coroutine_threadsafe(run_in_terminal(func), self._loop).result()

    async def _run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._app = self._build_layout()

        worker = threading.Thread(target=self._work_loop, daemon=True)
        worker.start()

        self._state.asker = self._terminal_asker
        try:
            await self._app.run_async()
        finally:
            self._state.asker = None

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


## ----------- clipboard ---------------
def _make_system_clipboard() -> Clipboard:
    """优先使用系统剪贴板（macOS pbcopy / Linux xclip / Windows ctypes）。

    通过 prompt_toolkit 的 ``PyperclipClipboard``（底层 pyperclip）实现，让 Ctrl+C
    真的写进系统剪贴板、可被外部粘贴。pyperclip 未安装（如还没 ``uv sync``）时
    退回应用内 ``InMemoryClipboard``，行为退回旧版、但不至于崩。
    """
    try:
        from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard
    except Exception:
        return InMemoryClipboard()
    return PyperclipClipboard()


def _copy_output(
    event: Any,
    output_buffer: Buffer | None,
    output_transcript: OutputTranscript | None,
    on_copy: Callable[[int], None] | None = None,
) -> bool:
    """把 output 的选区（若有）或全部文本复制到剪贴板。返回是否复制了内容。"""
    if output_buffer is None or output_transcript is None:
        return False
    if output_buffer.selection_state is not None:
        data = output_buffer.copy_selection()
    else:
        data = ClipboardData(output_transcript.text)
    if not data.text:
        return False
    try:
        event.app.clipboard.set_data(data)
    except Exception:
        # 系统剪贴板进程不可用（无图形环境 / 缺 pbcopy 等）时退回内存剪贴板，
        # 至少保证应用内粘贴可用，不把按键处理炸掉。
        fallback = InMemoryClipboard()
        fallback.set_data(data)
        event.app.clipboard = fallback
    if on_copy is not None:
        on_copy(len(data.text))
    return True


## ----------- keybindings & statusline ---------------
def build_key_bindings(
    state: RuntimeState,
    output_window: Window | None = None,
    output_buffer: Buffer | None = None,
    output_transcript: OutputTranscript | None = None,
    input_area: TextArea | None = None,
    on_copy: Callable[[int], None] | None = None,
) -> KeyBindings:
    """按键绑定：ESC 中止 / s-tab 切权限 / 滚动并复制输出文本。"""
    kb = KeyBindings()

    output_focused = Condition(
        lambda: output_buffer is not None and get_app().layout.current_buffer is output_buffer
    )

    @kb.add("escape")
    def _(event: Any) -> None:
        # 在 output 区做选区/阅读时，esc 只是把焦点还给输入框，不中止 agent。
        if output_focused() and input_area is not None:
            event.app.layout.focus(input_area)
            event.app.invalidate()
        else:
            # Provider 会观察该信号并主动关闭正在读取的 HTTP stream。
            state.abort_event.set()

    @kb.add("s-tab")
    def _(event: Any) -> None:
        state.cycle_permission_mode()
        event.app.invalidate()

    @kb.add("c-c")
    @kb.add("c-d")
    def _(event: Any) -> None:
        if output_focused() and input_area is not None:
            # 在 output 区：把选区（没有选区就复制全部输出）复制到剪贴板，
            # 然后把焦点还给输入框（不退出）。终端习惯：Ctrl+C 复制。
            _copy_output(event, output_buffer, output_transcript, on_copy)
            event.app.layout.focus(input_area)
            event.app.invalidate()
        else:
            event.app.exit()

    if output_buffer is not None:

        def scroll_output_lines(delta: int) -> None:
            # 移动 Buffer 光标；窗口的 _scroll 会保持光标可见，从而滚动视口。
            document = output_buffer.document
            if delta < 0:
                output_buffer.cursor_position += document.get_cursor_up_position(count=-delta)
            else:
                output_buffer.cursor_position += document.get_cursor_down_position(count=delta)

        def output_page_lines() -> int:
            info = output_window.render_info if output_window is not None else None
            height = (info.window_height - 1) if info is not None else 0
            return max(1, height)

        @kb.add("c-up")
        def _(event: Any) -> None:
            scroll_output_lines(-1)
            event.app.invalidate()

        @kb.add("c-down")
        def _(event: Any) -> None:
            scroll_output_lines(1)
            event.app.invalidate()

        @kb.add("pageup")
        def _(event: Any) -> None:
            scroll_output_lines(-output_page_lines())
            event.app.invalidate()

        @kb.add("pagedown")
        def _(event: Any) -> None:
            scroll_output_lines(output_page_lines())
            event.app.invalidate()

        @kb.add("end", filter=output_focused)
        def _(event: Any) -> None:
            if output_transcript is not None:
                # 跳到输出末尾并恢复自动跟随。
                output_buffer.cursor_position = len(output_transcript.text)
                if input_area is not None:
                    event.app.layout.focus(input_area)
                event.app.invalidate()

        @kb.add("home", filter=output_focused)
        def _(event: Any) -> None:
            if output_transcript is not None:
                output_buffer.cursor_position = 0
                if input_area is not None:
                    event.app.layout.focus(input_area)
                event.app.invalidate()

    return kb


def bottom_toolbar(state: RuntimeState) -> str:
    """底部状态栏——当前模式 + 模型。"""
    mode = {"default": "default", "acceptEdits": "accept edits", "plan": "plan"}.get(
        state.permission_mode, state.permission_mode
    )
    active = next((t.active_form for t in state.todo_store if t.status == "in_progress"), "")
    todo = f" · {active}" if active else ""
    return f" {mode} · {state.model}{todo} "
