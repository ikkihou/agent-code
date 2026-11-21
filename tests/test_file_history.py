"""Tests for the file-history backup/restore path and the /undo slash command."""

from __future__ import annotations

from pathlib import Path

from agent_code.file_history import backup, latest_backup, restore
from agent_code.slash import SlashContext, dispatch_slash


def _history_dir(tmp_path: Path, rel: str) -> Path:
    return tmp_path / ".agent" / "history" / rel


def test_latest_backup_returns_newest(tmp_path: Path) -> None:
    d = _history_dir(tmp_path, "sub/f.txt")
    d.mkdir(parents=True)
    (d / "20260101T000000.000Z").write_text("old", encoding="utf-8")
    (d / "20260102T000000.000Z").write_text("newer", encoding="utf-8")

    newest = latest_backup(tmp_path, Path("sub/f.txt"))
    assert newest is not None
    assert newest.name == "20260102T000000.000Z"


def test_latest_backup_returns_none_without_history(tmp_path: Path) -> None:
    assert latest_backup(tmp_path, Path("missing.txt")) is None


def test_restore_writes_backup_content_over_file(tmp_path: Path) -> None:
    d = _history_dir(tmp_path, "sub/f.txt")
    d.mkdir(parents=True)
    (d / "20260102T000000.000Z").write_text("restored content", encoding="utf-8")

    target = tmp_path / "sub" / "f.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("current content", encoding="utf-8")

    restored = restore(tmp_path, Path("sub/f.txt"))

    assert restored is not None
    assert target.read_text(encoding="utf-8") == "restored content"


def test_restore_returns_none_without_backup(tmp_path: Path) -> None:
    assert restore(tmp_path, Path("nope.txt")) is None


def _ctx(tmp_path: Path) -> SlashContext:
    return SlashContext(
        cwd=tmp_path,
        permission_mode="default",
        model="mock",
        provider="mock",
        session_id=None,
    )


def test_undo_slash_restores_newest_backup(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    backup(tmp_path, p, "original")
    p.write_text("changed", encoding="utf-8")

    result = dispatch_slash("/undo a.txt", _ctx(tmp_path))

    assert result.handled is True
    assert "restored a.txt" in result.message
    assert p.read_text(encoding="utf-8") == "original"


def test_undo_slash_reports_no_backup(tmp_path: Path) -> None:
    result = dispatch_slash("/undo a.txt", _ctx(tmp_path))
    assert result.handled is True
    assert "no backup found" in result.message


def test_undo_slash_prints_usage_without_args(tmp_path: Path) -> None:
    result = dispatch_slash("/undo", _ctx(tmp_path))
    assert result.handled is True
    assert "Usage: /undo <path>" in result.message


def test_undo_slash_rejects_path_escaping_cwd(tmp_path: Path) -> None:
    result = dispatch_slash("/undo ../secret.txt", _ctx(tmp_path))
    assert result.handled is True
    assert result.message.startswith("error:")
