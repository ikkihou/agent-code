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
from typing import Any, Callable

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.widgets import TextArea

from . import prompt_ui
from .output import OutputChunk, OutputWriter, render_console_chunk
from .runtime import RuntimeState
from .slash import SlashContext, dispatch_slash

PromptRequest = dict[str, Any]
PendingPrompt = dict[str, Any]
StyleAndText = tuple[str, str]


class OutputTranscript:
    def __init__(self, initial_text: str = "") -> None:
        self.fragments: list[StyleAndText] = []
        self.text = ""
        self.line_count = 0
        append_output_text(self, initial_text)


def append_output_text(transcript: OutputTranscript, output: str | OutputChunk) -> None:
    chunk = output if isinstance(output, OutputChunk) else OutputChunk(output)
    if not chunk.text:
        return
    fragments = _chunk_fragments(chunk)
    display_text = "".join(text for _, text in fragments)
    transcript.text += display_text
    transcript.line_count += display_text.count("\n")
    _append_fragments(transcript.fragments, fragments)


def _chunk_fragments(chunk: OutputChunk) -> list[StyleAndText]:
    if chunk.format == "ansi":
        return [(style, text) for style, text in ANSI(chunk.text).__pt_formatted_text__()]
    return [("", chunk.text)]


def _append_fragments(target: list[StyleAndText], fragments: list[StyleAndText]) -> None:
    for style, text in fragments:
        if not text:
            continue
        if target and target[-1][0] == style:
            last_style, last_text = target[-1]
            target[-1] = (last_style, last_text + text)
        else:
            target.append((style, text))


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
    output_transcript = OutputTranscript(initial_output)
    output_control = FormattedTextControl(lambda: output_transcript.fragments)
    output_window = Window(
        content=output_control,
        wrap_lines=True,
        right_margins=[ScrollbarMargin(display_arrows=True)],
    )
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
            output_window.vertical_scroll = max(0, output_transcript.line_count)
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
            accept_handler=accept_input,
        )
        root = HSplit(
            [
                output_window,
                input_area,
                Window(
                    FormattedTextControl(lambda: bottom_toolbar(state)),
                    height=1,
                    style="reverse",
                ),
            ]
        )
        app = Application(
            layout=Layout(root, focused_element=input_area),
            key_bindings=build_key_bindings(state),
            full_screen=True,
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


def build_key_bindings(state: RuntimeState) -> KeyBindings:
    """v1 先只绑 ESC。v2 加 shift+tab。"""
    kb = KeyBindings()

    @kb.add("escape")
    def _(event: Any) -> None:
        # Provider 会观察该信号并主动关闭正在读取的 HTTP stream。
        state.abort_event.set()

    @kb.add("s-tab")
    def _(event: Any) -> None:
        state.cycle_permission_mode()
        event.app.invalidate()

    @kb.add("c-c")
    @kb.add("c-d")
    def _(event: Any) -> None:
        event.app.exit()

    return kb


def bottom_toolbar(state: RuntimeState) -> str:
    """底部状态栏——当前模式 + 模型。"""
    mode = {"default": "default", "acceptEdits": "accept edits", "plan": "plan"}.get(
        state.permission_mode, state.permission_mode
    )
    active = next((t.active_form for t in state.todo_store if t.status == "in_progress"), "")
    todo = f" · {active}" if active else ""
    return f" {mode} · {state.model}{todo} "
