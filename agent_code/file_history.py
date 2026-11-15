from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path


def backup(cwd: Path, path: Path, old_content: str) -> Path | None:
    """Back up the pre-write content of a file to .agent/history/<rel>/<ts>."""
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


def _history_dir(cwd: Path, rel_path: Path) -> Path:
    return cwd / ".agent" / "history" / rel_path


def latest_backup(cwd: Path, rel_path: Path) -> Path | None:
    """Return the newest backup for ``rel_path`` (relative to cwd), or None.

    Backup filenames are fixed-width UTC timestamps (``YYYYMMDDTHHMMSS.mmmZ``), so the
    lexicographic maximum is also the newest.
    """
    backup_dir = _history_dir(cwd, rel_path)
    if not backup_dir.is_dir():
        return None
    backups = [p for p in backup_dir.iterdir() if p.is_file()]
    if not backups:
        return None
    return max(backups, key=lambda p: p.name)


def restore(cwd: Path, rel_path: Path) -> Path | None:
    """Restore the newest backup of ``rel_path`` over the file; return the backup path.

    Returns None when there is no backup (or the target would escape cwd).
    """
    backup_path = latest_backup(cwd, rel_path)
    if backup_path is None:
        return None

    target = (cwd / rel_path).resolve()
    try:
        target.relative_to(cwd.resolve())
    except ValueError:
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(backup_path, target)
    return backup_path
