"""Aggregate registration entry point: default_tools() assembles every feature
sub-module plus the cron tools."""

from __future__ import annotations

from . import edit, memory, misc, plan, read, shell, todo
from .core import Tool, ToolRegistry


def default_tools() -> ToolRegistry:
    # The cron tools stay lazily imported: cron_tools depends on this package's
    # ToolContext, and importing inside the function body is what breaks that
    # cycle (same approach as the pre-refactor tools.py).
    from ..cron_tools import cron_cancel, cron_create, cron_list

    registry = ToolRegistry()
    for tool in (
        *read.tools(),
        *edit.tools(),
        *shell.tools(),
        *memory.tools(),
        *todo.tools(),
        *plan.tools(),
        *misc.tools(),
    ):
        registry.register(tool)
    registry.register(
        Tool(
            name="cron_create",
            description=(
                "Create a recurring cron job. The job will re-run the given slash/prompt "
                "every N seconds. Use for periodic checks like PR status polling."
            ),
            run=cron_create,
            parameters={
                "type": "object",
                "properties": {
                    "slash": {
                        "type": "string",
                        "description": "Slash command or prompt to run.",
                    },
                    "every_seconds": {
                        "type": "integer",
                        "description": "Interval in seconds.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional human-readable label.",
                    },
                },
                "required": ["slash", "every_seconds"],
            },
        )
    )
    registry.register(
        Tool(
            name="cron_list",
            description="List all active cron jobs with their IDs, intervals, and last-run times.",
            run=cron_list,
            is_read_only=True,
            parameters={"type": "object", "properties": {}, "required": []},
        )
    )
    registry.register(
        Tool(
            name="cron_cancel",
            description="Cancel a cron job by its ID.",
            run=cron_cancel,
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Cron job ID to cancel."},
                },
                "required": ["id"],
            },
        )
    )
    return registry
