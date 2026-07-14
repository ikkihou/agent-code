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
from typing import Any

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
from .diff_ui import confirm_edit, render_diff

console = Console()


@dataclass
class AgentResult:
    final: str
    trace: list[str]
    messages: list[dict[str, Any]]


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
) -> AgentResult:
    resolved_cwd = cwd or Path.cwd()
    ctx = ToolContext(
        cwd=resolved_cwd,
        skip_policy=SkipPolicy.default(gitignore=load_gitignore(resolved_cwd)),
    )
    messsages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    def emit(line: str) -> None:
        trace.append(line)
        console.print(line)

    trace: list[str] = []

    for step in range(max_steps):
        ## 1. sending api request
        response = provider.complete(messsages, tools=tools.list())

        ## 2. add LLM response to history
        messsages.append(_assistant_message(response))

        ## 3(1). tool call or end execution
        if not response.tool_calls:
            final = response.text or ""
            emit(f"final: {final}")
            return AgentResult(final=final, trace=trace, messages=messsages)

        ## 3(2). execute all tool calls
        tool_result_blocks: list[dict[str, Any]] = []
        for call in response.tool_calls:
            emit(f"tool_call: {call.name} {call.arguments}")

            # file_write / file_edit 的harness拦截:先做前置校验,再渲染diff,最后让用户确认
            if call.name in ("file_write", "file_edit"):
                path_str = call.arguments.get("file_path", "")

                try:
                    path = resolve_in_cwd(ctx.cwd, path_str)
                except (ValueError, OSError) as e:
                    result = ToolResult(call.id, f"error: {e}", is_error=True)
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

                old_content = path.read_text(encoding="utf-8") if path.exists() else ""

                ## (1) 前置校验
                validation_error: str | None = None
                if call.name == "file_write":
                    ## file_write: 新建文件直接放行，覆盖已有文件需先读且无 mtime 冲突
                    if path.exists():
                        if path not in ctx.read_state.entries:
                            validation_error = f"error: file has not been read yet. Read {path_str} first before editing."
                        else:
                            validation_error = check_mtime_conflict(
                                ctx.read_state, path
                            )
                elif call.name == "file_edit":
                    if not path.exists():
                        validation_error = f"error: file does not exist: {path_str}"
                    else:
                        validation_error = ensure_read_before_edit(
                            ctx.read_state, path
                        ) or check_mtime_conflict(ctx.read_state, path)

                ## (2) 计算new content
                new_content: str | None = None
                if call.name == "file_write":
                    new_content = call.arguments.get("content", "")
                elif call.name == "file_edit" and validation_error is None:
                    new_content, replace_err = apply_single_replace(
                        old_content,
                        call.arguments.get("old_string", ""),
                        call.arguments.get("new_string", ""),
                        bool(call.arguments.get("replace_all", False)),
                    )
                    if replace_err is not None:
                        validation_error = replace_err

                ## (3) 校验失败：不渲染 diff、不问用户，直接 error observation
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

                ## (4) 校验通过：渲染 diff + 用户确认
                if new_content is not None:
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
        messsages.append({"role": "user", "content": tool_result_blocks})

    final = f"Agent reached max steps ({max_steps}) without finishing. Last response: {response}"
    emit(f"final: {final}")
    return AgentResult(final=final, trace=trace, messages=messsages)
