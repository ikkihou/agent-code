from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .runtime import RuntimeState


@dataclass
class SlashContext:
    """slash handler 接收的运行时上下文。不暴露 provider、session 内部状态，，
    只给handler它需要的只读信息。
    """

    cwd: Path
    permission_mode: str
    model: str
    provider: str
    session_id: str | None
    state: RuntimeState | None = None
    registry: SlashRegistry | None = None


class SlashResult:
    """slash command 执行结果。handled=True 表示已处理， CLI 不再把输入当普通 prompt。
    should_query=True 时 CLI 把 prompt 字段作为新的 user 信息喂给模型。
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
    """命令名 → handler 的注册表，仿 tools/core.py 的 ToolRegistry。"""

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
    """列出所有已注册 slash command。"""
    registry = ctx.registry or default_slash_registry()
    lines = ["[bold]可用命令：[/bold]"]
    for command in registry.commands():
        lines.append(f"  [bold]/{command.name}[/bold]  {command.description}")
    # 不调用模型——纯本地控制命令
    return SlashResult(handled=True, message="\n".join(lines), markup=True)


def _cmd_model(args: list[str], ctx: SlashContext) -> SlashResult:
    """显示或切换当前模型。不传参打印当前值；传参时只打印提示，
    告诉用户当前 CLI 实现不支持运行时热切换 provider。"""
    if not args:
        return SlashResult(
            handled=True,
            message=f"provider: {ctx.provider}  model: {ctx.model}",
        )
    target = args[0]
    if ctx.state is not None:
        ctx.state.model = target  # 下一轮 run_turn 按 state.model 重建 provider
    return SlashResult(handled=True, message=f"model → {target}（下一轮生效，当前轮不变）")


def _cmd_context(_args: list[str], ctx: SlashContext) -> SlashResult:
    """打印当前 session 和权限模式。"""
    session = ctx.session_id or "(none)"
    return SlashResult(
        handled=True,
        message=f"cwd: {ctx.cwd}\nsession: {session}\npermission: {ctx.permission_mode}\nmodel: {ctx.provider}/{ctx.model}",
    )


def _cmd_permissions(args: list[str], ctx: SlashContext) -> SlashResult:
    """显示权限模式。v1 不在 REPL 内热切换运行态。"""
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
        message=f"Agent 权限已切换至 {target}",
    )


def _cmd_sessions(args: list[str], ctx: SlashContext) -> SlashResult:
    from .session import _sessions_renderable

    return SlashResult(handled=True, message=_sessions_renderable(ctx.cwd), markup=True)


def _cmd_loop_add(args: list[str], ctx: SlashContext) -> SlashResult:
    """本地 /loop add：直接调 cron_create 的函数逻辑，不用绕模型。"""
    from .cron_tools import cron_create
    from .tools import ToolContext

    if not args:
        return SlashResult(
            handled=True,
            message="用法: /loop add <slash或prompt> --every <60s|5m|2h> --label <标签>",
        )
    # 简单解析：参数以 -- 开头的是选项，其余拼成 slash
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
                    message="--every 需要整数秒，或 60s / 5m / 2h 这种格式",
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
            handled=True, message="用法: /loop add <slash或prompt> --every <60s|5m|2h>"
        )
    if every_seconds is None:
        return SlashResult(
            handled=True,
            message="缺少 --every。用法: /loop add <slash或prompt> --every <60s|5m|2h>",
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
        return SlashResult(handled=True, message="用法: /loop cancel <id>")
    tool_ctx = ToolContext(cwd=ctx.cwd, state=ctx.state)
    msg = cron_cancel({"id": args[0]}, tool_ctx)
    return SlashResult(handled=True, message=msg)


def _cmd_loop(args: list[str], ctx: SlashContext) -> SlashResult:
    """管理 cron 定时任务：/loop add/list/cancel。"""
    if not args:
        return SlashResult(
            handled=True,
            message="用法: /loop add <slash或prompt> --every <60s|5m|2h> --label <标签>\n      /loop list\n      /loop cancel <id>",
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
    """构建并注册内置 slash 命令。调用方（cli.py）每次运行建一份，注入到 SlashContext。"""
    registry = SlashRegistry()
    registry.register("help", "显示所有可用 slash command", _cmd_help)
    registry.register("model", "显示当前模型/provider", _cmd_model)
    registry.register("context", "显示当前 session、cwd、权限模式", _cmd_context)
    registry.register("permissions", "显示权限模式 (default/acceptEdits/plan)", _cmd_permissions)
    registry.register("sessions", "显示当前路径下所有的会话记录", _cmd_sessions)
    registry.register("loop", "管理 cron 定时任务: add/list/cancel", _cmd_loop)
    registry.register("todo", "显示当前 todo 列表", _cmd_todo)
    return registry
