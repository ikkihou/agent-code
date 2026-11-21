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
from rich.markup import escape

from .runtime import PromptAsker, PromptFallback, PromptRequest, RuntimeState


def _ask_request(state: RuntimeState, request: PromptRequest, fallback: PromptFallback) -> Any:
    """Where the worker asks the user something. The interactive shell injects an asker,
    so the question is posted back to the main-thread event loop; one-shot mode injects
    nothing (state.asker is None) and asks directly through the typer fallback."""
    if state.asker is not None:
        return state.asker(request)
    return fallback()


def render_diff(old: str, new: str, path: str, *, markup: bool = True) -> str:
    """Build a unified diff with difflib and color added/removed lines with rich markup.

    With ``markup=True`` each line's *content* is markup-escaped first, so a body
    containing ``[x]`` cannot be parsed as a Rich style when the caller prints with
    ``markup=True``; only the ``[red]``/``[green]`` wrappers added here are markup.
    With ``markup=False`` the diff is returned as plain text (no wrappers), for paths
    that render it verbatim rather than as Rich markup.
    """
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
        if not markup:
            colored.append(line)
        elif line.startswith("---") or line.startswith("+++"):
            colored.append(f"[bold]{escape(line)}[/bold]")
        elif line.startswith("-"):
            colored.append(f"[red]{escape(line)}[/red]")
        elif line.startswith("+"):
            colored.append(f"[green]{escape(line)}[/green]")
        elif line.startswith("@@"):
            colored.append(f"[cyan]{escape(line)}[/cyan]")
        else:
            colored.append(escape(line))
    return "\n".join(colored)


def confirm_edit(state: RuntimeState, path: str, detail: str = "") -> bool:
    """Ask the user whether to apply this edit; defaults to no.

    ``detail`` carries the diff preview so the interactive asker can show the actual
    change instead of asking for a blind approval.
    """
    return bool(
        _ask_request(
            state,
            {
                "type": "confirm",
                "message": f"Apply this edit to {path}?",
                "default": False,
                "detail": detail,
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
    """Ask the user whether to allow a non-bash ask-class tool, e.g. external network access.

    The detail is already embedded in the message here (unlike edits/commands, where the
    diff/command is too big for one line), so don't also send it as the body.
    """
    message = f"Allow {tool_name}: {detail}?"
    return bool(
        _ask_request(
            state,
            {
                "type": "confirm",
                "message": message,
                "default": False,
            },
            lambda: typer.confirm(message, default=False),
        )
    )


def _render_plan_panel(plan_summary: str) -> str:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    buffer = StringIO()
    Console(file=buffer, no_color=True).print(
        Panel(
            # Text(...) so the plan is verbatim: Rich parses markup in plain str
            # renderables, and plan text comes from the model.
            Text(plan_summary or "(empty plan_summary)"),
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


def prompt_single_choice(state: RuntimeState, question: str, labels: list[str]) -> str | None:
    def ask_choice() -> str:
        from rich.console import Console

        console = Console()
        # Question/labels are model-controlled content: markup must stay off, or a
        # label containing "[x]" would be parsed as a Rich style (garbled or crash),
        # and highlight=False keeps bare numbers from being colorized.
        console.print(f"\n? {question}", markup=False, highlight=False)
        for i, label in enumerate(labels, 1):
            console.print(f"  {i}. {label}", markup=False, highlight=False)
        console.print("  0. Skip / Other", markup=False, highlight=False)

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
    except ValueError, typer.Abort:
        return None
