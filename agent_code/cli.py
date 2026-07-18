#!/usr/bin/env python3
"""
##
##       filename: cli.py
##        created: 2026/06/14
##         author: Paul_Bao
##            IDE: Neovim
##       Version : 1.0
##       Contact : paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations
from random import seed
from tkinter import SE

from pathlib import Path

import typer
from rich.console import Console

from .agent import run_agent
from .model import AnthropicProvider, create_provider
from .tools import default_tools
from .session import Session

console = Console()
app = typer.Typer(add_completion=False)


def render_header(cwd: Path, provider: str, model: str, base_url: str | None) -> None:
    console.print("[bold]Agent Code[/bold]")
    console.print(f"[dim]cwd: {cwd}[/dim]")
    console.print(f"[dim]provider: {provider}  model: {model}[/dim]")
    if base_url:
        console.print(f"[dim]base_url: {base_url}[/dim]")
    console.print()


def handle_slash(line: str) -> bool:
    # slash command 是 CLI 控制命令，不交给模型。
    if line == "/help":
        console.print("可用命令：/help, /exit")
        return True
    return False


def run_once(
    prompt: str,
    cwd: Path,
    provider_name: str,
    model: str,
    base_url: str | None,
    max_steps: int,
    permission_mode: str,
    session: Session | None = None,
) -> None:
    render_header(cwd, provider_name, model, base_url)
    if session:
        suffix = " (resumed)" if session.resumed else ""
        console.print(f"[dim]session: {session.session_id}{suffix}[/dim]")
    provider = create_provider(provider_name, model, base_url)
    run_agent(
        prompt,
        provider,
        default_tools(),
        max_steps=max_steps,
        cwd=cwd,
        permission_mode=permission_mode,
        session=session,
    )


def _prompt() -> str | None:
    """读取一行用户输入，返回 stripped 内容，EOF/中断时返回 None。"""
    try:
        line = input("> ")
        return line.strip()
    except (EOFError, KeyboardInterrupt):
        return None


@app.callback(invoke_without_command=True)
def main_command(
    prompt: str = typer.Argument("", help="Prompt to send to the agent."),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", "-C"),
    provider: str = typer.Option("anthropic", "--provider"),
    model: str = typer.Option("deepseek-v4-flash", "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
    max_steps: int = typer.Option(8, "--max-steps"),
    permission_mode: str = typer.Option(
        "default",
        "--permission-mode",
        help="Permission mode: default, acceptEdits, plan",
    ),
    resume: str | None = typer.Option(
        None, "--resume", help="按 session id 恢复指定会话"
    ),
    continue_: bool = typer.Option(
        False, "--continue", "-c", help="恢复 cwd 最近一次会话"
    ),
) -> None:

    # 启动时只解析一次 cwd，让整次运行共享同一个工作目录。
    resolved_cwd = cwd.resolve()

    #
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

    text = prompt.strip()
    if text:
        if session is None:
            session = Session.create(resolved_cwd)
        run_once(
            text,
            resolved_cwd,
            provider,
            model,
            base_url,
            max_steps,
            permission_mode,
            session=session,
        )
        return

    # REPL 分支——命令后面没跟 prompt，走下面交互循环
    render_header(resolved_cwd, provider, model, base_url)
    console.print("输入 /help 查看命令，输入 /exit 退出。")
    if not session:
        session = Session.create(resolved_cwd)
    while True:
        line = _prompt()
        if line is None:  # EOF / Ctrl+D / Ctrl+C
            console.print("\nBye.")
            return
        if not line:
            continue
        if line == "/exit":
            console.print("Bye.")
            return
        if line.startswith("/") and handle_slash(line):
            continue
        run_once(
            text,
            resolved_cwd,
            provider,
            model,
            base_url,
            max_steps,
            permission_mode,
            session,
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
