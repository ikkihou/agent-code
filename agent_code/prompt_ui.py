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
    """Where the worker asks the user something. The interactive shell injects an asker,
    so the question is posted back to the main-thread event loop; one-shot mode injects
    nothing (state.asker is None) and asks directly through the typer fallback."""
    if state.asker is not None:
        return state.asker(request)
    return fallback()


def render_diff(old: str, new: str, path: str) -> str:
    """Build a unified diff with difflib and color added/removed lines with rich markup."""
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
    """Ask the user whether to apply this edit; defaults to no."""
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
    """Ask the user whether to run this bash command; defaults to no."""
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
    """Ask the user whether to allow a non-bash ask-class tool, e.g. external network access."""
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
