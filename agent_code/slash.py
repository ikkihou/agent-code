from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .runtime import RuntimeState


@dataclass
class SlashContext:
    """Runtime context handed to slash handlers. Never exposes provider or session
    internals; gives a handler only the read-only information it needs.
    """

    cwd: Path
    permission_mode: str
    model: str
    provider: str
    session_id: str | None
    state: RuntimeState | None = None
    registry: SlashRegistry | None = None


class SlashResult:
    """Result of running a slash command. handled=True means it was consumed and the
    CLI must not treat the input as a normal prompt. should_query=True makes the CLI
    feed the prompt field to the model as a new user message.
    """

    def __init__(
        self,
        handled: bool = True,
        should_query: bool = False,
        prompt: str = "",
        message: Any = "",
        markup: bool = False,
    ) -> None:
        self.handled = handled
        self.should_query = should_query
        self.prompt = prompt
        self.message = message
        self.markup = markup


SlashHandler = Callable[[list[str], SlashContext], SlashResult]


@dataclass
class SlashCommand:
    name: str
    description: str
    handler: SlashHandler


class SlashRegistry:
    """Registry mapping command name -> handler, modelled on tools/core.py's ToolRegistry."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, name: str, description: str, handler: SlashHandler) -> None:
        self._commands[name] = SlashCommand(name, description, handler)

    def commands(self) -> list[SlashCommand]:
        return [self._commands[name] for name in sorted(self._commands)]

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name)


def dispatch_slash(line: str, ctx: SlashContext) -> SlashResult:
    if not line.startswith("/"):
        return SlashResult(handled=False)

    try:
        parts = shlex.split(line[1:].strip())
    except ValueError as e:
        return SlashResult(handled=True, message=f"Invalid command syntax: {e}")
    if not parts:
        return SlashResult(handled=False)

    name = parts[0]
    args = parts[1:]
    registry = ctx.registry or default_slash_registry()
    cmd = registry.get(name)

    if cmd is None:
        return SlashResult(handled=True, message=f"Unknow command: /{name}")

    return cmd.handler(args, ctx)


## ----------------- handlers --------------------


def _cmd_help(_args: list[str], ctx: SlashContext) -> SlashResult:
    """List every registered slash command."""
    registry = ctx.registry or default_slash_registry()
    lines = ["[bold]Available commands:[/bold]"]
    for command in registry.commands():
        lines.append(f"  [bold]/{command.name}[/bold]  {command.description}")
    # Never calls the model -- a purely local control command.
    return SlashResult(handled=True, message="\n".join(lines), markup=True)


def _cmd_model(args: list[str], ctx: SlashContext) -> SlashResult:
    """Show or switch the current model. With no argument it prints the current values;
    with an argument it only prints a notice, because the current CLI implementation
    does not support hot-swapping the provider at runtime."""
    if not args:
        return SlashResult(
            handled=True,
            message=f"provider: {ctx.provider}  model: {ctx.model}",
        )
    target = args[0]
    if ctx.state is not None:
        ctx.state.model = target  # the next run_turn rebuilds the provider from state.model
    return SlashResult(handled=True, message=f"model → {target} (applies next turn, not this one)")


def _cmd_context(_args: list[str], ctx: SlashContext) -> SlashResult:
    """Print the current session and permission mode."""
    session = ctx.session_id or "(none)"
    return SlashResult(
        handled=True,
        message=f"cwd: {ctx.cwd}\nsession: {session}\npermission: {ctx.permission_mode}\nmodel: {ctx.provider}/{ctx.model}",
    )


def _cmd_permissions(args: list[str], ctx: SlashContext) -> SlashResult:
    """Show the permission mode. v1 does not hot-swap runtime state from the REPL."""
    modes = ["default", "acceptEdits", "plan"]
    if not args:
        return SlashResult(
            handled=True,
            message=f"permission mode: {ctx.permission_mode}\navailable: {', '.join(modes)}",
        )
    target = args[0]
    if target not in modes:
        return SlashResult(handled=True, message=f"Unknown mode: {target}. Use: {', '.join(modes)}")
    assert ctx.state is not None
    ctx.state.permission_mode = target
    return SlashResult(
        handled=True,
        message=f"Agent permission switched to {target}",
    )


def _cmd_sessions(args: list[str], ctx: SlashContext) -> SlashResult:
    from .session import _sessions_renderable

    return SlashResult(handled=True, message=_sessions_renderable(ctx.cwd), markup=True)


def _cmd_loop_add(args: list[str], ctx: SlashContext) -> SlashResult:
    """Local /loop add: calls cron_create's function directly instead of going through the model."""
    from .cron_tools import cron_create
    from .tools import ToolContext

    if not args:
        return SlashResult(
            handled=True,
            message="Usage: /loop add <slash-or-prompt> --every <60s|5m|2h> --label <label>",
        )
    # Simple parse: arguments starting with -- are options, the rest form the slash.
    slash_parts: list[str] = []
    every_seconds: int | None = None
    label = ""
    i = 0

    def _parse_every(raw: str) -> int:
        units = {"s": 1, "m": 60, "h": 3600}
        if raw[-1:] in units:
            return int(raw[:-1]) * units[raw[-1]]
        return int(raw)

    while i < len(args):
        if args[i] == "--every" and i + 1 < len(args):
            try:
                every_seconds = _parse_every(args[i + 1])
            except (ValueError, IndexError):
                return SlashResult(
                    handled=True,
                    message="--every needs whole seconds, or a form like 60s / 5m / 2h",
                )
            i += 2
        elif args[i] == "--label" and i + 1 < len(args):
            label = args[i + 1]
            i += 2
        else:
            slash_parts.append(args[i])
            i += 1
    slash = " ".join(slash_parts)
    if not slash:
        return SlashResult(
            handled=True, message="Usage: /loop add <slash-or-prompt> --every <60s|5m|2h>"
        )
    if every_seconds is None:
        return SlashResult(
            handled=True,
            message="Missing --every. Usage: /loop add <slash-or-prompt> --every <60s|5m|2h>",
        )
    tool_ctx = ToolContext(cwd=ctx.cwd, state=ctx.state)
    msg = cron_create({"slash": slash, "every_seconds": every_seconds, "label": label}, tool_ctx)
    return SlashResult(handled=True, message=msg)


def _cmd_loop_list(_args: list[str], ctx: SlashContext) -> SlashResult:
    from .cron_tools import cron_list
    from .tools import ToolContext

    tool_ctx = ToolContext(cwd=ctx.cwd, state=ctx.state)
    msg = cron_list({}, tool_ctx)
    return SlashResult(handled=True, message=msg)


def _cmd_loop_cancel(args: list[str], ctx: SlashContext) -> SlashResult:
    from .cron_tools import cron_cancel
    from .tools import ToolContext

    if not args:
        return SlashResult(handled=True, message="Usage: /loop cancel <id>")
    tool_ctx = ToolContext(cwd=ctx.cwd, state=ctx.state)
    msg = cron_cancel({"id": args[0]}, tool_ctx)
    return SlashResult(handled=True, message=msg)


def _cmd_loop(args: list[str], ctx: SlashContext) -> SlashResult:
    """Manage cron jobs: /loop add/list/cancel."""
    if not args:
        return SlashResult(
            handled=True,
            message=(
                "Usage: /loop add <slash-or-prompt> --every <60s|5m|2h> --label <label>\n"
                "      /loop list\n"
                "      /loop cancel <id>"
            ),
        )
    subcommand = args[0]
    rest = args[1:]
    if subcommand == "add":
        return _cmd_loop_add(rest, ctx)
    if subcommand == "list":
        return _cmd_loop_list(rest, ctx)
    if subcommand == "cancel":
        return _cmd_loop_cancel(rest, ctx)
    return SlashResult(handled=True, message=f"Unknown /loop subcommand: {subcommand}")


def _cmd_todo(_args: list[str], ctx: SlashContext) -> SlashResult:
    items = ctx.state.todo_store if ctx.state else []
    icon = {"pending": "○", "in_progress": "◉", "completed": "✓"}
    body = "\n".join(f"  {icon.get(t.status, '?')} {t.content}" for t in items) or "(no todos)"
    return SlashResult(handled=True, message=body)


def default_slash_registry() -> SlashRegistry:
    """Build and register the built-in slash commands. The caller (cli.py) builds one per
    run and injects it into SlashContext."""
    registry = SlashRegistry()
    registry.register("help", "show all available slash commands", _cmd_help)
    registry.register("model", "show the current model/provider", _cmd_model)
    registry.register("context", "show the current session, cwd and permission mode", _cmd_context)
    registry.register(
        "permissions", "show the permission mode (default/acceptEdits/plan)", _cmd_permissions
    )
    registry.register("sessions", "list all session records under the current path", _cmd_sessions)
    registry.register("loop", "manage cron jobs: add/list/cancel", _cmd_loop)
    registry.register("todo", "show the current todo list", _cmd_todo)
    return registry
