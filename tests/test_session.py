from __future__ import annotations

import os
from pathlib import Path

from agent_code.output import OutputChunk, ThinkingItem, ToolCallItem
from agent_code.session import Session


def test_transcript_roundtrip(tmp_path: Path) -> None:
    session = Session.create(tmp_path)
    session.append_transcript(OutputChunk("first line\n"))
    session.append_transcript(OutputChunk("[bold]> hi[/bold]\n\n", format="rich"))
    # Prompt echoes carry style fragments; resume replay must get them back intact.
    styled = OutputChunk(
        "\n\n> styled\n",
        fragments=(("bold white on #444444", "> styled"),),
        full_width=True,
    )
    session.append_transcript(styled)

    chunks = session.transcript_chunks

    assert chunks == [
        OutputChunk("first line\n"),
        OutputChunk("[bold]> hi[/bold]\n\n", format="rich"),
        styled,
    ]


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


def test_structured_transcript_roundtrip(tmp_path: Path) -> None:
    """The sidecar keeps thinking/tool structure (and tool detail) across a resume."""
    session = Session.create(tmp_path)
    session.append_transcript(OutputChunk("text\n"))
    session.append_transcript(ThinkingItem("pondering"))
    tool = ToolCallItem(
        name="bash",
        args_preview='{"command": "ls"}',
        status="ok",
        args_full='{"command": "ls -la"}',
        result="a.txt",
    )
    session.append_transcript(tool)

    assert session.transcript_items == [OutputChunk("text\n"), ThinkingItem("pondering"), tool]
    # The flat view stays text-only for callers that only want plain chunks.
    assert session.transcript_chunks == [OutputChunk("text\n")]


def test_legacy_flat_transcript_record_reads_as_text(tmp_path: Path) -> None:
    """A record written before the structured sidecar (no ``kind``) is a text chunk."""
    session = Session.create(tmp_path)
    with session.transcript_path.open("a", encoding="utf-8") as f:
        f.write('{"text": "legacy\\n", "format": "plain"}\n')

    assert session.transcript_items == [OutputChunk("legacy\n")]
