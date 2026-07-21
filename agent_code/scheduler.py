from __future__ import annotations

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File      :   scheduler.py
Date      :   2026-07-21 11:46:44
Author    :   baoyihui
Contact   :   yihui.bao@apopgeei.com
"""

# here put the import lib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from typing import Any


class CronJob:
    """一个定时任务。id 是 12 位 hex，slash 是到点要重放的命令 /prompt"""

    def __init__(
        self,
        job_id: str,
        slash: str,
        every_seconds: int,
        label: str = "",
        last_run_at: str | None = None,
        created_at: str | None = None,
    ) -> None:

        self.id = job_id
        self.slash = slash
        self.every_seconds = every_seconds
        self.label = label
        self.last_run_at = last_run_at
        self.created_at = created_at


_CRON_FILE = ".agent/cron.json"


def _cron_path(cwd: Path) -> Path:
    """返回 .agent/cron.json 路径，自动创建 .agent/ 目录。"""
    agent_dir = cwd / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return agent_dir / "cron.json"


def _load_jobs(cwd: Path) -> list[CronJob]:
    """从 .agent/cron.json 加载持久化 job 列表。文件不存在或损坏返回 []。"""
    fpath = _cron_path(cwd)
    if not fpath.exists():
        return []
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    jobs: list[CronJob] = []
    for item in data.get("jobs", []):
        try:
            jobs.append(
                CronJob(
                    job_id=item["id"],
                    slash=item["slash"],
                    every_seconds=item["every_seconds"],
                    label=item.get("label", ""),
                    last_run_at=item.get("last_run_at"),
                    created_at=item.get("created_at"),
                )
            )
        except (KeyError, TypeError):
            continue  # 跳过损坏的 job，不让一条坏数据阻塞调度器
    return jobs


def _save_jobs(cwd: Path, jobs: list[CronJob]) -> None:
    """把当前 job 列表序列化到 .agent/cron.json。"""
    fpath = _cron_path(cwd)
    data = {
        "jobs": [
            {
                "id": j.id,
                "slash": j.slash,
                "every_seconds": j.every_seconds,
                "label": j.label,
                "last_run_at": j.last_run_at,
                "created_at": j.created_at,
            }
            for j in jobs
        ]
    }
    fpath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
