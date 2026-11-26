"""Startup header: mascot, wordmark, key hints and cell-accurate clipping."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.cells import cell_len

from agent_code import __version__
from agent_code.output import OutputChunk
from agent_code.ui.banner import _BAO, build_banner

CWD = Path("/tmp/example-project")


def _banner(
    columns: int, monkeypatch: pytest.MonkeyPatch, base_url: str | None = None
) -> OutputChunk:
    """Build the banner at a pinned terminal width.

    ``shutil.get_terminal_size`` reads COLUMNS before touching the tty, so the env var
    pins the width even under pytest's captured stdout.
    """
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setenv("LINES", "24")
    return build_banner(CWD, "anthropic", "deepseek-v4-flash", base_url)


def test_banner_shows_logo_wordmark_and_startup_info(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _banner(120, monkeypatch).text
    for line in _BAO:
        assert line in text
    assert "agent-code" in text
    assert f"v{__version__}" in text
    assert "example-project" in text
    assert "anthropic/deepseek-v4-flash" in text
    assert "/help" in text
    assert "shift+tab" in text
    assert "base_url" not in text


@pytest.mark.parametrize("columns", [120, 100, 80, 60, 40, 20])
def test_header_lines_fit_the_terminal(columns: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """No row may exceed the terminal width (it would wrap into the transcript)."""
    text = _banner(columns, monkeypatch).text
    lines = text.splitlines()
    assert lines
    for line in lines:
        assert cell_len(line) <= columns


def test_base_url_is_listed_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _banner(120, monkeypatch, base_url="https://api.example.com").text
    assert "base_url https://api.example.com" in text


def test_long_path_is_middle_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "60")
    long_cwd = Path("/tmp") / ("d" * 200)
    chunk = build_banner(long_cwd, "anthropic", "deepseek-v4-flash", None)
    assert str(long_cwd) not in chunk.text
    assert "…" in chunk.text


def test_narrow_terminal_keeps_the_logo_and_clips_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _banner(20, monkeypatch).text
    lines = text.splitlines()
    assert any(line.strip() for line in lines)
    for line in _BAO:
        assert line in text
    assert "…" in text


def test_text_is_ansi_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """``text`` feeds transcripts and plain consumers; styling lives in fragments only."""
    chunk: OutputChunk = _banner(120, monkeypatch)
    assert "\x1b" not in chunk.text
    assert chunk.fragments
    assert "".join(piece for _, piece in chunk.fragments) == chunk.text
