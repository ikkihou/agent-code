"""Startup banner: bao logo, box alignment and clipping."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.cells import cell_len

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


def test_banner_shows_bao_and_startup_info(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _banner(120, monkeypatch).text
    for line in _BAO:
        assert line in text
    assert f"cwd: {CWD}" in text
    assert "provider: anthropic" in text
    assert "model: deepseek-v4-flash" in text
    assert "/help" in text
    assert "base_url" not in text


@pytest.mark.parametrize("columns", [120, 100, 80, 60, 40, 20])
def test_box_lines_are_rectangular(
    columns: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every box line must be the same width: a short one means a broken right border."""
    text = _banner(columns, monkeypatch).text
    lines = [line for line in text.splitlines() if line.startswith(("╭", "│", "╰"))]
    assert lines
    widths = {cell_len(line) for line in lines}
    assert len(widths) == 1
    assert widths.pop() <= columns


def test_base_url_is_listed_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _banner(120, monkeypatch, base_url="https://api.example.com").text
    assert "base_url: https://api.example.com" in text


def test_long_lines_are_clipped_inside_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "60")
    long_cwd = Path("/tmp") / ("d" * 200)
    chunk = build_banner(long_cwd, "anthropic", "deepseek-v4-flash", None)
    assert str(long_cwd) not in chunk.text
    assert "…" in chunk.text


def test_narrow_terminal_keeps_the_box_and_clips_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _banner(20, monkeypatch).text
    lines = text.splitlines()
    assert any(line.startswith("╭") for line in lines)
    assert any(line.startswith("╰") for line in lines)
    # Content is clipped, never wrapped: the box stays one line per row.
    assert len([line for line in lines if line.startswith("│")]) == 3
    assert "…" in text


def test_text_is_ansi_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """``text`` feeds transcripts and plain consumers; styling lives in fragments only."""
    chunk: OutputChunk = _banner(120, monkeypatch)
    assert "\x1b" not in chunk.text
    assert chunk.fragments
    assert "".join(piece for _, piece in chunk.fragments) == chunk.text
