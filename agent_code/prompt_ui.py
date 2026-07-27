#!/usr/bin/env python3
"""
##
##       filename: prompt_ui.py
##        created: 2026/07/16
##         author: Paul_Bao
##            IDE: Neovim
##       Version : 1.0
##       Contact : paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

import difflib
import sys
from io import StringIO

import typer

from .runtime import TodoItem

_terminal_asker = None  # 交互 shell 启动时由 interactive.py 注入；one-shot 保持 None


def set_terminal_asker(asker) -> None:
    global _terminal_asker
    _terminal_asker = asker


def _ask(func):
    """worker 要问用户时走这里。交互 shell 注入了 asker → 丢回主线程事件循环问；
    one-shot 没注入（_terminal_asker is None）→ 直接问。"""
    if _terminal_asker is not None:
        return _terminal_asker(func)
    return func()


def render_diff(old: str, new: str, path: str) -> str:
    """用 difflib 生成 unified diff，给增删行加 rich markup 着色。"""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    colored: list[str] = []
    for line in diff_lines:
        line = line.rstrip()
        if line.startswith("---") or line.startswith("+++"):
            colored.append(f"[bold]{line}[/bold]")
        elif line.startswith("-"):
            colored.append(f"[red]{line}[/red]")
        elif line.startswith("+"):
            colored.append(f"[green]{line}[/green]")
        elif line.startswith("@@"):
            colored.append(f"[cyan]{line}[/cyan]")
        else:
            colored.append(line)
    return "\n".join(colored)


def confirm_edit(path: str) -> bool:
    """让用户确认是否应用这次编辑，默认不应用。"""
    return _ask(lambda: typer.confirm(f"Apply this edit to {path}?", default=False))


def confirm_command(command: str) -> bool:
    """让用户确认是否执行这条 bash 命令，默认不执行。"""
    return _ask(lambda: typer.confirm("Run this command?", default=False))


def confirm_tool_use(tool_name: str, detail: str) -> bool:
    """让用户确认非 bash 的 ask 类工具，例如访问外部网络。"""
    return _ask(lambda: typer.confirm(f"Allow {tool_name}: {detail}?", default=False))


def confirm_plan(plan_summary: str) -> bool:
    def _do() -> bool:
        from rich.console import Console
        from rich.panel import Panel

        buffer = StringIO()
        Console(file=buffer, no_color=True).print(
            Panel(
                plan_summary or "(empty plan_summary)",
                title="Plan",
                border_style="blue",
            )
        )
        panel = buffer.getvalue()

        typer.echo(panel, nl=False)

        return typer.confirm("Approve this plan and exit plan mode?", default=False)

    return _ask(_do)


def prompt_single_choice(question: str, labels: list[str]) -> str | None:
    def ask_choice() -> str:
        from rich.console import Console

        console = Console()
        console.print(f"\n[bold yellow]? {question}[/bold yellow]")
        for i, label in enumerate(labels, 1):
            console.print(f"  {i}. {label}")
        console.print("  0. [dim]Skip / Other[/dim]")

        return typer.prompt("Choice", default="0")

    try:
        choice = _ask(ask_choice)
        idx = int(choice)
        if 1 <= idx <= len(labels):
            return labels[idx - 1]
        return None
    except (ValueError, typer.Abort):
        return None
