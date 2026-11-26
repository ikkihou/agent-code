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
from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from .agent import build_system_prompt, run_agent
from .context import ContextBudget, resolve_window
from .model import create_provider
from .output import OutputChunk, TranscriptItem
from .runtime import RuntimeState
from .scheduler import CronScheduler
from .session import Session
from .slash import SlashContext, default_slash_registry, dispatch_slash
from .tools import default_tools
from .ui.app import run_interactive_shell
from .ui.banner import build_banner
from .ui.events import render_user_prompt
from .ui.transcript import TranscriptSink
from .wire import (
    CommandPreview,
    ContextCompacted,
    DiffPreview,
    Line,
    PromptEcho,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
    Wire,
    WireEvent,
)

console = Console()
app = typer.Typer(add_completion=False)


def render_header(cwd: Path, provider: str, model: str, base_url: str | None) -> None:
    """One-shot frontend: print the same banner the REPL commits into scrollback."""
    console.print(_banner_text(build_banner(cwd, provider, model, base_url)), end="")


def _banner_text(chunk: OutputChunk) -> Text:
    """Banner chunk -> Rich Text (fragments are already in Rich's vocabulary)."""
    text = Text()
    for style, piece in chunk.fragments:
        text.append(piece, style=style)
    return text


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
    context_window: int = 0,
) -> None:
    render_header(cwd, provider_name, model, base_url)
    if session:
        suffix = " (resumed)" if session.resumed else ""
        console.print(f"[dim]session: {session.session_id}{suffix}[/dim]")
    provider = create_provider(provider_name, model, base_url)
    budget = ContextBudget(resolve_window(provider_name, override=context_window))
    state = RuntimeState(
        permission_mode=permission_mode,
        model=model,
        provider=provider_name,
        abort_event=signal if signal is not None else threading.Event(),
        scheduler=scheduler,
        context_budget=budget,
    )
    wire = _console_wire(session.append_transcript if session is not None else None)
    if session is not None:
        # The REPL echoes the submitted prompt into its transcript; one-shot has no
        # prompt echo, so persist it here to keep resumed history complete.
        session.append_transcript(render_user_prompt(prompt))
    wire.send(TurnStarted())
    try:
        run_agent(
            prompt,
            provider,
            default_tools(),
            max_steps=max_steps,
            cwd=cwd,
            state=state,
            session=session,
            system_prompt=system_prompt,
            wire=wire,
            budget=budget,
        )
    finally:
        # Flushes the final streamed paragraph and any pending tool fold.
        wire.send(TurnEnded())


class _ConsoleWireSink:
    """one-shot frontend: a synchronous consumer of wire events that renders straight
    to the terminal.

    run_agent calls send on this same thread and notify is empty, so the sink receives
    each event batch immediately. When ``on_transcript`` is set, the same events also
    feed a ``TranscriptSink``, so one-shot sessions get the same styled transcript
    sidecar the REPL writes (and ``--continue`` can replay them).
    """

    def __init__(self, on_transcript: Callable[[TranscriptItem], None] | None = None) -> None:
        self._on_transcript = on_transcript
        self._transcript = TranscriptSink() if on_transcript is not None else None

    def send(self, events: list[WireEvent]) -> None:
        if self._transcript is not None and self._on_transcript is not None:
            for item in self._transcript.handle_batch(events):
                self._on_transcript(item)
        for event in events:
            if isinstance(event, TextDelta):
                console.print(event.text, markup=False, highlight=False, end="")
            elif isinstance(event, Line):
                console.print(event.text, markup=event.markup, highlight=False)
            elif isinstance(event, PromptEcho):
                console.print(f"> {event.text}", markup=False, highlight=False)
            elif isinstance(event, ToolCallStarted):
                console.print(
                    f"tool_call: {event.name} {event.args_preview}",
                    markup=False,
                    highlight=False,
                )
            elif isinstance(event, ToolCallCompleted):
                suffix = (
                    f" ({event.outcome})"
                    if event.outcome
                    else (" (error)" if event.is_error else "")
                )
                console.print(f"✓ {event.name}{suffix}", markup=False, highlight=False)
            elif isinstance(event, DiffPreview):
                console.print(
                    f"\nDiff for {escape(event.path)}:", markup=True, highlight=False
                )
                from .prompt_ui import render_diff

                # render_diff wraps lines in [red]/[green] markup; diff-line content
                # is escaped inside render_diff, so a body "[x]" cannot be parsed as
                # a Rich style here.
                console.print(
                    render_diff(event.old, event.new, event.path), markup=True, highlight=False
                )
            elif isinstance(event, CommandPreview):
                console.print(
                    f"\nCommand: {event.command}", markup=False, highlight=False
                )
            elif isinstance(event, ContextCompacted):
                console.print(f"[dim]{event.describe()}[/dim]")


def _console_wire(on_transcript: Callable[[TranscriptItem], None] | None = None) -> Wire:
    wire = Wire()
    wire.sink = _ConsoleWireSink(on_transcript).send
    return wire


@app.callback(invoke_without_command=True)
def main_command(
    prompt: str = typer.Argument("", help="Prompt to send to the agent."),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", "-C"),
    provider: str = typer.Option("anthropic", "--provider"),
    model: str = typer.Option("deepseek-flash", "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
    max_steps: int = typer.Option(20, "--max-steps"),
    permission_mode: str = typer.Option(
        "default",
        "--permission-mode",
        help="Permission mode: default, acceptEdits, plan, fullAccess",
    ),
    resume: str | None = typer.Option(None, "--resume", help="resume the given session id"),
    continue_: bool = typer.Option(
        False, "--continue", "-c", help="resume the latest session in cwd"
    ),
    context_window: int = typer.Option(
        0,
        "--context-window",
        help="Model context window in tokens (0 = provider default / env)",
    ),
) -> None:

    # Resolve cwd once at startup so the whole run shares one working directory.
    resolved_cwd = cwd.resolve()

    # Load the conversation record.
    session: Session | None = None
    if continue_:
        session = Session.load_latest(resolved_cwd)
        if session is None:
            console.print("[red]No earlier session found, cannot --continue.[/red]")
            raise typer.Exit(code=1)
    elif resume:
        session = Session.load_by_id(resolved_cwd, resume)
        if session is None:
            console.print(f"[red]session not found: {resume}[/red]")
            raise typer.Exit(code=1)

    ## Create and start cron scheduler
    scheduler = CronScheduler(resolved_cwd)
    scheduler.start()
    slash_registry = default_slash_registry()

    # Build the system prompt.
    system_prompt = build_system_prompt(cwd)

    def run_user_input(line: str) -> None:
        """Single entry point for user input: try slash dispatch first, then enter the agent
        loop when nothing matched. Both REPL input and cron pending prompts come
        through here."""
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
                context_window=context_window,
            ),
        )
        if slash_result.handled:
            if slash_result.message:
                console.print(slash_result.message, markup=slash_result.markup)
            if slash_result.should_query:
                # Run the prompt expanded by the slash command as a new user input.
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
                    context_window=context_window,
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
            context_window=context_window,
        )

    # (1) One-shot branch.
    text = prompt.strip()
    if text:
        run_user_input(text.strip())
        return

    # (2) REPL branch -- no prompt after the command, fall into the interactive loop below.
    if session is None:
        session = Session.create(resolved_cwd)

    state = RuntimeState(
        permission_mode=permission_mode,
        model=model,
        provider=provider,
        scheduler=scheduler,
        context_budget=ContextBudget(
            resolve_window(
                provider,
                override=context_window,
            )
        ),
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
            budget=state.context_budget,
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
            context_window=context_window,
        )

    initial_output = build_banner(resolved_cwd, provider, model, base_url)
    run_interactive_shell(
        state,
        run_turn,
        make_slash_context,
        initial_output,
        initial_transcript=session.transcript_items,
        on_transcript=session.append_transcript,
        drain_pending=scheduler.drain_pending,
        slash_registry=slash_registry,
        cwd=resolved_cwd,
    )


def _ensure_utf8_stdio() -> None:
    """Switch the standard streams to UTF-8 so writing as GBK cannot crash on Chinese
    Windows.

    The real console goes through Python's WindowsConsoleIO with wide characters and
    is usually fine; the problems come from redirection/pipes to files, where Python
    writes with the locale encoding (GBK), or from a child process emitting GBK while we
    decode as UTF-8 -- the latter is already handled in ``decode_subprocess_output``
    with a UTF-8 -> system encoding fallback. This normalizes the upstream side, and
    together with PYTHONIOENCODING makes the whole chain (bash/hooks/rg children
    included) speak one encoding.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except ValueError, OSError:
            continue
    # Make child processes (bash / hooks / rg) emit UTF-8, matching the decoding
    # convention above.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def main() -> None:
    _ensure_utf8_stdio()
    app()


if __name__ == "__main__":
    main()
