from __future__ import annotations

import shlex

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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


class SlashResult:
    """slash command 执行结果。handled=True 表示已处理， CLI 不再把输入当普通 prompt。
    should_query=True 时 CLI 把 prompt 字段作为新的 user 信息喂给模型。
    """

    def __init__(
        self,
        handled: bool = True,
        should_query: bool = False,
        prompt: str = "",
        message: str = "",
    ) -> None:
        self.handled = handled
        self.should_query = should_query
        self.prompt = prompt
        self.message = message


SlashHandler = Callable[[list[str], SlashContext], SlashResult]


@dataclass
class SlashCommand:
    name: str
    description: str
    handler: SlashHandler


_registry: dict[str, SlashCommand] = {}


def register(name: str, description: str, handler: SlashHandler) -> None:
    _registry[name] = SlashCommand(name, description, handler)


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
    cmd = _registry.get(name)

    if cmd is None:
        return SlashResult(handled=True, message=f"Unknow command: /{name}")

    return cmd.handler(args, ctx)


## ----------------- handlers --------------------


def _cmd_help(_args: list[str], ctx: SlashContext) -> SlashResult:
    """列出所有已注册 slash command。"""
    lines = ["[bold]可用命令：[/bold]"]
    for name in sorted(_registry.keys()):
        desc = _registry[name].description
        lines.append(f"  [bold]/{name}[/bold]  {desc}")
    # 不调用模型——纯本地控制命令
    return SlashResult(handled=True, message="\n".join(lines))


def _cmd_model(args: list[str], ctx: SlashContext) -> SlashResult:
    """显示或切换当前模型。不传参打印当前值；传参时只打印提示，
    告诉用户当前 CLI 实现不支持运行时热切换 provider。"""
    if not args:
        return SlashResult(
            handled=True,
            message=f"provider: {ctx.provider}  model: {ctx.model}",
        )
    # 切换模型需要重建 provider，牵涉到 Anthropic/OpenAI 客户端实例化，
    # 当前版本不支持 REPL 中热切换。Day 8 随着 Plan Mode 一起做。
    return SlashResult(
        handled=True,
        message=f"Cannot change model at runtime. Current: {ctx.provider}/{ctx.model}",
    )


def _cmd_context(_args: list[str], ctx: SlashContext) -> SlashResult:
    """打印当前 session 和权限模式。"""
    session = ctx.session_id or "(none)"
    return SlashResult(
        handled=True,
        message=f"cwd: {ctx.cwd}\nsession: {session}\npermission: {ctx.permission_mode}\nmodel: {ctx.provider}/{ctx.model}",
    )


def _cmd_compact(_args: list[str], ctx: SlashContext) -> SlashResult:
    """显示 compact 状态。真正的手动 compact 需要能重写 session 历史，先不做。"""
    # Day 6 已经有自动 compact：run_agent 里 messages 超过阈值会触发。
    # 手动 /compact 要重写 Session JSONL 或当前 messages，这会扩大 v1 的状态管理范围。
    return SlashResult(
        handled=True,
        message="compact: 当前版本只支持自动 compact。messages 超过阈值时会在 Agent Loop 内触发。",
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
        return SlashResult(
            handled=True, message=f"Unknown mode: {target}. Use: {', '.join(modes)}"
        )
    return SlashResult(
        handled=True,
        message=f"当前版本不在 REPL 内热切换权限模式。请用 --permission-mode {target} 重新启动。",
    )


def _cmd_plan(args: list[str], ctx: SlashContext) -> SlashResult:
    """显示 plan 模式提示。完整 Plan Mode 闭环等 Day 8。"""
    if args and args[0] == "off":
        return SlashResult(
            handled=True,
            message="当前版本不在 REPL 内热切换权限模式。请重新用 --permission-mode default 启动。",
        )
    if ctx.permission_mode == "plan":
        return SlashResult(
            handled=True, message="当前已经是 plan 模式。完整审批闭环会在 Day 8 实现。"
        )
    return SlashResult(
        handled=True,
        message="要进入 plan 模式，请重新用 --permission-mode plan 启动。完整审批闭环会在 Day 8 实现。",
    )


def _cmd_sessions(args: list[str], ctx: SlashContext) -> SlashResult:
    from .session import _render_sessions

    _render_sessions(ctx.cwd)
    return SlashResult(
        handled=True,
        message="",
    )


# --- 注册内置命令 ---
register("help", "显示所有可用 slash command", _cmd_help)
register("model", "显示当前模型/provider", _cmd_model)
register("context", "显示当前 session、cwd、权限模式", _cmd_context)
register("compact", "显示 compact 状态", _cmd_compact)
register("permissions", "显示权限模式 (default/acceptEdits/plan)", _cmd_permissions)
register("plan", "显示 plan 模式提示", _cmd_plan)
register("sessions", "显示当前路径下所有的会话记录", _cmd_sessions)
