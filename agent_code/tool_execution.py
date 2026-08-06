"""工具调用执行层：单次工具调用的权限决策 → 确认/拦截 → 执行 → hooks。

从 agent.py 拆出，承接 execute_one_tool_call 及其配套的 plan 边界与并行分组逻辑，
让 run_agent 主循环只负责调度，不内联每种工具的特殊分支。
"""

from __future__ import annotations

from typing import Any, Callable

from .fs_safety import (
    apply_single_replace,
    check_mtime_conflict,
    ensure_read_before_edit,
    resolve_in_cwd,
)
from .hooks import run_hooks
from .model import ToolCall, ToolResult
from .permissions import PermissionRequest, decide_permission
from .prompt_ui import (
    confirm_command,
    confirm_edit,
    confirm_plan,
    confirm_tool_use,
    prompt_single_choice,
    render_diff,
)
from .runtime import RuntimeState
from .tools import ToolContext, ToolRegistry

PrintFunc = Callable[..., None]  # 本地重声明，勿 import agent.PrintFunc（防循环导入）


def _error_block(tool_call_id: str, content: str) -> dict[str, Any]:
    """构造一个 error 性质的 tool_result block。"""
    return {
        "type": "tool_result",
        "tool_use_id": tool_call_id,
        "content": content,
        "is_error": True,
    }


def _error_observation(
    emit: Callable[[str], None], call_id: str, content: str
) -> dict[str, Any]:
    """发射小写 observation 并返回 error block。

    除 exit_plan_mode 拒绝路径（那条保持大写 O 才在终端可见）外，所有 error 走这里。
    """
    emit(f"observation: {content}")
    return _error_block(call_id, content)


def approve_plan(state: RuntimeState, plan_text: str) -> bool:
    """plan 批准统一入口：确认则把模式切到 acceptEdits 并返回 True。"""
    if confirm_plan(state, plan_text):
        state.permission_mode = "acceptEdits"
        return True
    return False


def _edit_preflight(
    call: ToolCall, ctx: ToolContext
) -> tuple[tuple[str, str, str] | None, str | None]:
    """file_write/file_edit 的读前校验 + diff 预览构造。

    Returns:
        (edit_preview, error_content)：preview 为 (path_str, old_content, new_content)；
        error_content 非 None 表示校验失败，调用方应直接返回 error block。
    """
    path_str = call.arguments.get("file_path", "")
    if not path_str:
        return None, "error: missing required argument 'file_path'"
    try:
        path = resolve_in_cwd(ctx.cwd, path_str)
    except (ValueError, OSError) as exc:
        return None, f"error: {exc}"

    old_content = path.read_text(encoding="utf-8") if path.exists() else ""
    validation_error: str | None = None
    if call.name == "file_write":
        if path.exists():
            validation_error = ensure_read_before_edit(
                ctx.read_state, path
            ) or check_mtime_conflict(ctx.read_state, path)
        new_content = call.arguments.get("content", "")
    else:
        new_content = ""
        if not path.exists():
            validation_error = f"error: file does not exist: {path_str}"
        else:
            validation_error = ensure_read_before_edit(
                ctx.read_state, path
            ) or check_mtime_conflict(ctx.read_state, path)
        if validation_error is None:
            new_content, replace_err = apply_single_replace(
                old_content,
                call.arguments.get("old_string", ""),
                call.arguments.get("new_string", ""),
                bool(call.arguments.get("replace_all", False)),
            )
            if replace_err is not None:
                validation_error = replace_err
    if validation_error is not None:
        return None, validation_error
    return (path_str, old_content, new_content if new_content else ""), None


def _handle_ask(
    call: ToolCall,
    edit_preview: tuple[str, str, str] | None,
    state: RuntimeState,
    emit: Callable[[str], None],
    print_output: PrintFunc,
) -> dict[str, Any] | None:
    """ask 模式下按工具类型分发确认 UI。

    Returns:
        返回 block 表示流程到此终止（拒绝或被拦截）；返回 None 表示确认通过，继续执行。
    """
    if call.name in ("file_write", "file_edit") and edit_preview is not None:
        path_str, old_content, new_content = edit_preview
        print_output(f"\n[bold]Diff for {path_str}:[/bold]")
        print_output(render_diff(old_content, new_content, path_str))
        if not confirm_edit(state, path_str):
            return _error_observation(emit, call.id, "error: edit rejected by user")
    elif call.name == "bash":
        command = call.arguments.get("command", "")
        print_output(f"\n[bold yellow]Command:[/bold yellow] {command}")
        if not confirm_command(state, command):
            return _error_observation(emit, call.id, "error: command rejected by user")
    elif call.name in ("web_fetch", "web_search"):
        detail = call.arguments.get("url") or call.arguments.get("query") or str(call.arguments)
        if not confirm_tool_use(state, call.name, detail):
            return _error_observation(emit, call.id, "error: tool use rejected by user")
    elif call.name == "ask_user_question":
        options = call.arguments.get("options", [])
        labels = [str(o) for o in options] if isinstance(options, list) else []
        selected = prompt_single_choice(state, call.arguments.get("prompt", ""), labels)
        content = (
            "User skipped the question." if selected is None else f'User selected: "{selected}"'
        )
        emit(f"observation: {content}")
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": content,
            "is_error": False,
        }
    return None


def _format_call_args(args: dict[str, Any]) -> str:
    """trace 里的工具参数可能很大（file_write 的内容、后面 v6 exit_plan_mode 的整段计划）。
    长字符串只在 trace 里截断，完整参数仍照常传给工具。"""
    preview: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 80:
            preview[key] = value[:80] + "…"
        else:
            preview[key] = value
    return str(preview)


def execute_one_tool_call(
    call: ToolCall,
    ctx: ToolContext,
    state: RuntimeState,
    tools: ToolRegistry,
    emit: Callable[[str], None],
    print_output: PrintFunc,
) -> dict[str, Any]:
    """跑单个工具，返回一个 tool_result block。"""
    emit(f"tool_call: {call.name} {_format_call_args(call.arguments)}")

    request = PermissionRequest(
        tool_name=call.name,
        args=call.arguments,
        mode=state.permission_mode,
        cwd=ctx.cwd,
    )
    decision = decide_permission(request)

    if decision.behavior != "deny":
        pre = run_hooks("PreToolUse", call.name, call.arguments, ctx.cwd)
        blocked = [h for h in pre if not h["success"]]
        if blocked:
            msg = "\n".join(f"  [hook] {h['command']}: {h['output']}" for h in blocked)
            return _error_observation(
                emit, call.id, f"tool blocked by PreToolUse hook:\n{msg}"
            )

        if call.name == "exit_plan_mode":
            plan_summary = call.arguments.get("plan_summary", "")
            if not approve_plan(state, plan_summary):
                obs = "Plan not approved. Revise the plan and call exit_plan_mode again"
                emit(f"Observation: {obs}")  # 大写 O：这条要保持终端可见
                return _error_block(call.id, obs)

    # 文件写前置校验（file_write/file_edit；acceptEdits 也要过校验，只是后面跳过确认 UI）
    edit_preview: tuple[str, str, str] | None = None
    if call.name in ("file_write", "file_edit") and decision.behavior != "deny":
        edit_preview, preflight_err = _edit_preflight(call, ctx)
        if preflight_err is not None:
            return _error_observation(emit, call.id, preflight_err)

    # deny：直接返回 error，不弹 UI
    if decision.behavior == "deny":
        return _error_observation(emit, call.id, f"error: {decision.message}")

    # ask：按工具类型分发确认 UI（confirm_* 已在 1.5 包了 _ask，自动借回终端）
    if decision.behavior == "ask":
        block = _handle_ask(call, edit_preview, state, emit, print_output)
        if block is not None:
            return block

    # allow / ask 通过：执行
    result = tools.run(call, ctx)
    emit(f"observation: {result.content}")
    if not result.is_error:
        for h in run_hooks(
            "PostToolUse",
            call.name,
            call.arguments,
            ctx.cwd,
            tool_result=result.content,
        ):
            status = "ok" if h["success"] else f"warning: {h['output']}"
            print_output(f"[dim]hook: PostToolUse {call.name} {status}[/dim]")
    return {
        "type": "tool_result",
        "tool_use_id": result.tool_call_id,
        "content": result.content,
        "is_error": result.is_error,
    }


def partition_tool_calls(calls: list[ToolCall], tools: ToolRegistry) -> list[list[ToolCall]]:
    """连续只读工具合成并行组；写工具截断、自成串行组。
    例：[Read, Read, Write, Read] → [[Read, Read], [Write], [Read]]"""
    batches: list[list] = []
    current: list = []
    for call in calls:
        tool = tools.get(call.name)
        if tool is not None and tool.is_read_only:
            current.append(call)
        else:
            if current:  # 写工具前先收掉前面攒的只读组
                batches.append(current)
                current = []
            batches.append([call])  # 写/未知工具单独一组（未知 fail-closed 当串行）
    if current:
        batches.append(current)
    return batches


def execute_plan_boundary_calls(
    calls: list[ToolCall],
    ctx: ToolContext,
    state: RuntimeState,
    tools: ToolRegistry,
    emit: Callable[[str], None],
    print_output: PrintFunc,
) -> list[dict[str, Any]] | None:
    """plan 模式下，exit_plan_mode 是 turn boundary：同轮其它工具不执行。"""
    if state.permission_mode != "plan":
        return None
    exit_call = next((call for call in calls if call.name == "exit_plan_mode"), None)
    if exit_call is None:
        return None

    blocks: list[dict[str, Any]] = []
    for call in calls:
        if call is exit_call:
            blocks.append(execute_one_tool_call(call, ctx, state, tools, emit, print_output))
            continue
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": "Skipped because exit_plan_mode is waiting for approval. Re-issue this tool after approval if needed.",
                "is_error": True,
            }
        )
    return blocks
