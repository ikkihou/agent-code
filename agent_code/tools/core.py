"""Tools package infrastructure: shared types such as Tool / ToolContext / ToolRegistry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..fs_safety import ReadFileState, SkipPolicy
from ..model import ToolCall, ToolResult
from ..runtime import RuntimeState


@dataclass
class ToolContext:
    cwd: Path
    skip_policy: SkipPolicy = field(default_factory=SkipPolicy.default)
    read_state: ReadFileState = field(default_factory=ReadFileState)
    state: RuntimeState | None = None


ToolFunc = Callable[[dict[str, Any], ToolContext], str]


@dataclass
class Tool:
    name: str
    description: str
    run: ToolFunc
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )
    is_read_only: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        # The registry is the harness boundary between a tool name and its Python fn.
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def run(self, call: ToolCall, context: ToolContext) -> ToolResult:
        # Unknown tools also return an observation so the agent loop never crashes.
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"unknown tool: {call.name}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id, content=tool.run(call.arguments, context)
        )

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)
