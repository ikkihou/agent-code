from __future__ import annotations
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File      :   interactive.py
Date      :   2026-07-22 15:28:36
Author    :   baoyihui
Contact   :   yihui.bao@apopgeei.com
"""

# here put the import lib

"""
主线程 = PromptSession（输入、键位、状态栏 + slash 分派）；
worker 线程 = run_agent（阻塞 provider.complete + 工具执行）。
"""

import asyncio
import queue
import threading
from typing import Any, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from . import prompt_ui
from .runtime import RuntimeState
from .slash import SlashContext, dispatch_slash


def run_interactive_shell(
    state: RuntimeState,
    run_turn: Callable[[str], None],
    make_slash_context: Callable[[], SlashContext],
) -> None:
    job_queue: queue.Queue[str] = queue.Queue()

    def work_loop() -> None:
        while True:
            text = job_queue.get()
            if text == "__EXIT__":
                break
            state.abort_event.clear()
            run_turn(text)

    worker = threading.Thread(target=work_loop, daemon=True)
    worker.start()

    session: PromptSession[str] = PromptSession(
        key_bindings=build_key_bindings(state),
        bottom_toolbar=lambda: bottom_toolbar(state),
    )

    async def _run() -> None:
        loop = asyncio.get_running_loop()

        def terminal_asker(func: Callable[[], Any]) -> Any:
            return asyncio.run_coroutine_threadsafe(
                run_in_terminal(func), loop
            ).result()

        prompt_ui.set_terminal_asker(terminal_asker)

        with patch_stdout():
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
                            print(result.message)
                        if result.should_query:
                            job_queue.put(result.prompt)
                        continue

                job_queue.put(text)

    asyncio.run(_run())
    job_queue.put("__EXIT__")


def build_key_bindings(state: RuntimeState) -> KeyBindings:
    """v1 先只绑 ESC。v2 加 shift+tab。"""
    kb = KeyBindings()

    @kb.add("escape")
    def _(event: Any) -> None:
        state.abort_event.set()  # 只置标志，真正的中断在 Agent Loop 步间处理（v3）

    return kb


def bottom_toolbar(state: RuntimeState) -> str:
    """底部状态栏——当前模式 + 模型。"""
    mode = {"default": "default", "acceptEdits": "accept edits", "plan": "plan"}.get(
        state.permission_mode, state.permission_mode
    )
    return f" {mode} · {state.model} "
