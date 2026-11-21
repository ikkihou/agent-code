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
from io import StringIO
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .output import OutputChunk


def _is_session_file(path: Path) -> bool:
    """True for a real session file, false for a rendered transcript sidecar.

    The transcript is named ``<session_id>.transcript.jsonl`` and lives next to
    ``<session_id>.jsonl``; a bare ``*.jsonl`` glob would treat it as a session and
    ``Session.history`` would fail on its missing ``role`` field.
    """
    return not path.name.endswith(".transcript.jsonl")


def _session_summaries(cwd: Path) -> list[tuple[str, int, float]]:
    """Return (session_id, user/assistant message count, file mtime) per session."""
    sessions_dir = _session_dir(cwd)
    summaries: list[tuple[str, int, float]] = []

    if not sessions_dir.is_dir():
        return summaries

    for file_path in sessions_dir.rglob("*.jsonl"):
        if not _is_session_file(file_path):
            continue
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
        except OSError, UnicodeError:
            continue

        summaries.append((file_path.stem, message_count, modified_at))

    return sorted(summaries, key=lambda item: item[2], reverse=True)


def _render_table_text(table: Table, width: int = 100) -> str:
    """Render a Rich table to plain text (no ANSI).

    Slash results cross the wire as *text*: the REPL sink only stores strings
    (``str(result.message)``), so returning the ``Table`` object itself printed
    ``<rich.table.Table object at 0x...>``. Lowering it to text here keeps one
    representation both frontends (one-shot console / REPL scrollback) can show.
    """
    console = Console(file=StringIO(), record=True, width=width, color_system=None)
    console.print(table)
    return "\n".join(line.rstrip() for line in console.export_text().splitlines())


def _sessions_text(cwd: Path) -> str:
    """Session list as Rich markup text: a rendered table, or a dim notice."""
    summaries = _session_summaries(cwd)
    if not summaries:
        return "[dim]No sessions found.[/dim]"

    table = Table(title="Sessions")
    table.add_column("session_id")
    table.add_column("messages", justify="right")
    table.add_column("last updated")
    for session_id, message_count, modified_at in summaries:
        updated = datetime.fromtimestamp(modified_at).astimezone().isoformat(timespec="seconds")
        table.add_row(session_id, str(message_count), updated)
    return _render_table_text(table)


def _sanitize_cwd(cwd: Path) -> str:
    path_str = str(cwd.resolve())
    sanitized = path_str.replace("/", "_").replace(":", "_").replace("\\", "_")
    return sanitized.lstrip("_")


def _session_dir(cwd: Path) -> Path:
    dir_path = cwd / ".agent" / "sessions" / _sanitize_cwd(cwd)
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


class Session:
    def __init__(self, cwd: Path, session_id: str, file_path: Path, resumed: bool = False) -> None:
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
    def load_latest(cls, cwd: Path) -> "Session | None":
        sessions_dir = _session_dir(cwd)
        jsonl_files = [path for path in sessions_dir.glob("*.jsonl") if _is_session_file(path)]
        if not jsonl_files:
            return None
        latest = max(jsonl_files, key=lambda p: p.stat().st_mtime)
        sid = latest.stem
        return cls(cwd=cwd, session_id=sid, file_path=latest, resumed=True)

    @classmethod
    def load_by_id(cls, cwd: Path, session_id: str) -> "Session | None":
        file_path = _session_dir(cwd) / f"{session_id}.jsonl"
        if not file_path.exists():
            return None
        return cls(cwd=cwd, session_id=session_id, file_path=file_path, resumed=True)

    @property
    def history(self) -> list[dict[str, Any]]:
        """Parse the jsonl file and return the list of messages.

        Lines without a usable ``role``/``content`` are skipped rather than raising:
        a stray non-message record in the file must not break resume.
        """
        messages: list[dict[str, Any]] = []
        if not self.file_path.exists():
            return messages

        for line in self.file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict) or "role" not in data or "content" not in data:
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
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

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
        if chunk.full_width:
            # Persist the intent, not the padded line: resume uses the current terminal
            # width, and transcript files stay independent of one terminal's columns.
            record["full_width"] = True
        with open(self.transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

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
            full_width = bool(data.get("full_width", False))
            if not full_width and fragments:
                # Transcripts written before the flag existed used this background for
                # submitted prompts; infer it so older files also replay as full-width.
                # Current records spell it in Rich vocabulary ("on #444444"), older
                # ones in prompt_toolkit vocabulary ("bg:#444444").
                full_width = any(
                    "bg:#444444" in str(style) or "on #444444" in str(style)
                    for style, _ in fragments
                )
            chunks.append(
                OutputChunk(
                    data.get("text", ""),
                    format=data.get("format", "plain"),
                    fragments=(
                        tuple((str(style), str(text)) for style, text in fragments)
                        if fragments
                        else ()
                    ),
                    full_width=full_width,
                )
            )
        return chunks
