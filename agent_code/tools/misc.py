"""基础/杂项工具：echo、system_date、ask_user_question。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .core import Tool, ToolContext


def echo(args: dict[str, Any], ctx: ToolContext) -> str:
    return str(args.get("text", ""))


def system_date(args: dict[str, Any], ctx: ToolContext) -> str:
    # system_date 是模型看不到系统时钟时，需要向 harness 请求的能力。
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _ask_user_question(args: dict[str, Any], ctx: ToolContext) -> str:
    """由 agent.py 拦截块处理——工具函数本身不读 stdin。
    拦截块识别 call.name == "ask_user_question"，调 prompt_ui 后把结果作为 observation 返回。"""
    prompt = args.get("prompt", "")
    options = args.get("options", [])
    if not prompt:
        return "error: missing required argument 'prompt'"
    if not options or not isinstance(options, list):
        return "error: options must be a non-empty list"
    # 实际交互在 agent.py 拦截块里完成——这里只返回占位。
    # 正常路径不会走到这里，因为拦截块会先处理。
    return (
        "error: ask_user_question must be handled by the harness, not executed directly"
    )


def tools() -> list[Tool]:
    """本模块工具的注册元数据——与实现就近存放。"""
    return [
        Tool(
            name="echo",
            description="Return the input text.",
            run=echo,
            is_read_only=True,
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        Tool(
            name="system_date",
            description="Return the current system date and time.",
            run=system_date,
            is_read_only=True,
        ),
        Tool(
            name="ask_user_question",
            description=(
                "Ask the user a structured single-choice question. "
                "Use when you need to decide between multiple approaches "
                "or need user preference before proceeding."
            ),
            run=_ask_user_question,
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The question to ask the user. Should end with ?.",
                    },
                    "options": {
                        "type": "array",
                        "description": "List of option labels (2-4 recommended).",
                        "items": {"type": "string"},
                    },
                },
                "required": ["prompt", "options"],
            },
        ),
    ]
