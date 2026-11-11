"""
##       filename: cli.py
##        created: 2026/06/14
##         author: Paul_Bao
##            IDE: Neovim
##       Version : 1.0
##       Contact : paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import typer
from rich.console import Console

from .agent import build_system_prompt, run_agent
from .interactive import run_interactive_shell
from .model import create_provider
from .runtime import RuntimeState
from .scheduler import CronScheduler
from .session import Session
from .slash import SlashContext, default_slash_registry, dispatch_slash
from .tools import default_tools
from .wire import (
    CommandPreview,
    DiffPreview,
    Line,
    PromptEcho,
    TextDelta,
    ToolCallStarted,
    Wire,
)

console = Console()
app = typer.Typer(add_completion=False)


def render_header(cwd: Path, provider: str, model: str, base_url: str | None) -> None:
    console.print("[bold]Agent Code[/bold]")
    console.print(f"[dim]cwd: {cwd}[/dim]")
    console.print(f"[dim]provider: {provider}  model: {model}[/dim]")
    if base_url:
        console.print(f"[dim]base_url: {base_url}[/dim]")
    console.print()


def header_text(cwd: Path, provider: str, model: str, base_url: str | None) -> str:
    lines = [
        "Agent Code",
        f"cwd: {cwd}",
        f"provider: {provider}  model: {model}",
    ]
    if base_url:
        lines.append(f"base_url: {base_url}")
    return "\n".join(lines) + "\n\n"


def run_once(
    prompt: str,
    cwd: Path,
    provider_name: str,
    model: str,
    base_url: str | None,
    max_steps: int,
    permission_mode: str,
    session: Session | None = None,
    system_prompt: str | None = None,
    signal: threading.Event | None = None,
    scheduler: CronScheduler | None = None,
) -> None:
    render_header(cwd, provider_name, model, base_url)
    if session:
        suffix = " (resumed)" if session.resumed else ""
        console.print(f"[dim]session: {session.session_id}{suffix}[/dim]")
    provider = create_provider(provider_name, model, base_url)
    state = RuntimeState(
        permission_mode=permission_mode,
        model=model,
        provider=provider_name,
        abort_event=signal if signal is not None else threading.Event(),
        scheduler=scheduler,
    )
    run_agent(
        prompt,
        provider,
        default_tools(),
        max_steps=max_steps,
        cwd=cwd,
        state=state,
        session=session,
        system_prompt=system_prompt,
        wire=_console_wire(),
    )


class _ConsoleWireSink:
    """one-shot 前端：wire 事件的同步消费者，直接渲染到终端。

    run_agent 在同线程调用 send → notify 为空 → sink 立刻收到事件批。
    """

    def send(self, events: list[object]) -> None:
        for event in events:
            if isinstance(event, TextDelta):
                console.print(event.text, markup=False, highlight=False, end="")
            elif isinstance(event, Line):
                console.print(event.text, markup=event.markup, highlight=False)
            elif isinstance(event, PromptEcho):
                console.print(f"> {event.text}")
            elif isinstance(event, ToolCallStarted):
                console.print(f"tool_call: {event.name} {event.args_preview}")
            elif isinstance(event, DiffPreview):
                console.print(f"\nDiff for {event.path}:", markup=True)
                from .prompt_ui import render_diff

                console.print(render_diff(event.old, event.new, event.path), markup=True)
            elif isinstance(event, CommandPreview):
                console.print(f"\nCommand: {event.command}", markup=True)


def _console_wire() -> Wire:
    wire = Wire()
    wire.sink = _ConsoleWireSink().send
    return wire


@app.callback(invoke_without_command=True)
def main_command(
    prompt: str = typer.Argument("", help="Prompt to send to the agent."),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", "-C"),
    provider: str = typer.Option("anthropic", "--provider"),
    model: str = typer.Option("workbuddy/auto", "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
    max_steps: int = typer.Option(20, "--max-steps"),
    permission_mode: str = typer.Option(
        "default",
        "--permission-mode",
        help="Permission mode: default, acceptEdits, plan",
    ),
    resume: str | None = typer.Option(None, "--resume", help="按 session id 恢复指定会话"),
    continue_: bool = typer.Option(False, "--continue", "-c", help="恢复 cwd 最近一次会话"),
) -> None:

    # 启动时只解析一次 cwd，让整次运行共享同一个工作目录。
    resolved_cwd = cwd.resolve()

    # 加载对话记录
    session: Session | None = None
    if continue_:
        session = Session.load_latest(resolved_cwd)
        if session is None:
            console.print("[red]没有找到历史会话，无法 --continue。[/red]")
            raise typer.Exit(code=1)
    elif resume:
        session = Session.load_by_id(resolved_cwd, resume)
        if session is None:
            console.print(f"[red]找不到 session: {resume}[/red]")
            raise typer.Exit(code=1)

    ## Create and start cron scheduler
    scheduler = CronScheduler(resolved_cwd)
    scheduler.start()
    slash_registry = default_slash_registry()

    # 构造系统提示词
    system_prompt = build_system_prompt(cwd)

    def run_user_input(line: str) -> None:
        """统一处理用户输入：先走 slash dispatch，未命中再进入 Agent Loop。
        REPL 用户输入和 cron pending prompt 都必须走这个入口。"""
        nonlocal session
        slash_result = dispatch_slash(
            line,
            SlashContext(
                cwd=resolved_cwd,
                permission_mode=permission_mode,
                model=model,
                provider=provider,
                session_id=session.session_id if session else None,
                registry=slash_registry,
            ),
        )
        if slash_result.handled:
            if slash_result.message:
                console.print(slash_result.message, markup=slash_result.markup)
            if slash_result.should_query:
                # 把 slash 展开的 prompt 作为新一轮用户输入跑 Agent Loop
                if session is None:
                    session = Session.create(resolved_cwd)
                run_once(
                    slash_result.prompt,
                    resolved_cwd,
                    provider,
                    model,
                    base_url,
                    max_steps,
                    permission_mode,
                    session=session,
                    system_prompt=system_prompt,
                    scheduler=scheduler,
                )
            return

        if session is None:
            session = Session.create(resolved_cwd)
        run_once(
            line,
            resolved_cwd,
            provider,
            model,
            base_url,
            max_steps,
            permission_mode,
            session=session,
            system_prompt=system_prompt,
            scheduler=scheduler,
        )

    # (1) 一次性调用分支
    text = prompt.strip()
    if text:
        run_user_input(text.strip())
        return

    # (2) REPL 分支——命令后面没跟 prompt，走下面交互循环
    if session is None:
        session = Session.create(resolved_cwd)

    state = RuntimeState(
        permission_mode=permission_mode, model=model, provider=provider, scheduler=scheduler
    )
    tools = default_tools()

    def run_turn(line: str, wire: Wire) -> None:
        turn_provider = create_provider(state.provider, state.model, base_url)
        run_agent(
            line,
            turn_provider,
            tools,
            max_steps=max_steps,
            cwd=resolved_cwd,
            state=state,
            session=session,
            system_prompt=system_prompt,
            wire=wire,
        )

    def make_slash_context() -> SlashContext:
        return SlashContext(
            cwd=resolved_cwd,
            permission_mode=state.permission_mode,
            model=state.model,
            provider=state.provider,
            session_id=session.session_id if session else None,
            state=state,
            registry=slash_registry,
        )

    initial_output = header_text(resolved_cwd, provider, model, base_url)
    run_interactive_shell(
        state,
        run_turn,
        make_slash_context,
        initial_output,
        initial_transcript=session.transcript_chunks,
        on_transcript=session.append_transcript,
        drain_pending=scheduler.drain_pending,
        slash_registry=slash_registry,
    )


def _ensure_utf8_stdio() -> None:
    """把标准流切到 UTF-8，避免中文 Windows 下按 GBK 编码写出时崩溃。

    真正控制台由 Python 的 WindowsConsoleIO 走宽字符，一般不出问题；出问题的是
    重定向/管道到文件时 Python 用 locale 编码（GBK）写出，或子进程按 GBK 产出后
    被我方按 UTF-8 解码——后者在 ``decode_subprocess_output`` 里已按
    UTF-8 → 系统编码兜底。这里统一上游，连同 PYTHONIOENCODING 一起让整条
    链路（含 bash/hooks/rg 子进程）说同一种编码。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue
    # 让子进程（bash / hooks / rg）也按 UTF-8 产出，和上面的解码约定一致。
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def main() -> None:
    _ensure_utf8_stdio()
    app()


if __name__ == "__main__":
    main()
