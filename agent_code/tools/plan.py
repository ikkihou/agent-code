"""计划模式工具：enter_plan_mode、exit_plan_mode。"""

from __future__ import annotations

from typing import Any

from .core import Tool, ToolContext


def enter_plan_mode(args: dict[str, Any], ctx: ToolContext) -> str:
    state = ctx.state
    if state is None:
        return "error: no runtime state"

    state.permission_mode = "plan"
    return (
        "Plan mode on. Draft a plan—write tools are denied. "
        "When the plan is ready, you MUST call exit_plan_mode(plan_summary). "
        "Do not ask for approval in final text."
    )


def exit_plan_mode(args: dict[str, Any], ctx: ToolContext) -> str:
    return "Plan approved. Write tools are now enabled."


def tools() -> list[Tool]:
    """本模块工具的注册元数据——与实现就近存放。"""
    return [
        Tool(
            name="enter_plan_mode",
            description=(
                "Enter plan mode: draft a plan before writing. Write tools are denied until approval. "
                "When the plan is ready, call exit_plan_mode(plan_summary). Do not ask for approval in final text."
            ),
            run=enter_plan_mode,
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="exit_plan_mode",
            description=(
                "Submit your plan for user approval. Use this when the plan is ready. "
                "Write tools unlock only after the user approves."
            ),
            run=exit_plan_mode,
            parameters={
                "type": "object",
                "properties": {
                    "plan_summary": {
                        "type": "string",
                        "description": "The plan to review.",
                    }
                },
                "required": ["plan_summary"],
            },
        ),
    ]
