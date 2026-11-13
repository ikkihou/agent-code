"""Tool call execution layer: permission decision -> confirm/intercept -> execute -> hooks
for a single tool call.

Split out of agent.py: this module owns execute_one_tool_call plus the plan-boundary
and parallel-grouping logic around it, leaving the run_agent main loop responsible
only for scheduling instead of inlining a special branch per tool.

Output contract: this module emits structured events only through ``Wire``
(ToolCallStarted / DiffPreview / CommandPreview) -- the tool layer only describes
"what happened", while coloring and line formatting belong entirely to the frontend.
The "observation" returned by a tool is no longer printed to the terminal; the full
content goes back to the model in a tool_result block.
"""

from __future__ import annotations

from typing import Any

from .fs_safety import (
    apply_single_replace,
    check_mtime_conflict,
    ensure_read_before_edit,
    read_text_file,
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
)
from .runtime import RuntimeState
from .tools import ToolContext, ToolRegistry
from .wire import CommandPreview, DiffPreview, Line, ToolCallCompleted, ToolCallStarted, Wire


def _error_block(tool_call_id: str, content: str) -> dict[str, Any]:
    """Build an error-flavored tool_result block."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_call_id,
        "content": content,
        "is_error": True,
    }


def approve_plan(state: RuntimeState, plan_text: str) -> bool:
    """Single entry point for plan approval: on confirmation, switch the mode to
    acceptEdits and return True."""
    if confirm_plan(state, plan_text):
        state.permission_mode = "acceptEdits"
        return True
    return False


def _edit_preflight(
    call: ToolCall, ctx: ToolContext
) -> tuple[tuple[str, str, str] | None, str | None]:
    """Read-before-edit validation plus diff preview construction for
    file_write/file_edit.

    Returns:
        (edit_preview, error_content): preview is (path_str, old_content, new_content);
        a non-None error_content means validation failed and the caller should return
        an error block directly.
    """
    path_str = call.arguments.get("file_path", "")
    if not path_str:
        return None, "error: missing required argument 'file_path'"
    try:
        path = resolve_in_cwd(ctx.cwd, path_str)
    except (ValueError, OSError) as exc:
        return None, f"error: {exc}"

    old_content = read_text_file(path)[0] if path.exists() else ""
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
    wire: Wire,
) -> dict[str, Any] | None:
    """Dispatch the confirmation UI by tool type in ask mode.

    Returns:
        A block ends the flow here (rejected or intercepted); None means confirmed and
        execution continues.
    """
    if call.name in ("file_write", "file_edit") and edit_preview is not None:
        path_str, old_content, new_content = edit_preview
        wire.send(DiffPreview(path=path_str, old=old_content, new=new_content))
        if not confirm_edit(state, path_str):
            return _error_block(call.id, "error: edit rejected by user")
    elif call.name == "bash":
        command = call.arguments.get("command", "")
        wire.send(CommandPreview(command=command))
        if not confirm_command(state, command):
            return _error_block(call.id, "error: command rejected by user")
    elif call.name in ("web_fetch", "web_search"):
        detail = call.arguments.get("url") or call.arguments.get("query") or str(call.arguments)
        if not confirm_tool_use(state, call.name, detail):
            return _error_block(call.id, "error: tool use rejected by user")
    elif call.name == "ask_user_question":
        options = call.arguments.get("options", [])
        labels = [str(o) for o in options] if isinstance(options, list) else []
        selected = prompt_single_choice(state, call.arguments.get("prompt", ""), labels)
        if selected is None:
            content = "User skipped the question."
        else:
            content = f'User selected: "{selected}"'
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": content,
            "is_error": False,
        }
    return None


def _format_call_args(args: dict[str, Any]) -> str:
    """Tool argument previews can be large (file_write content, or the whole plan text
    of v6's exit_plan_mode). Long strings are truncated in the display preview only;
    the full arguments still reach the tool."""
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
    wire: Wire,
) -> dict[str, Any]:
    """Run a single tool and return one tool_result block."""
    wire.send(
        ToolCallStarted(call_id=call.id, name=call.name, args_preview=_format_call_args(call.arguments))
    )

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
            return _error_block(call.id, f"tool blocked by PreToolUse hook:\n{msg}")

        if call.name == "exit_plan_mode":
            plan_summary = call.arguments.get("plan_summary", "")
            if not approve_plan(state, plan_summary):
                obs = "Plan not approved. Revise the plan and call exit_plan_mode again"
                wire.send(Line(f"plan rejected: {obs}"))
                return _error_block(call.id, obs)

    # File-write preflight (file_write/file_edit; acceptEdits must pass it too, it only
    # skips the confirmation UI afterwards).
    edit_preview: tuple[str, str, str] | None = None
    if call.name in ("file_write", "file_edit") and decision.behavior != "deny":
        edit_preview, preflight_err = _edit_preflight(call, ctx)
        if preflight_err is not None:
            return _error_block(call.id, preflight_err)

    # deny: return the error immediately, no UI.
    if decision.behavior == "deny":
        return _error_block(call.id, f"error: {decision.message}")

    # ask: dispatch the confirmation UI by tool type (the confirm_* helpers already wrap
    # _ask, which routes the question back to the terminal).
    if decision.behavior == "ask":
        block = _handle_ask(call, edit_preview, state, wire)
        if block is not None:
            return block

    # allow / ask approved: execute.
    result = tools.run(call, ctx)
    wire.send(ToolCallCompleted(call_id=call.id, name=call.name, is_error=result.is_error))
    if not result.is_error:
        for h in run_hooks(
            "PostToolUse",
            call.name,
            call.arguments,
            ctx.cwd,
            tool_result=result.content,
        ):
            status = "ok" if h["success"] else f"warning: {h['output']}"
            wire.send(Line(f"hook: PostToolUse {call.name} {status}", markup=True))
    return {
        "type": "tool_result",
        "tool_use_id": result.tool_call_id,
        "content": result.content,
        "is_error": result.is_error,
    }


def partition_tool_calls(calls: list[ToolCall], tools: ToolRegistry) -> list[list[ToolCall]]:
    """Group consecutive read-only tools into parallel batches; a write tool closes the
    current group and forms its own serial batch.
    Example: [Read, Read, Write, Read] -> [[Read, Read], [Write], [Read]]"""
    batches: list[list] = []
    current: list = []
    for call in calls:
        tool = tools.get(call.name)
        if tool is not None and tool.is_read_only:
            current.append(call)
        else:
            if current:  # Close the read-only group accumulated so far before the write.
                batches.append(current)
                current = []
            batches.append([call])  # Write/unknown tools get their own batch (unknown fail-closed, treated as serial)
    if current:
        batches.append(current)
    return batches


def execute_plan_boundary_calls(
    calls: list[ToolCall],
    ctx: ToolContext,
    state: RuntimeState,
    tools: ToolRegistry,
    wire: Wire,
) -> list[dict[str, Any]] | None:
    """In plan mode, exit_plan_mode is a turn boundary: no other tool in the same turn runs."""
    if state.permission_mode != "plan":
        return None
    exit_call = next((call for call in calls if call.name == "exit_plan_mode"), None)
    if exit_call is None:
        return None

    blocks: list[dict[str, Any]] = []
    for call in calls:
        if call is exit_call:
            blocks.append(execute_one_tool_call(call, ctx, state, tools, wire))
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
