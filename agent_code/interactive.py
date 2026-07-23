#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File      :   interactive.py
Date      :   2026-07-22 15:28:36
Author    :   baoyihui
Contact   :   yihui.bao@apopgeei.com

主线程 = PromptSession（输入、键位、状态栏 + slash 分派）；
worker 线程 = run_agent（阻塞 provider.complete + 工具执行）。
"""

# here put the import lib
from __future__ import annotations
import asyncio
import queue
import sys
import threading
from typing import Any, Callable
from contextlib import redirect_stderr, redirect_stdout

from rich.console import Console
from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from . import prompt_ui
from .runtime import RuntimeState
from .slash import SlashContext, dispatch_slash

console = Console()


def _call_with_terminal_output(func: Callable[[], Any], stdout_proxy: Any) -> Any:
    """Run a blocking prompt against the real terminal, not StdoutProxy."""
    terminal_output = getattr(stdout_proxy, "original_stdout", None)
    if terminal_output is None:
        return func()
    with redirect_stdout(terminal_output), redirect_stderr(terminal_output):
        return func()


def run_interactive_shell(
    state: RuntimeState,
    run_turn: Callable[[str], None],
    make_slash_context: Callable[[], SlashContext],
) -> None:
    job_queue: queue.Queue[str] = queue.Queue()
    busy = threading.Event()

    def work_loop() -> None:
        while True:
            text = job_queue.get()
            if text == "__EXIT__":
                break
            state.abort_event.clear()
            busy.set()

            try:
                run_turn(text)
            except Exception as e:  # provider/工具异常别让 worker 静默死掉
                print(f"[error] {e}")
            finally:
                busy.clear()

            while not state.input_queue.empty():
                job_queue.put(state.input_queue.get())

    worker = threading.Thread(target=work_loop, daemon=True)
    worker.start()

    session: PromptSession[str] = PromptSession(
        key_bindings=build_key_bindings(state),
        bottom_toolbar=lambda: bottom_toolbar(state),
    )

    async def _run() -> None:
        loop = asyncio.get_running_loop()

        with patch_stdout(raw=True):
            stdout_proxy = sys.stdout

            def terminal_asker(func: Callable[[], Any]) -> Any:
                def call_on_real_terminal() -> Any:
                    # ``typer.confirm`` writes a prompt without a trailing newline.
                    # StdoutProxy buffers that prompt, so it remains invisible while
                    # input() blocks the event loop. Bypass the proxy while the
                    # prompt-toolkit application is suspended by run_in_terminal.
                    return _call_with_terminal_output(func, stdout_proxy)

                async def _run_in_terminal() -> Any:
                    return await run_in_terminal(call_on_real_terminal)

                return asyncio.run_coroutine_threadsafe(
                    _run_in_terminal(), loop
                ).result()

            prompt_ui.set_terminal_asker(terminal_asker)
            try:
                while True:
                    try:
                        text = (await session.prompt_async("> ")).strip()
                    except (KeyboardInterrupt, EOFError):
                        break

                    if not text:
                        continue

                    if text == "/exit":
                        break

                    if text.startswith("/"):
                        result = dispatch_slash(text, make_slash_context())
                        if result.handled:
                            if result.message:
                                console.print(result.message)
                            if result.should_query:
                                job_queue.put(result.prompt)
                            continue

                    if busy.is_set():
                        state.input_queue.put(text)
                        console.print("[queued] turn 结束后处理")
                    else:
                        job_queue.put(text)
            finally:
                prompt_ui.set_terminal_asker(None)

    asyncio.run(_run())
    job_queue.put("__EXIT__")


def build_key_bindings(state: RuntimeState) -> KeyBindings:
    """v1 先只绑 ESC。v2 加 shift+tab。"""
    kb = KeyBindings()

    @kb.add("escape")
    def _(event: Any) -> None:
        state.abort_event.set()  # 只置标志，真正的中断在 Agent Loop 步间处理（v3）

    @kb.add("s-tab")
    def _(event: Any) -> None:
        state.cycle_permission_mode()

    return kb


def bottom_toolbar(state: RuntimeState) -> str:
    """底部状态栏——当前模式 + 模型。"""
    mode = {"default": "default", "acceptEdits": "accept edits", "plan": "plan"}.get(
        state.permission_mode, state.permission_mode
    )
    active = next(
        (t.active_form for t in state.todo_store if t.status == "in_progress"), ""
    )
    todo = f" · {active}" if active else ""
    return f" {mode} · {state.model}{todo} "
