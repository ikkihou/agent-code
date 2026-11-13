"""cron scheduler regression tests: the drain_pending `.empty()` bug fix plus queuing of
due jobs."""

from __future__ import annotations

from agent_code.scheduler import CronScheduler


def test_drain_pending_returns_queued_prompts(tmp_path) -> None:
    sched = CronScheduler(tmp_path)
    sched._pending.put("prompt A")
    sched._pending.put("prompt B")

    # Before the fix, accessing `.empty` (a bound method) was always truthy -> always [];
    # after the fix the prompts come out as-is.
    assert sched.drain_pending() == ["prompt A", "prompt B"]
    assert sched.drain_pending() == []


def test_check_due_enqueues_fireable_job(tmp_path) -> None:
    sched = CronScheduler(tmp_path)
    sched.add_job("/loop list", every_seconds=1)

    # A new job has last_run_at/created_at == None -> baseline 0 -> due immediately.
    sched._check_due()
    assert sched.drain_pending() == ["/loop list"]


def test_check_due_respects_interval(tmp_path) -> None:
    sched = CronScheduler(tmp_path)
    sched.add_job("prompt", every_seconds=3600)

    sched._check_due()
    assert sched.drain_pending() == ["prompt"]

    # Just ran, last_run_at was updated -> not due again any time soon.
    sched._check_due()
    assert sched.drain_pending() == []
