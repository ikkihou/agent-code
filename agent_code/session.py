from __future__ import annotations
from pydantic import FilePath

#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   session.py
@Time    :   2026/07/18 17:12:29
@Author  :   Paul_Bao
@Version :   1.0
@Contact :   paulbao@mail.ecust.edu.cn
"""

"""session.py的功能：
1. 创建会话
2. 管理已有会话
3. 读/写消息历史
"""

# here put the import lib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sanitize_cwd(cwd: Path) -> str:
    path_str = str(cwd.resolve())
    sanitized = path_str.replace("/", "_").replace(":", "_").replace("\\", "_")
    return sanitized.lstrip("_")


def _session_dir(cwd: Path) -> Path:
    dir_path = cwd / ".agent" / "sessions" / _sanitize_cwd(cwd)
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


class Session:
    def __init__(
        self, cwd: Path, session_id: str, file_path: Path, resumed: bool = False
    ) -> None:
        self.cwd = cwd
        self.session_id = session_id
        self.file_path = file_path
        self.resumed = resumed

    @classmethod
    def create(cls, cwd: Path) -> "Session":
        sid = uuid.uuid4().hex[:12]
        file_path = _session_dir(cwd) / f"{sid}.jsonl"
        file_path.touch()
        return cls(cwd=cwd, session_id=sid, file_path=file_path, resumed=False)

    @classmethod
    def load_latest(cls, cwd: Path) -> "Session" | None:
        sessions_dir = _session_dir(cwd)
        jsonl_files = list(sessions_dir.glob("*.jsonl"))
        if not jsonl_files:
            return None
        latest = max(jsonl_files, key=lambda p: p.stat().st_mtime)
        sid = latest.stem
        return cls(cwd=cwd, session_id=sid, file_path=latest, resumed=True)

    @classmethod
    def load_by_id(cls, cwd: Path, session_id: str) -> "Session" | None:
        file_path = _session_dir(cwd) / f"{session_id}.jsonl"
        if not file_path.exists():
            return None
        return cls(cwd=cwd, session_id=session_id, file_path=file_path, resumed=True)

    @property
    def history(self) -> list[dict[str, Any]]:
        """解析jsonl文件，返回messages列表"""
        messages: list[dict[str, Any]] = []
        if not self.file_path.exists:
            return messages

        for line in self.file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            messages.append({"role": data["role"], "content": data["content"]})

        return messages

    def append_messages(self, msgs: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with open(self.file_path, "a", encoding="utf-8") as f:
            for msg in msgs:
                record = {
                    "role": msg["role"],
                    "content": msg["content"],
                    "timestamp": now,
                }
                f.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
