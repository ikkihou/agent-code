"""InteractiveShell：scrollback+Live 式 REPL 主体。

线程模型与终端所有权模型见包文档（ui/__init__.py）。本模块只保留有状态编排：worker 线程
（turn 执行 / cron 分发）、UI 协程（事件消费 / spinner / 提问）、
输入提交与按键绑定。纯渲染与解析逻辑在兄弟模块中。
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import traceback
from typing import Any, Callable

from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.application.current import get_app_or_none, get_app_session
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession

from ..output import OutputChunk
from ..runtime import PromptFallback, PromptRequest, RuntimeState
from ..slash import SlashContext, SlashRegistry, dispatch_slash
from ..wire import (
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
from .completer import SlashCompleter
from .events import event_to_chunks, render_user_prompt
from .layout import _install_bottom_anchored_layout, bottom_toolbar, build_style
from .live import LiveArea, _preview_lines
from .prompts import (
    parse_choice_index,
    parse_confirm_answer,
    prompt_for_request,
    render_prompt_request,
)
from .scrollback import _commit_chunk

PendingPrompt = dict[str, Any]


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
        style = build_style()
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
            # 输入区四周画框（┌─┐│└─┘），与 scrollback 视觉分区。
            show_frame=True,
            # 默认 8：为补全菜单预留 8 行——单行输入框下撑出大片空白，
            # 加 frame 后尤其难看。补全菜单出现时布局会临时加高，无需预占。
            reserve_space_for_menu=0,
            # 显式绑定当前 AppSession 的输入输出：不传的话 PromptSession 会
            # 按默认控件造一套 Win32 控制台输入，pipe/测试替身全部失效。
            input=app_session.input,
            output=output,
        )
        self._session = session
        _install_bottom_anchored_layout(session)

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
