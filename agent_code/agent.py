"""
##
##       filename: agent.py
##        created: 2026/06/14
##         author: Paul_Bao
##            IDE: Neovim
##       Version : 1.0
##       Contact : paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from .compact_basic import compact
from .fs_safety import (
    SkipPolicy,
    apply_single_replace,
    check_mtime_conflict,
    ensure_read_before_edit,
    load_gitignore,
    resolve_in_cwd,
)
from .hooks import run_hooks
from .model import ModelProvider, ModelRequestAborted, ModelResponse, ToolResult
from .output import OutputWriter, render_console_chunk
from .permissions import PermissionRequest, decide_permission
from .project_memory import load_agent_md
from .prompt_ui import (
    confirm_command,
    confirm_edit,
    confirm_plan,
    confirm_tool_use,
    prompt_single_choice,
    render_diff,
)
from .runtime import RuntimeState
from .session import Session
from .tools import ToolContext, ToolRegistry

console = Console()
PrintFunc = Callable[..., None]


@dataclass
class AgentResult:
    final: str
    trace: list[str]
    messages: list[dict[str, Any]]


@dataclass
class _LineBufferedStreamRenderer:
    """Render complete lines so prompt-toolkit cannot overwrite partial chunks."""

    write_line: Callable[[str], None]
    pending: str = ""
    started: bool = False

    def feed(self, text: str) -> None:
        if not text:
            return

        self.started = True
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.write_line(line.removesuffix("\r"))

    def finish(self) -> None:
        if self.pending:
            self.write_line(self.pending)
            self.pending = ""


def _make_printer(output: OutputWriter | None = None) -> PrintFunc:
    if output is None:
        return console.print

    def print_to_output(*objects: Any, **kwargs: Any) -> None:
        styled = (
            kwargs.get("markup", True) is not False
            or kwargs.get("style") is not None
            or any(not isinstance(obj, str) for obj in objects)
        )
        output(render_console_chunk(*objects, styled=styled, **kwargs))

    return print_to_output


_SYSTEM_CORE = (
    "You are an AI coding agent running inside a CLI harness. "
    "You have access to tools for reading/writing files, running shell commands, "
    "searching the web, and asking the user questions. "
    "Use tools when needed; respond directly when you can."
)


def build_system_prompt(cwd: Path) -> str:
    """组装 system prompt：核心指南 + AGENT.md + MEMORY.md 索引。
    注入顺序：core prompt → 项目规则 → 跨 session 记忆索引。"""
    from .memdir.store import load_index as load_memory_index

    parts: list[str] = [_SYSTEM_CORE]
    agent_md = load_agent_md(cwd)
    if agent_md:
        parts.append(agent_md)

    memory_idx = load_memory_index(cwd)
    if memory_idx:
        parts.append(f"<project-memory>\n{memory_idx}\n</project-memory>")

    return "\n\n".join(parts)


# 把内部 ModelResponse 还原成 Anthropic Messages API 的 assistant content blocks格式
def _assistant_message(response: ModelResponse) -> dict[str, Any]:
    if response.assistant_content:
        return {"role": "assistant", "content": response.assistant_content}

    content: list[dict[str, Any]] = []
    if response.text:
        content.append({"type": "text", "text": response.text})
    for call in response.tool_calls or []:
        content.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    return {"role": "assistant", "content": content}


def run_agent(  ## AGENT LOOP
    prompt: str,
    provider: ModelProvider,
    tools: ToolRegistry,
    state: RuntimeState,
    max_steps: int = 8,
    cwd: Path | None = None,
    session: Session | None = None,
    system_prompt: str | None = None,
    output: OutputWriter | None = None,
) -> AgentResult:
    ## 解析 cwd
    resolved_cwd = cwd or Path.cwd()
    ## 构建工具上下文
    ctx = ToolContext(
        cwd=resolved_cwd,
        skip_policy=SkipPolicy.default(gitignore=load_gitignore(resolved_cwd)),
        state=state,
    )

    ## 加载历史对话
    if session and session.history:
        messages = list(session.history)
        messages.append({"role": "user", "content": prompt})
    else:
        messages = [{"role": "user", "content": prompt}]

    if session:
        session.append_messages([messages[-1]])

    print_output = _make_printer(output)

    def emit(line: str) -> None:
        # 工具结果可能很长：完整内容只通过 tool_result 回填给模型，终端只看工具调用/最终回答。
        if line.startswith("observation:"):
            return
        trace.append(line)
        print_output(line, markup=False, highlight=False)

    def interrupted_result() -> AgentResult:
        emit("interrupted by user")
        return AgentResult(final="interrupted", trace=trace, messages=messages)

    def write_stream_line(line: str) -> None:
        print_output(
            line,
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    trace: list[str] = []
    continuation_count = 0
    response: ModelResponse | None = None
    for step in range(max_steps):
        # 用户可能在工具执行期间按下 ESC。请求前检查可避免再多发一轮模型请求。
        if state.abort_event.is_set():
            return interrupted_result()

        ## 1. sending api request
        if len(messages) > 40:
            messages = compact(messages, keep=8)
            print_output(f"[dim]compacted: {len(messages)} messages remaining[/dim]")

        response = None
        stream_renderer = _LineBufferedStreamRenderer(write_stream_line)
        try:
            for event in provider.complete_stream(
                messages,
                tools.list(),
                system_prompt,
                signal=state.abort_event,
            ):
                if event.type == "text_delta" and event.text:
                    stream_renderer.feed(event.text)
                elif event.type == "completed":
                    response = event.response
        except ModelRequestAborted:
            stream_renderer.finish()
            return interrupted_result()

        stream_renderer.finish()
        streamed_text = stream_renderer.started
        if response is None:
            raise RuntimeError("provider stream ended without a completed response")

        # Cancellation wins the race with stream completion. In that case do not
        # persist the assistant response or execute any tool calls it contains.
        if state.abort_event.is_set():
            return interrupted_result()

        ## 2. add complete LLM response to history
        messages.append(_assistant_message(response))

        ## 3(1). tool call or end execution
        if not response.tool_calls:
            final = response.text or ""
            if state.permission_mode == "plan" and final.strip():
                if confirm_plan(final):
                    state.permission_mode = "acceptEdits"
                    messages.append({"role": "user", "content": "Plan approved. Implement it now."})
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Plan not approved. Revise the plan and present it again.",
                        }
                    )
                if session:
                    session.append_messages(messages[-2:])
                continue

            from .hooks import run_hooks_raw

            forced: str | None = None
            if continuation_count < 2:
                payload = {
                    "event": "Stop",
                    "final_text": final,
                    "cwd": str(ctx.cwd),
                    "continuation_count": continuation_count,
                }
                for h in run_hooks_raw("Stop", payload, ctx.cwd):
                    if not h["success"] and h["output"].strip():
                        forced = h["output"].strip()
                        break
            if forced is not None:
                continuation_count += 1
                emit(f"continue: {forced}")
                messages.append({"role": "user", "content": f"continue: {forced}"})
                if session:
                    session.append_messages(messages[-2:])
                continue  # 回到 loop 顶，再跑一轮
            if streamed_text:
                trace.append(f"final: {final}")
            else:
                emit(f"final: {final}")
            if session:
                session.append_messages([messages[-1]])
            return AgentResult(final=final, trace=trace, messages=messages)

        ## 3(2). execute all tool calls
        tool_result_blocks = execute_plan_boundary_calls(
            response.tool_calls,
            ctx,
            state,
            tools,
            emit,
            print_output,
        )
        if tool_result_blocks is None:
            tool_result_blocks = []
            for batch in partition_tool_calls(response.tool_calls, tools):
                if len(batch) == 1:
                    tool_result_blocks.append(
                        execute_one_tool_call(batch[0], ctx, state, tools, emit, print_output)
                    )
                else:
                    # 只读组并行。ex.map 按输入顺序返回结果，
                    # 所以 tool_result 顺序天然对齐 tool_use 顺序——这是必须守的协议约束。
                    with ThreadPoolExecutor(max_workers=4) as ex:
                        results = list(
                            ex.map(
                                lambda c: execute_one_tool_call(
                                    c, ctx, state, tools, emit, print_output
                                ),
                                batch,
                            )
                        )
                    tool_result_blocks.extend(results)

        messages.append({"role": "user", "content": tool_result_blocks})
        if session:
            session.append_messages(messages[-2:])

    if response is None:
        final = f"Agent reached max steps ({max_steps}) without making a request."
        emit(f"final: {final}")
        return AgentResult(final=final, trace=trace, messages=messages)

    final = f"Agent reached max steps ({max_steps}) without finishing. Last response: {response}"
    emit(f"final: {final}")
    return AgentResult(final=final, trace=trace, messages=messages)


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


def execute_one_tool_call(call, ctx, state, tools, emit, print_output) -> dict[str, Any]:
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
            obs = f"tool blocked by PreToolUse hook:\n{msg}"
            emit(f"observation: {obs}")
            return {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": obs,
                "is_error": True,
            }

        if call.name == "exit_plan_mode":
            plan_summary = call.arguments.get("plan_summary", "")
            if not confirm_plan(plan_summary):
                obs = "Plan not approved. Revise the plan and call exit_plan_mode again"
                emit(f"Observation: {obs}")
                return {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": obs,
                    "is_error": True,
                }
            state.permission_mode = "acceptEdits"

    # 文件写前置校验（file_write/file_edit；acceptEdits 也要过校验，只是后面跳过确认 UI）
    edit_preview: tuple[str, str, str] | None = None
    if call.name in ("file_write", "file_edit") and decision.behavior != "deny":
        path_str = call.arguments.get("file_path", "")
        if not path_str:
            r = ToolResult(
                call.id,
                "error: missing required argument 'file_path'",
                is_error=True,
            )
            emit(f"observation: {r.content}")
            return {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.content,
                "is_error": True,
            }
        try:
            path = resolve_in_cwd(ctx.cwd, path_str)
        except (ValueError, OSError) as exc:
            r = ToolResult(call.id, f"error: {exc}", is_error=True)
            emit(f"observation: {r.content}")
            return {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.content,
                "is_error": True,
            }
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
            r = ToolResult(call.id, validation_error, is_error=True)
            emit(f"observation: {r.content}")
            return {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.content,
                "is_error": True,
            }
        edit_preview = (path_str, old_content, new_content if new_content else "")

    # deny：直接返回 error，不弹 UI
    if decision.behavior == "deny":
        r = ToolResult(call.id, f"error: {decision.message}", is_error=True)
        emit(f"observation: {r.content}")
        return {
            "type": "tool_result",
            "tool_use_id": r.tool_call_id,
            "content": r.content,
            "is_error": True,
        }

    # ask：按工具类型分发确认 UI（confirm_* 已在 1.5 包了 _ask，自动借回终端）
    if decision.behavior == "ask":
        if call.name in ("file_write", "file_edit") and edit_preview is not None:
            path_str, old_content, new_content = edit_preview
            print_output(f"\n[bold]Diff for {path_str}:[/bold]")
            print_output(render_diff(old_content, new_content, path_str))
            if not confirm_edit(path_str):
                r = ToolResult(call.id, "error: edit rejected by user", is_error=True)
                emit(f"observation: {r.content}")
                return {
                    "type": "tool_result",
                    "tool_use_id": r.tool_call_id,
                    "content": r.content,
                    "is_error": True,
                }
        elif call.name == "bash":
            command = call.arguments.get("command", "")
            print_output(f"\n[bold yellow]Command:[/bold yellow] {command}")
            if not confirm_command(command):
                r = ToolResult(call.id, "error: command rejected by user", is_error=True)
                emit(f"observation: {r.content}")
                return {
                    "type": "tool_result",
                    "tool_use_id": r.tool_call_id,
                    "content": r.content,
                    "is_error": True,
                }
        elif call.name in ("web_fetch", "web_search"):
            detail = call.arguments.get("url") or call.arguments.get("query") or str(call.arguments)
            if not confirm_tool_use(call.name, detail):
                r = ToolResult(call.id, "error: tool use rejected by user", is_error=True)
                emit(f"observation: {r.content}")
                return {
                    "type": "tool_result",
                    "tool_use_id": r.tool_call_id,
                    "content": r.content,
                    "is_error": True,
                }
        elif call.name == "ask_user_question":
            options = call.arguments.get("options", [])
            labels = [str(o) for o in options] if isinstance(options, list) else []
            selected = prompt_single_choice(call.arguments.get("prompt", ""), labels)
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


def partition_tool_calls(calls, tools) -> list[list]:
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
    calls,
    ctx,
    state,
    tools,
    emit,
    print_output,
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
