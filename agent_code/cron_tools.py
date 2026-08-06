from __future__ import annotations

from typing import Any

from .scheduler import CronScheduler
from .tools import ToolContext


def _get_scheduler(ctx: ToolContext) -> CronScheduler:
    """REPL 里复用运行中的 scheduler（挂 RuntimeState 上）；one-shot 临时读写 cron.json。"""
    if ctx.state is not None and ctx.state.scheduler is not None:
        return ctx.state.scheduler
    return CronScheduler(ctx.cwd)


def cron_create(args: dict[str, Any], ctx: ToolContext) -> str:
    """创建一条 cron job——工具函数只做薄包装。"""
    scheduler = _get_scheduler(ctx)
    slash = args.get("slash", "")
    every_seconds = int(args.get("every_seconds", 0))
    label = args.get("label", "")
    if not slash:
        return "error: missing required argument 'slash'"
    if every_seconds <= 0:
        return "error: every_seconds must be positive"
    job = scheduler.add_job(slash, every_seconds, label)
    return f"Cron job created: {job.id} — every {every_seconds}s: {slash}"


def cron_list(args: dict[str, Any], ctx: ToolContext) -> str:
    """列出当前所有 cron job。"""
    scheduler = _get_scheduler(ctx)
    jobs = scheduler.list_jobs()
    if not jobs:
        return "(no cron jobs)"
    lines = []
    for j in jobs:
        last = j.last_run_at or "never"
        label = f" — {j.label}" if j.label else ""
        lines.append(
            f"  [{j.id}] every {j.every_seconds}s: {j.slash}{label}  (last: {last})"
        )
    return "\n".join(lines)


def cron_cancel(args: dict[str, Any], ctx: ToolContext) -> str:
    """取消一条 cron job。"""
    scheduler = _get_scheduler(ctx)
    jid = args.get("id", "")
    if not jid:
        return "error: missing required argument 'id'"
    if scheduler.cancel_job(jid):
        return f"Cron job cancelled: {jid}"
    return f"error: job not found: {jid}"
