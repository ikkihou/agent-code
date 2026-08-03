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
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea
from rich.console import Console

from . import prompt_ui
from .runtime import RuntimeState
from .slash import SlashContext, dispatch_slash

console = Console()


OutputWriter = Callable[[str], None]


def append_output_text(output_area: TextArea, text: str) -> None:
    if not text:
        return
    value = output_area.text + text
    output_area.buffer.set_document(
        Document(value, cursor_position=len(value)),
        bypass_readonly=True,
    )


def run_interactive_shell(
    state: RuntimeState,
    run_turn: Callable[[str, OutputWriter], None],
    make_slash_context: Callable[[], SlashContext],
    initial_output: str = "",
) -> None:
    job_queue: queue.Queue[str] = queue.Queue()
    busy = threading.Event()
    ui_writer: OutputWriter | None = None
    output_area = TextArea(
        text=initial_output,
        focusable=False,
        read_only=True,
        scrollbar=True,
        wrap_lines=True,
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

        def append_output(text: str) -> None:
            append_output_text(output_area, text)
            app.invalidate()

        def ui_write(text: str) -> None:
            loop.call_soon_threadsafe(append_output, text)

        nonlocal ui_writer
        ui_writer = ui_write

        def render_message(message: str) -> str:
            from io import StringIO

            buffer = StringIO()
            Console(file=buffer, no_color=True, width=120).print(message)
            return buffer.getvalue()

        def submit_text(text: str) -> None:
            text = text.strip()
            if not text:
                return

            if text == "/exit":
                app.exit()
                return

            if text.startswith("/"):
                result = dispatch_slash(text, make_slash_context())
                if result.handled:
                    if result.message:
                        append_output(render_message(result.message))
                    if result.should_query:
                        job_queue.put(result.prompt)
                    return

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
            prompt="> ",
            wrap_lines=False,
            accept_handler=accept_input,
        )
        root = HSplit(
            [
                output_area,
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

        def terminal_asker(func: Callable[[], Any]) -> Any:
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
