from __future__ import annotations

from pathlib import Path

from agent_code.output import OutputChunk
from agent_code.session import Session


def test_transcript_roundtrip(tmp_path: Path) -> None:
    session = Session.create(tmp_path)
    session.append_transcript(OutputChunk("first line\n"))
    session.append_transcript(OutputChunk("[bold]> hi[/bold]\n\n", format="rich"))

    chunks = session.transcript_chunks

    assert chunks == [
        OutputChunk("first line\n"),
        OutputChunk("[bold]> hi[/bold]\n\n", format="rich"),
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
