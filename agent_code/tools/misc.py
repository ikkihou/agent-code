"""Basic and miscellaneous tools: echo, system_date, ask_user_question."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .core import Tool, ToolContext


def echo(args: dict[str, Any], ctx: ToolContext) -> str:
    return str(args.get("text", ""))


def system_date(args: dict[str, Any], ctx: ToolContext) -> str:
    # system_date exists because the model cannot see the system clock and has to ask
    # the harness for it.
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _ask_user_question(args: dict[str, Any], ctx: ToolContext) -> str:
    """Handled by agent.py's intercept block -- the tool function itself never reads stdin.
    The intercept block matches call.name == "ask_user_question", calls prompt_ui, and
    feeds the answer back as the observation."""
    prompt = args.get("prompt", "")
    options = args.get("options", [])
    if not prompt:
        return "error: missing required argument 'prompt'"
    if not options or not isinstance(options, list):
        return "error: options must be a non-empty list"
    # The real interaction happens in agent.py's intercept block -- this is only a
    # placeholder. The normal path never reaches here because the intercept block
    # handles the call first.
    return (
        "error: ask_user_question must be handled by the harness, not executed directly"
    )


def tools() -> list[Tool]:
    """Registration metadata for this module's tools -- kept next to the implementation."""
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
