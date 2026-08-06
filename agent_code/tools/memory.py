"""长期记忆工具：memory_write、memory_recall。"""

from __future__ import annotations

from typing import Any

from ..memdir.store import recall_memory, write_memory
from .core import Tool, ToolContext


def _memory_write(args: dict[str, Any], ctx: ToolContext) -> str:
    """写入一条长期记忆——工具函数只做薄包装。"""
    mem_type = args.get("type", "")
    title = args.get("title", "")
    body = args.get("body", "")
    if mem_type not in ("user", "feedback", "project", "reference"):
        return "error: type must be one of: user, feedback, project, reference"
    if not title:
        return "error: missing required argument 'title'"
    if not body:
        return "error: missing required argument 'body'"
    try:
        entry = write_memory(ctx.cwd, mem_type, title, body)
        return f"Memory saved: [{entry.mem_type}] {entry.title} -> {entry.file_path}"
    except Exception as exc:
        return f"error: {exc}"


def _memory_recall(args: dict[str, Any], ctx: ToolContext) -> str:
    """关键字搜索长期记忆——工具函数只做薄包装。"""
    query = args.get("query", "")
    top_k = int(args.get("top_k", 5))
    if not query:
        return "error: missing required argument 'query'"
    try:
        entries = recall_memory(ctx.cwd, query, top_k=top_k)
        if not entries:
            return "(no matching memories found)"
        lines = []
        for e in entries:
            snippet = e.body[:200] + ("..." if len(e.body) > 200 else "")
            lines.append(f"## [{e.mem_type}] {e.title}")
            lines.append(f"  file: {e.file_path}")
            lines.append(f"  {snippet}")
            lines.append("")
        return "\n".join(lines)
    except Exception as exc:
        return f"error: {exc}"


def tools() -> list[Tool]:
    """本模块工具的注册元数据——与实现就近存放。"""
    return [
        Tool(
            name="memory_write",
            description=(
                "Write a fact to long-term memory. Memories persist across sessions. "
                "Use for: user preferences, project conventions, feedback received, "
                "technical references to external systems."
            ),
            run=_memory_write,
            parameters={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference"],
                        "description": "Memory category.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title for this memory.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Full markdown content of the memory.",
                    },
                },
                "required": ["type", "title", "body"],
            },
        ),
        Tool(
            name="memory_recall",
            description=(
                "Search long-term memory by keywords. Returns matching entries with snippets. "
                "Use when you need to recall facts about the user, project, or past decisions."
            ),
            run=_memory_recall,
            is_read_only=True,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to search for.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results to return (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        ),
    ]
