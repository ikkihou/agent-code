"""
@File    :   session.py
@Time    :   2026/07/18 17:12:29
@Author  :   Paul_Bao
@Version :   1.0
@Contact :   paulbao@mail.ecust.edu.cn

Session persistence.

What session.py does:
1. Create a session
2. Manage existing sessions
3. Read/write the message history
"""

# here put the import lib
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.table import Table

from .output import OutputChunk

SessionRenderable = str | Table


def _session_summaries(cwd: Path) -> list[tuple[str, int, float]]:
    """Return (session_id, user/assistant message count, file mtime) per session."""
    sessions_dir = _session_dir(cwd)
    summaries: list[tuple[str, int, float]] = []

    if not sessions_dir.is_dir():
        return summaries

    for file_path in sessions_dir.rglob("*.jsonl"):
        message_count = 0
        try:
            for line in file_path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("role") in {
                    "user",
                    "assistant",
                }:
                    message_count += 1
            modified_at = file_path.stat().st_mtime
        except (OSError, UnicodeError):
            continue

        summaries.append((file_path.stem, message_count, modified_at))

    return sorted(summaries, key=lambda item: item[2], reverse=True)


def _sessions_renderable(cwd: Path) -> SessionRenderable:
    summaries = _session_summaries(cwd)
    if not summaries:
        return "[dim]No sessions found.[/dim]"

    table = Table(title="Sessions")
    table.add_column("session_id")
    table.add_column("messages", justify="right")
    table.add_column("last updated")
    for session_id, message_count, modified_at in summaries:
        updated = (
            datetime.fromtimestamp(modified_at)
            .astimezone()
            .isoformat(timespec="seconds")
        )
        table.add_row(session_id, str(message_count), updated)
    return table


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
        """Parse the jsonl file and return the list of messages."""
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

    @property
    def transcript_path(self) -> Path:
        """Path of the rendered transcript sidecar file: <sid>.transcript.jsonl, in the
        same directory as the session file.

        Uses with_name rather than with_suffix -- multi-part suffixes raise
        ValueError under Python 3.12's with_suffix.
        """
        return self.file_path.with_name(self.file_path.stem + ".transcript.jsonl")

    def append_transcript(self, chunk: OutputChunk) -> None:
        """Append one OutputChunk already rendered to the output panel to the transcript
        file.

        Non-empty fragments are persisted too, so a resume replay can restore the
        original styling (grey prompt blocks and so on); format "rich" stores the raw
        markup text.
        """
        if not chunk.text:
            return
        record: dict[str, Any] = {"text": chunk.text, "format": chunk.format}
        if chunk.fragments:
            record["fragments"] = [[style, text] for style, text in chunk.fragments]
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )

    @property
    def transcript_chunks(self) -> list[OutputChunk]:
        """Read back the historical render transcript, used to rebuild the output panel
        on resume. Returns an empty list when the file is missing."""
        chunks: list[OutputChunk] = []
        if not self.transcript_path.exists():
            return chunks
        for line in self.transcript_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            fragments = data.get("fragments")
            chunks.append(
                OutputChunk(
                    data.get("text", ""),
                    format=data.get("format", "plain"),
                    fragments=(
                        tuple((str(style), str(text)) for style, text in fragments)
                        if fragments
                        else ()
                    ),
                )
            )
        return chunks
