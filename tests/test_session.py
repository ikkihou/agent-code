from __future__ import annotations

import json
import os
from pathlib import Path

from agent_code.output import OutputChunk
from agent_code.session import Session


def test_transcript_roundtrip(tmp_path: Path) -> None:
    session = Session.create(tmp_path)
    session.append_transcript(OutputChunk("first line\n"))
    session.append_transcript(OutputChunk("[bold]> hi[/bold]\n\n", format="rich"))
    # Prompt echoes carry style fragments; resume replay must get them back intact.
    styled = OutputChunk(
        "\n\n> styled\n",
        fragments=(("bold ansiwhite bg:#444444", "> styled"),),
        full_width=True,
    )
    session.append_transcript(styled)

    chunks = session.transcript_chunks

    assert chunks == [
        OutputChunk("first line\n"),
        OutputChunk("[bold]> hi[/bold]\n\n", format="rich"),
        styled,
    ]


def test_transcript_infers_full_width_for_legacy_prompt(tmp_path: Path) -> None:
    """Older transcript files predate full_width; infer it from the prompt background."""
    session = Session.create(tmp_path)
    legacy = {
        "text": "\n\n> old\n\n",
        "format": "plain",
        "fragments": [["ansiwhite bg:#444444", "> old"]],
    }
    with session.transcript_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(legacy) + "\n")

    chunk = session.transcript_chunks[0]
    assert chunk.full_width is True


def test_transcript_empty_when_missing(tmp_path: Path) -> None:
    session = Session.create(tmp_path)

    assert session.transcript_chunks == []


def test_transcript_skips_empty_and_bad_lines(tmp_path: Path) -> None:
    session = Session.create(tmp_path)
    session.append_transcript(OutputChunk(""))
    session.append_transcript(OutputChunk("kept\n"))
    # Hand-write a bad JSON line to verify reads skip it instead of raising.
    with session.transcript_path.open("a", encoding="utf-8") as f:
        f.write("{not valid json}\n")

    assert [c.text for c in session.transcript_chunks] == ["kept\n"]


def test_resumed_session_reads_existing_transcript(tmp_path: Path) -> None:
    created = Session.create(tmp_path)
    created.append_transcript(OutputChunk("hello\n"))

    resumed = Session.load_by_id(tmp_path, created.session_id)

    assert resumed is not None
    assert resumed.resumed is True
    assert resumed.transcript_chunks == [OutputChunk("hello\n")]


def test_load_latest_ignores_newer_transcript_sidecar(tmp_path: Path) -> None:
    """A transcript sidecar is not a session, even when its mtime is newer."""
    from agent_code.session import _session_summaries

    session = Session.create(tmp_path)
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    session.append_messages(messages)
    session.append_transcript(OutputChunk("\n\n> hello\n"))

    now = os.stat(session.transcript_path).st_mtime
    os.utime(session.file_path, (now - 10, now - 10))
    os.utime(session.transcript_path, (now + 10, now + 10))

    latest = Session.load_latest(tmp_path)

    assert latest is not None
    assert latest.session_id == session.session_id
    assert latest.file_path == session.file_path
    assert latest.history == messages
    assert [summary[0] for summary in _session_summaries(tmp_path)] == [session.session_id]


def test_history_skips_lines_without_role_or_content(tmp_path: Path) -> None:
    """A non-message record (or garbage line) in a session file must not break resume:
    history skips it instead of raising KeyError."""
    session = Session.create(tmp_path)
    with session.file_path.open("a", encoding="utf-8") as f:
        f.write('{"role": "user", "content": "hello"}\n')
        f.write('{"kind": "checkpoint", "data": 1}\n')  # no role/content
        f.write("not json at all\n")
        f.write('{"role": "assistant", "content": "hi"}\n')

    assert session.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_history_empty_when_file_missing(tmp_path: Path) -> None:
    """A deleted session file yields an empty history, not FileNotFoundError."""
    session = Session.create(tmp_path)
    session.file_path.unlink()

    assert session.history == []
