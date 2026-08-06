"""进程/shell 工具：bash、git_status、git_diff。"""

from __future__ import annotations

from typing import Any

from ..bash_runner import run_sync as _bash_run_sync
from ..bg_manager import start_background
from .core import Tool, ToolContext


def _git_status(args: dict[str, Any], ctx: ToolContext) -> str:
    """薄包装 git status——只读、默认 allow。"""
    return _bash_run_sync("git status", ctx.cwd, timeout=10)


def _git_diff(args: dict[str, Any], ctx: ToolContext) -> str:
    """薄包装 git diff——只读、默认 allow。"""
    return _bash_run_sync("git diff", ctx.cwd, timeout=10)


def bash(args: dict[str, Any], ctx: ToolContext) -> str:
    """执行 shell 命令。前置校验和用户确认在 agent.py 拦截块完成。"""
    command = args.get("command", "")
    if not command:
        return "error: missing required argument 'command'"
    timeout = int(args.get("timeout", 30))
    background = bool(args.get("background", False))

    if background:
        result = start_background(command, ctx.cwd)
        return (
            f"Command running in background with ID: {result['background_id']}.\n"
            f"Output is being written to: {result['output_file']}\n"
            f"Stderr is being written to: {result['stderr_file']}\n"
            f"PID: {result['pid']}\n\n"
            f"{result['message']}"
        )

    return _bash_run_sync(command, ctx.cwd, timeout=timeout)


def tools() -> list[Tool]:
    """本模块工具的注册元数据——与实现就近存放。"""
    return [
        Tool(
            name="git_status",
            description="Run git status to see the current state of the working directory.",
            run=_git_status,
            is_read_only=True,
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="git_diff",
            description="Run git diff to see unstaged changes in the working directory.",
            run=_git_diff,
            is_read_only=True,
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="bash",
            description=(
                "Execute a shell command. Working directory persists but shell state "
                "does not (each call is a fresh shell). timeout in seconds (default 30). "
                "Avoid cd; use the tool's implicit cwd instead."
            ),
            run=bash,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds, default 30.",
                        "default": 30,
                    },
                    "background": {
                        "type": "boolean",
                        "description": "Run in background. Returns immediately with a background_id. Default false.",
                        "default": False,
                    },
                },
                "required": ["command"],
            },
        ),
    ]
