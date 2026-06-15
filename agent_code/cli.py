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
from .model import ModelProvider, AnthropicProvider
from .tools import default_tools

console = Console()
app = typer.Typer(add_completion=False)


def render_header(cwd: Path) -> None:
    # cwd 是后续文件工具和 bash 工具都要遵守的工作边界。
    console.print("[bold]Agent Code[/bold]")
    console.print(f"[dim]cwd: {cwd}[/dim]\n")


def handle_slash(line: str) -> bool:
    # slash command 是 CLI 控制命令，不交给模型。
    if line == "/help":
        console.print("可用命令：/help, /exit")
        return True
    return False


def run_once(prompt: str, cwd: Path) -> None:
    render_header(cwd)
    try:
        result = run_agent(prompt, AnthropicProvider(), default_tools(), cwd=cwd)
        for line in result.trace:
            console.print(line)
    except Exception as e:
        console.print(f"[red]Agent 出错：{e}[/red]")


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
) -> None:
    # 启动时只解析一次 cwd，让整次运行共享同一个工作目录。
    resolved_cwd = cwd.resolve()
    text = prompt.strip()

    if text:
        # 有 prompt 参数时进入一次性模式：运行一次就退出。
        run_once(text, resolved_cwd)
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
        run_once(line, resolved_cwd)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
