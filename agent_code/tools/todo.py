"""任务清单工具：todo_write、todo_read。"""

from __future__ import annotations

from typing import Any

from ..runtime import TodoItem
from .core import Tool, ToolContext


def _render_todos(items: list[TodoItem]) -> str:
    icon = {"pending": "○", "in_progress": "◉", "completed": "✓"}
    return (
        "\n".join(f"  {icon.get(t.status, '?')} {t.content}" for t in items)
        or "(no todos)"
    )


def todo_write(args: dict[str, Any], ctx: ToolContext) -> str:
    state = ctx.state
    if state is None:
        return "error: no runtime state"
    items = [
        TodoItem(
            content=t.get("content", ""),
            status=t.get("status", "pending"),
            active_form=t.get("activeForm", ""),
        )
        for t in args.get("todos", [])
    ]
    state.todo_store = items

    lines = [_render_todos(items), "", "Todos updated"]
    completed = sum(1 for t in items if t.status == "completed")
    kws = ("test", "pytest", "verify", "lint", "check")
    has_verify = any(any(k in t.content.lower() for k in kws) for t in items)
    if completed >= 3 and not has_verify:
        lines.append(
            "提示：关掉了 3+ 个任务但没有验证步骤，建议先加一个测试/验证项再收尾。"
        )
    return "\n".join(lines)


def todo_read(args: dict[str, Any], ctx: ToolContext) -> str:
    state = ctx.state
    return _render_todos(state.todo_store) if state else "(no todos)"


def tools() -> list[Tool]:
    """本模块工具的注册元数据——与实现就近存放。"""
    return [
        Tool(
            name="todo_write",
            description=(
                "Create and manage a structured task list. Use for multi-step tasks (3+ steps). "
                "Keep exactly ONE item in_progress. Mark completed immediately when done. "
                "The todos array is a FULL replacement—always send the entire list."
            ),
            run=todo_write,
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "Imperative task name.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": "Present-continuous form.",
                                },
                            },
                            "required": ["content", "status", "activeForm"],
                        },
                    },
                },
                "required": ["todos"],
            },
            is_read_only=False,
        ),
        Tool(
            name="todo_read",
            description="Read the current todo list.",
            run=todo_read,
            parameters={"type": "object", "properties": {}, "required": []},
            is_read_only=True,
        ),
    ]
