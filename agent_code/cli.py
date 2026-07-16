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

from pathlib import Path

import typer
from rich.console import Console

from .agent import run_agent
from .model import AnthropicProvider, create_provider
from .tools import default_tools

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
) -> None:
    render_header(cwd, provider_name, model, base_url)
    provider = create_provider(provider_name, model, base_url)
    run_agent(
        prompt,
        provider,
        default_tools(),
        max_steps=max_steps,
        cwd=cwd,
        permission_mode=permission_mode,
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
) -> None:

    # 启动时只解析一次 cwd，让整次运行共享同一个工作目录。
    resolved_cwd = cwd.resolve()
    text = prompt.strip()

    if text:
        # 有 prompt 参数时进入一次性模式：运行一次就退出。
        run_once(
            text, resolved_cwd, provider, model, base_url, max_steps, permission_mode
        )
        return

    # REPL 分支——命令后面没跟 prompt，走下面交互循环
    render_header(resolved_cwd)
    console.print("输入 /help 查看命令，输入 /exit 退出。")
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
            text, resolved_cwd, provider, model, base_url, max_steps, permission_mode
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
