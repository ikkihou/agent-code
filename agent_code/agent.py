#!/usr/bin/env python3
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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .model import ModelProvider, ModelResponse, ToolResult
from .tools import ToolRegistry, ToolContext
from .fs_safety import (
    SkipPolicy,
    load_gitignore,
    resolve_in_cwd,
    ensure_read_before_edit,
    apply_single_replace,
    check_mtime_conflict,
)

from rich.console import Console
from .prompt_ui import (
    confirm_command,
    confirm_edit,
    confirm_tool_use,
    render_diff,
    prompt_single_choice,
)
from .permissions import PermissionRequest, decide_permission
from .session import Session
from .project_memory import load_agent_md

console = Console()


@dataclass
class AgentResult:
    final: str
    trace: list[str]
    messages: list[dict[str, Any]]


_SYSTEM_CORE = (
    "You are an AI coding agent running inside a CLI harness. "
    "You have access to tools for reading/writing files, running shell commands, "
    "searching the web, and asking the user questions. "
    "Use tools when needed; respond directly when you can."
)


def build_system_prompt(cwd: Path) -> str:
    parts: list[str] = [_SYSTEM_CORE]
    agent_md = load_agent_md(cwd)
    if agent_md:
        parts.append(agent_md)
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


def run_agent(
    prompt: str,
    provider: ModelProvider,
    tools: ToolRegistry,
    max_steps: int = 8,
    cwd: Path | None = None,
    permission_mode: str = "default",  # 新增：default | acceptEdits | plan
    session: Session | None = None,
    system_prompt: str | None = None,
) -> AgentResult:
    resolved_cwd = cwd or Path.cwd()
    ctx = ToolContext(
        cwd=resolved_cwd,
        skip_policy=SkipPolicy.default(gitignore=load_gitignore(resolved_cwd)),
    )
    messages: list[dict[str, Any]] = []
    if session and session.history:
        messages = list(session.history)
        messages.append({"role": "user", "content": prompt})
    else:
        messages = [{"role": "user", "content": prompt}]

    if session:
        session.append_messages([messages[-1]])

    def emit(line: str) -> None:
        trace.append(line)
        console.print(line)

    trace: list[str] = []

    for step in range(max_steps):
        ## 1. sending api request
        response = provider.complete(messages, tools.list(), system_prompt)

        ## 2. add LLM response to history
        messages.append(_assistant_message(response))

        ## 3(1). tool call or end execution
        if not response.tool_calls:
            final = response.text or ""
            emit(f"final: {final}")
            if session:
                session.append_messages([messages[-1]])
            return AgentResult(final=final, trace=trace, messages=messages)

        ## 3(2). execute all tool calls
        tool_result_blocks: list[dict[str, Any]] = []
        for call in response.tool_calls:
            emit(f"tool_call: {call.name} {call.arguments}")

            # 权限引擎统一入口：所有工具调用先包装成 PermissionRequest
            request = PermissionRequest(
                tool_name=call.name,
                args=call.arguments,
                mode=permission_mode,
                cwd=ctx.cwd,
            )
            decision = decide_permission(request)

            edit_preview: tuple[str, str, str] | None = None
            if call.name in ("file_write", "file_edit") and decision.behavior != "deny":
                # acceptEdits 只跳过确认 UI，不能跳过 Day 4 的安全校验
                path_str = call.arguments.get("file_path", "")
                try:
                    path = resolve_in_cwd(ctx.cwd, path_str)
                except (ValueError, OSError) as exc:
                    result = ToolResult(call.id, f"error: {exc}", is_error=True)
                    emit(f"observation: {result.content}")
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": result.tool_call_id,
                            "content": result.content,
                            "is_error": True,
                        }
                    )
                    continue

                old_content = path.read_text(encoding="utf-8") if path.exists() else ""

                validation_error: str | None = None
                if call.name == "file_write":
                    if path.exists():
                        validation_error = ensure_read_before_edit(
                            ctx.read_state, path
                        ) or check_mtime_conflict(ctx.read_state, path)
                    new_content = call.arguments.get("content", "")
                else:  # file_edit
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
                    result = ToolResult(call.id, validation_error, is_error=True)
                    emit(f"observation: {result.content}")
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": result.tool_call_id,
                            "content": result.content,
                            "is_error": True,
                        }
                    )
                    continue

                # Ensure new_content is a str to satisfy expected type tuple[str, str, str]
                edit_preview = (
                    path_str,
                    old_content,
                    new_content if new_content is not None else "",
                )

            if decision.behavior == "deny":
                # deny 路径：直接返回 error observation，不弹 UI
                result = ToolResult(
                    call.id, f"error: {decision.message}", is_error=True
                )
                emit(f"observation: {result.content}")
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result.tool_call_id,
                        "content": result.content,
                        "is_error": True,
                    }
                )
                continue

            elif decision.behavior == "ask":
                # ask 路径：按工具类型分发不同的预览和确认 UI
                if call.name in ("file_write", "file_edit"):
                    # --- 文件编辑：安全校验已经做过；ask 模式只负责 diff + confirm ---
                    if edit_preview is not None:
                        path_str, old_content, new_content = edit_preview
                        diff_text = render_diff(old_content, new_content, path_str)
                        console.print(f"\n[bold]Diff for {path_str}:[/bold]")
                        console.print(diff_text)
                        if not confirm_edit(path_str):
                            result = ToolResult(
                                call.id, "error: edit rejected by user", is_error=True
                            )
                            emit(f"observation: {result.content}")
                            tool_result_blocks.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": result.tool_call_id,
                                    "content": result.content,
                                    "is_error": True,
                                }
                            )
                            continue

                elif call.name == "bash":
                    # --- bash：命令预览 + confirm ---
                    command = call.arguments.get("command", "")
                    timeout = call.arguments.get("timeout", 30)
                    console.print(f"\n[bold yellow]Command:[/bold yellow] {command}")
                    console.print(f"[dim]timeout: {timeout}s  cwd: {ctx.cwd}[/dim]")
                    if not confirm_command(command):
                        result = ToolResult(
                            call.id, "error: command rejected by user", is_error=True
                        )
                        emit(f"observation: {result.content}")
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": result.tool_call_id,
                                "content": result.content,
                                "is_error": True,
                            }
                        )
                        continue

                elif call.name in ("web_fetch", "web_search"):
                    # --- 网络工具：不写本地文件，但要让用户确认是否访问外部资源 ---
                    detail = (
                        call.arguments.get("url")
                        or call.arguments.get("query")
                        or str(call.arguments)
                    )
                    if not confirm_tool_use(call.name, detail):
                        result = ToolResult(
                            call.id, "error: tool use rejected by user", is_error=True
                        )
                        emit(f"observation: {result.content}")
                        tool_result_blocks.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": result.tool_call_id,
                                "content": result.content,
                                "is_error": True,
                            }
                        )
                        continue

                elif call.name == "ask_user_question":
                    question = call.arguments.get("prompt", "")
                    options = call.arguments.get("options", [])
                    if not isinstance(options, list):
                        options = []
                    labels = [str(o) for o in options]
                    selected = prompt_single_choice(question, labels)
                    if selected is None:
                        result = ToolResult(
                            call.id, "User skipped the question.", is_error=False
                        )
                    else:
                        result = ToolResult(
                            call.id, f'User selected: "{selected}"', is_error=False
                        )
                    emit(f"observation: {result.content}")
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": result.tool_call_id,
                            "content": result.content,
                            "is_error": result.is_error,
                        }
                    )
                    continue

            result = tools.run(call, ctx)
            emit(f"observation: {result.content}")
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": result.tool_call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
            )
        messages.append({"role": "user", "content": tool_result_blocks})
        if session:
            session.append_messages(messages[-2:])

    final = f"Agent reached max steps ({max_steps}) without finishing. Last response: {response}"
    emit(f"final: {final}")
    return AgentResult(final=final, trace=trace, messages=messages)
