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
from typing import Any, Callable, Hashable

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard.base import ClipboardData
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.cursor_shapes import CursorShape, SimpleCursorShapeConfig
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from . import prompt_ui
from .output import OutputChunk, OutputWriter, render_console_chunk
from .runtime import RuntimeState
from .slash import SlashContext, dispatch_slash, slash_commands

PromptRequest = dict[str, Any]
PendingPrompt = dict[str, Any]
StyleAndText = tuple[str, str]


class SlashCompleter(Completer):
    def get_completions(self, document: Document, complete_event: Any) -> Any:
        text_before_cursor = document.text_before_cursor
        if not text_before_cursor.startswith("/") or any(
            char.isspace() for char in text_before_cursor
        ):
            return

        prefix = text_before_cursor[1:]
        for command in slash_commands():
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
        return [(style, text) for style, text in ANSI(chunk.text).__pt_formatted_text__()]
    return [("", chunk.text)]


def append_output_text(transcript: OutputTranscript, output: str | OutputChunk) -> None:
    chunk = output if isinstance(output, OutputChunk) else OutputChunk(output)
    if not chunk.text:
        return
    fragments = _chunk_fragments(chunk)
    display_text = "".join(text for _, text in fragments)
    transcript.text += display_text
    transcript.line_count += display_text.count("\n")
    _append_fragments(transcript.fragments, fragments)


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
    """把 OutputTranscript 的 ANSI 片段按行喂给 BufferControl，保留原有样式。"""

    def __init__(self, transcript: OutputTranscript) -> None:
        self._transcript = transcript

    def lex_document(self, document: Document) -> Callable[[int], list[StyleAndText]]:
        lines = split_fragments_by_lines(self._transcript.fragments)
        return lambda lineno: lines[lineno] if 0 <= lineno < len(lines) else []

    def invalidation_hash(self) -> Hashable:
        # 内容变化由 document.text 的 cache key 覆盖，这里只需返回稳定值。
        return "transcript-lexer"


def render_user_prompt(text: str, *, source: str = "user") -> str:
    normalized = text.rstrip()
    if not normalized.strip():
        return ""
    body = "\n".join(f"> {line}" for line in normalized.splitlines())
    return f"\n[{source}]\n{body}\n\n"


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
) -> None:
    job_queue: queue.Queue[str] = queue.Queue()
    busy = threading.Event()

    ui_writer: OutputWriter | None = None

    initial_transcript = OutputTranscript(initial_output)
    initial_control = FormattedTextControl(lambda: initial_transcript.fragments)
    initial_window = Window(
        content=initial_control,
        wrap_lines=True,
        height=3,
    )
    initial_frame = Frame(initial_window)

    output_transcript = OutputTranscript()
    output_buffer = Buffer(read_only=True)
    output_control = BufferControl(
        buffer=output_buffer,
        lexer=TranscriptLexer(output_transcript),
        focusable=False,
        # 点击 output 区域时把焦点移过来，配合鼠标拖拽做选区，方便复制文本。
        focus_on_click=True,
    )
    output_window = Window(
        content=output_control,
        wrap_lines=True,
        right_margins=[ScrollbarMargin(display_arrows=True)],
    )
    output_frame = Frame(output_window)

    input_area: TextArea | None = None

    def work_loop() -> None:
        while True:
            text = job_queue.get()
            if text == "__EXIT__":
                break
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

            while not state.input_queue.empty():
                job_queue.put(state.input_queue.get())

    async def _run() -> None:
        loop = asyncio.get_running_loop()

        def append_output(output: str | OutputChunk) -> None:
            append_output_text(output_transcript, output)
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

        def render_message(message: Any, *, styled: bool) -> OutputChunk:
            return render_console_chunk(message, styled=styled, width=120, markup=styled)

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
                result = dispatch_slash(text, make_slash_context())
                if result.handled:
                    if result.message:
                        append_output(render_message(result.message, styled=result.markup))
                    if result.should_query:
                        append_output(render_user_prompt(result.prompt, source="user /slash"))
                        job_queue.put(result.prompt)
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
            completer=SlashCompleter(),
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

        def terminal_asker(func: Callable[[], Any] | PromptRequest) -> Any:
            if isinstance(func, dict):
                return asyncio.run_coroutine_threadsafe(ask_in_app(func), loop).result()

            async def _run_in_terminal() -> Any:
                return await run_in_terminal(func)

            return asyncio.run_coroutine_threadsafe(_run_in_terminal(), loop).result()

        worker = threading.Thread(target=work_loop, daemon=True)
        worker.start()

        prompt_ui.set_terminal_asker(terminal_asker)
        try:
            await app.run_async()
        finally:
            ui_writer = None
            prompt_ui.set_terminal_asker(None)

    asyncio.run(_run())
    job_queue.put("__EXIT__")


def _copy_output(
    event: Any,
    output_buffer: Buffer | None,
    output_transcript: OutputTranscript | None,
    on_copy: Callable[[int], None] | None = None,
) -> bool:
    """把 output 的选区（若有）或全部文本复制到系统剪贴板。返回是否复制了内容。"""
    if output_buffer is None or output_transcript is None:
        return False
    if output_buffer.selection_state is not None:
        data = output_buffer.copy_selection()
    else:
        data = ClipboardData(output_transcript.text)
    if data.text:
        event.app.clipboard.set_data(data)
        if on_copy is not None:
            on_copy(len(data.text))
        return True
    return False


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
