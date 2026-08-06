"""cron 调度器回归测试：drain_pending 的 `.empty()` bug 修复 + 到点 job 入队。"""

from __future__ import annotations

from agent_code.scheduler import CronScheduler


def test_drain_pending_returns_queued_prompts(tmp_path) -> None:
    sched = CronScheduler(tmp_path)
    sched._pending.put("prompt A")
    sched._pending.put("prompt B")

    # 修复前 `.empty` 访问绑定方法恒真 → 永远返回 []；修复后应原样取出。
    assert sched.drain_pending() == ["prompt A", "prompt B"]
    assert sched.drain_pending() == []


def test_check_due_enqueues_fireable_job(tmp_path) -> None:
    sched = CronScheduler(tmp_path)
    sched.add_job("/loop list", every_seconds=1)

    # 新 job last_run_at/created_at 均为 None → baseline 0 → 立即到点。
    sched._check_due()
    assert sched.drain_pending() == ["/loop list"]


def test_check_due_respects_interval(tmp_path) -> None:
    sched = CronScheduler(tmp_path)
    sched.add_job("prompt", every_seconds=3600)

    sched._check_due()
    assert sched.drain_pending() == ["prompt"]

    # 刚跑过，last_run_at 已更新 → 短期内不会再次到点。
    sched._check_due()
    assert sched.drain_pending() == []
