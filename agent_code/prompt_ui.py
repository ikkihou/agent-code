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
from io import StringIO
from typing import Any

import typer

from .runtime import PromptAsker, PromptFallback, PromptRequest, RuntimeState


def _ask_request(
    state: RuntimeState, request: PromptRequest, fallback: PromptFallback
) -> Any:
    """worker 要问用户时走这里。交互 shell 注入了 asker → 丢回主线程事件循环问；
    one-shot 没注入（state.asker is None）→ 走 typer fallback 直接问。"""
    if state.asker is not None:
        return state.asker(request)
    return fallback()


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


def confirm_edit(state: RuntimeState, path: str) -> bool:
    """让用户确认是否应用这次编辑，默认不应用。"""
    return bool(
        _ask_request(
            state,
            {
                "type": "confirm",
                "message": f"Apply this edit to {path}?",
                "default": False,
            },
            lambda: typer.confirm(f"Apply this edit to {path}?", default=False),
        )
    )


def confirm_command(state: RuntimeState, command: str) -> bool:
    """让用户确认是否执行这条 bash 命令，默认不执行。"""
    return bool(
        _ask_request(
            state,
            {
                "type": "confirm",
                "message": "Run this command?",
                "default": False,
                "detail": command,
            },
            lambda: typer.confirm("Run this command?", default=False),
        )
    )


def confirm_tool_use(state: RuntimeState, tool_name: str, detail: str) -> bool:
    """让用户确认非 bash 的 ask 类工具，例如访问外部网络。"""
    return bool(
        _ask_request(
            state,
            {
                "type": "confirm",
                "message": f"Allow {tool_name}: {detail}?",
                "default": False,
                "detail": detail,
            },
            lambda: typer.confirm(f"Allow {tool_name}: {detail}?", default=False),
        )
    )


def _render_plan_panel(plan_summary: str) -> str:
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
    return buffer.getvalue()


def confirm_plan(state: RuntimeState, plan_summary: str) -> bool:
    panel = _render_plan_panel(plan_summary)

    def _do() -> bool:
        typer.echo(panel, nl=False)

        return typer.confirm("Approve this plan and exit plan mode?", default=False)

    return bool(
        _ask_request(
            state,
            {
                "type": "confirm",
                "message": "Approve this plan and exit plan mode?",
                "default": False,
                "body": panel,
            },
            _do,
        )
    )


def prompt_single_choice(
    state: RuntimeState, question: str, labels: list[str]
) -> str | None:
    def ask_choice() -> str:
        from rich.console import Console

        console = Console()
        console.print(f"\n[bold yellow]? {question}[/bold yellow]")
        for i, label in enumerate(labels, 1):
            console.print(f"  {i}. {label}")
        console.print("  0. [dim]Skip / Other[/dim]")

        return typer.prompt("Choice", default="0")

    try:
        choice = _ask_request(
            state,
            {
                "type": "choice",
                "question": question,
                "labels": labels,
                "default": 0,
            },
            ask_choice,
        )
        idx = int(choice)
        if 1 <= idx <= len(labels):
            return labels[idx - 1]
        return None
    except (ValueError, typer.Abort):
        return None
