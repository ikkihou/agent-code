from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def backup(cwd: Path, path: Path, old_content: str) -> Path | None:
    """文件写入内容前，将器备份到.agent/history/<rel>/<ts>"""
    try:
        rel = path.resolve().relative_to(cwd.resolve())
    except ValueError:
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"
    backup_dir = cwd / ".agent" / "history" / rel
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / ts

    try:
        backup_path.write_text(old_content, encoding="utf-8")
    except OSError:
        return None

    return backup_path
