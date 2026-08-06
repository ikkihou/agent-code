"""
File      :   interactive.py
Date      :   2026-07-22 15:28:36
Author    :   baoyihui
Contact   :   yihui.bao@apopgeei.com

主线程 = prompt_toolkit Application（输出面板 + 输入行 + 状态栏）；
worker 线程 = run_agent（消费 provider stream + 工具执行）。
"""

# here put the import lib
from __future__ import annotations

import asyncio
import queue
import threading
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
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea

from .output import OutputChunk, OutputWriter, render_console_chunk
from .runtime import PromptFallback, PromptRequest, RuntimeState
from .slash import SlashContext, SlashRegistry, default_slash_registry, dispatch_slash

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


def _chunk_fragments(chunk: OutputChunk) -> list[StyleAndText]:
    if chunk.format == "ansi":
        return [(style, text) for style, text, *_ in ANSI(chunk.text).__pt_formatted_text__()]
    return [("", chunk.text)]


def append_output_text(transcript: OutputTranscript, output: str | OutputChunk) -> str:
    """把 output 追加进 transcript，返回其纯文本（无 ANSI）部分。

    返回的 display_text 供调用方做展示同步（如持久化到 session transcript）。
    """
    chunk = output if isinstance(output, OutputChunk) else OutputChunk(output)
    if not chunk.text:
        return ""
    fragments = _chunk_fragments(chunk)
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
    # 白字深灰底反白高亮，source 头加粗；灰底用 256 色 #444444（47/ansigray 太浅，
    # 白字看不清）。背景只覆盖文本本身，整行铺满由 TranscriptLexer 在渲染时补位实现。
    return OutputChunk(
        # f"\n\x1b[1;97;48;5;238m[{source}]\x1b[0m\n\x1b[97;48;5;238m{body}\x1b[0m\n\n",
        f"\n\x1b[0m\n\x1b[97;48;5;238m{body}\x1b[0m\n\n",
        format="ansi",
    )


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


def run_interactive_shell(
    state: RuntimeState,
    run_turn: Callable[[str, OutputWriter], None],
    make_slash_context: Callable[[], SlashContext],
    initial_output: str = "",
    initial_transcript: list[OutputChunk] | None = None,
    on_transcript: Callable[[OutputChunk], None] | None = None,
    drain_pending: Callable[[], list[str]] | None = None,
    slash_registry: SlashRegistry | None = None,
) -> None:
    job_queue: queue.Queue[str] = queue.Queue()
    busy = threading.Event()

    ui_writer: OutputWriter | None = None

    def render_message(message: Any, *, styled: bool) -> OutputChunk:
        return render_console_chunk(message, styled=styled, width=120, markup=styled)

    def route_slash_command(text: str, render: Callable[[str | OutputChunk], None]) -> bool:
        """slash 分发：命中则就地渲染结果，should_query 的展开 prompt 入队。返回是否已处理。"""
        result = dispatch_slash(text, make_slash_context())
        if not result.handled:
            return False
        if result.message:
            render(render_message(result.message, styled=result.markup))
        if result.should_query:
            render(render_user_prompt(result.prompt, source="user /slash"))
            job_queue.put(result.prompt)
        return True

    def handle_cron_prompt(text: str, writer: OutputWriter) -> None:
        """worker 线程处理 cron 到点 prompt：slash 就地分发，普通 prompt 作为新一轮 turn 入队。"""
        text = text.strip()
        if not text or text == "/exit":
            return  # cron 到点 prompt 不触发应用退出
        if text.startswith("/"):
            route_slash_command(text, render=writer)
            return
        writer(render_user_prompt(text, source="cron"))
        job_queue.put(text)

    header_transcript = OutputTranscript(initial_output)
    header_control = FormattedTextControl(lambda: header_transcript.fragments)
    header_window = Window(
        content=header_control,
        wrap_lines=True,
        height=3,
    )
    initial_frame = Frame(header_window)

    output_transcript = OutputTranscript()
    output_buffer = Buffer(read_only=True)
    # resume 时预填充历史转录：启动第一帧就能看到上一会话的画面，而非等第一次 append。
    for chunk in initial_transcript or []:
        append_output_text(output_transcript, chunk)
    output_buffer.set_document(
        Document(output_transcript.text, cursor_position=len(output_transcript.text)),
        bypass_readonly=True,
    )
    output_lexer = TranscriptLexer(output_transcript)
    output_control = BufferControl(
        buffer=output_buffer,
        lexer=output_lexer,
        focusable=False,
        # 点击 output 区域时把焦点移过来，配合鼠标拖拽做选区，方便复制文本。
        focus_on_click=True,
    )
    output_window = Window(
        content=output_control,
        wrap_lines=True,
        right_margins=[ScrollbarMargin(display_arrows=True)],
    )
    # 渲染时 Lexer 需要按面板宽度给 user_prompt 行补位，回填窗口引用。
    output_lexer.output_window = output_window
    output_frame = Frame(output_window)

    input_area: TextArea | None = None

    def work_loop() -> None:
        while True:
            if drain_pending is None:
                text = job_queue.get()
            else:
                try:
                    text = job_queue.get(timeout=1.0)
                except queue.Empty:
                    text = None

            if text == "__EXIT__":
                break

            if text is not None:
                state.abort_event.clear()
                busy.set()

                try:
                    if ui_writer is None:
                        continue
                    run_turn(text, ui_writer)
                except Exception as e:  # provider/工具异常别让 worker 静默死掉
                    if ui_writer is not None:
                        ui_writer(f"[error] {e}\n")
                finally:
                    busy.clear()

            if ui_writer is None:
                continue

            while not state.input_queue.empty():
                job_queue.put(state.input_queue.get())

            # cron 到点 prompt：与用户输入一样走 slash 分发或作为新一轮 turn，
            # 但渲染走线程安全的 ui_writer。worker 在 get(timeout=1.0) 上空闲轮询，
            # 因此 cron 不依赖用户输入也能触发。
            if drain_pending is not None:
                for prompt in drain_pending():
                    handle_cron_prompt(prompt, ui_writer)

    async def _run() -> None:
        loop = asyncio.get_running_loop()

        def append_output(output: str | OutputChunk) -> None:
            chunk = output if isinstance(output, OutputChunk) else OutputChunk(output)
            if not chunk.text:
                return
            append_output_text(output_transcript, chunk)
            # 唯一显示漏斗：把渲染出的 chunk 同步给 session 转录边车，resume 时重放。
            if on_transcript is not None:
                on_transcript(chunk)
            doc = output_buffer.document
            # 自动跟随：视口当前停在底部（以 render_info 的 bottom_visible 为准）
            # 才把光标推到文本末尾；用户上滚阅读历史时 bottom_visible 为 False，
            # 光标保持不动、视口不被新输出拽走。滚回底部后下一次 append 自动恢复跟随。
            at_bottom = output_at_bottom(output_window)
            new_text = output_transcript.text
            new_cursor = output_follow_position(at_bottom, doc.cursor_position, len(new_text))
            output_buffer.set_document(
                Document(new_text, cursor_position=new_cursor),
                bypass_readonly=True,
            )
            app.invalidate()

        def ui_write(output: str | OutputChunk) -> None:
            loop.call_soon_threadsafe(append_output, output)

        nonlocal ui_writer
        ui_writer = ui_write
        input_prompt = ["> "]
        pending_prompt: PendingPrompt | None = None

        def submit_text(text: str) -> None:
            nonlocal pending_prompt
            text = text.strip()
            if pending_prompt is not None:
                request = pending_prompt["request"]
                future = pending_prompt["future"]
                request_type = request.get("type")

                if request_type == "choice":
                    labels = request.get("labels") or []
                    idx = parse_choice_index(
                        text,
                        len(labels),
                        int(request.get("default", 0)),
                    )
                    if idx is None:
                        append_output("Invalid choice. Enter a listed number.\n")
                        return
                    result = str(idx)
                    if not future.done():
                        future.set_result(result)
                    pending_prompt = None
                    input_prompt[0] = "> "
                    if idx == 0:
                        append_output("Choice: skip\n")
                    else:
                        append_output(f"Choice: {labels[idx - 1]}\n")
                    app.invalidate()
                    return

                answer = parse_confirm_answer(
                    text,
                    bool(request.get("default", False)),
                )
                if answer is None:
                    append_output("Invalid answer. Enter y or n.\n")
                    return
                if not future.done():
                    future.set_result(answer)
                pending_prompt = None
                input_prompt[0] = "> "
                append_output(f"Answer: {'yes' if answer else 'no'}\n")
                app.invalidate()
                return

            if not text:
                return

            if text == "/exit":
                app.exit()
                return

            if text.startswith("/"):
                route_slash_command(text, render=append_output)
                return

            append_output(render_user_prompt(text))
            if busy.is_set():
                state.input_queue.put(text)
                append_output("[queued] turn 结束后处理\n")
            else:
                job_queue.put(text)

        def accept_input(buffer: Any) -> bool:
            text = buffer.text
            buffer.set_document(Document("", cursor_position=0))
            submit_text(text)
            return True

        nonlocal input_area
        input_area = TextArea(
            multiline=False,
            prompt=lambda: input_prompt[0],
            wrap_lines=False,
            completer=SlashCompleter(slash_registry),
            complete_while_typing=True,
            accept_handler=accept_input,
            style="class:input-area",
        )
        input_frame = Frame(
            input_area,
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
                        FormattedTextControl(lambda: bottom_toolbar(state)),
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
        app = Application(
            layout=Layout(root, focused_element=input_area),
            key_bindings=build_key_bindings(
                state,
                output_window,
                output_buffer,
                output_transcript=output_transcript,
                input_area=input_area,
                on_copy=lambda n: append_output(f"[copied {n} chars to clipboard]\n"),
            ),
            cursor=SimpleCursorShapeConfig(CursorShape.BLINKING_BLOCK),
            full_screen=True,
            # 开启鼠标支持，滚轮即可滚动输出面板（上滚暂停自动跟随，滚回底部恢复）。
            mouse_support=True,
            # Ctrl+C 选中复制要写进系统剪贴板（macOS pbcopy），而不是 prompt_toolkit
            # 默认的应用内内存剪贴板——否则外部粘贴是空的。
            clipboard=_make_system_clipboard(),
        )

        async def ask_in_app(request: PromptRequest) -> Any:
            nonlocal pending_prompt
            future: asyncio.Future[Any] = loop.create_future()
            pending_prompt = {"request": request, "future": future}
            append_output(render_prompt_request(request))
            input_prompt[0] = prompt_for_request(request)
            if input_area is not None:
                input_area.buffer.set_document(Document("", cursor_position=0))
                app.layout.focus(input_area)
            app.invalidate()
            return await future

        def terminal_asker(func: PromptFallback | PromptRequest) -> Any:
            if isinstance(func, dict):
                return asyncio.run_coroutine_threadsafe(ask_in_app(func), loop).result()

            async def _run_in_terminal() -> Any:
                return await run_in_terminal(func)

            return asyncio.run_coroutine_threadsafe(_run_in_terminal(), loop).result()

        worker = threading.Thread(target=work_loop, daemon=True)
        worker.start()

        state.asker = terminal_asker
        try:
            await app.run_async()
        finally:
            ui_writer = None
            state.asker = None

    asyncio.run(_run())
    job_queue.put("__EXIT__")


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

        if output_transcript is not None:

            @kb.add("end", filter=output_focused)
            def _(event: Any) -> None:
                # 跳到输出末尾并恢复自动跟随。
                output_buffer.cursor_position = len(output_transcript.text)
                if input_area is not None:
                    event.app.layout.focus(input_area)
                event.app.invalidate()

            @kb.add("home", filter=output_focused)
            def _(event: Any) -> None:
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
