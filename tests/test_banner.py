"""Startup banner: logo size selection, box alignment and clipping."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.cells import cell_len

from agent_code.output import OutputChunk
from agent_code.ui.banner import _BOX_MIN_COLUMNS, _LOGO_COMPACT, _LOGO_WIDE, build_banner

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


def test_wide_logo_on_a_wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _banner(120, monkeypatch).text
    for line in _LOGO_WIDE:
        assert line in text


def test_narrow_terminal_falls_back_to_compact_logo(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _banner(60, monkeypatch).text
    for line in _LOGO_COMPACT:
        assert line in text
    assert _LOGO_WIDE[0] not in text


@pytest.mark.parametrize("columns", [120, 100, 80, 60, 50])
def test_box_lines_are_rectangular(columns: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every box line must be the same width: a short one means a broken right border."""
    text = _banner(columns, monkeypatch).text
    lines = [line for line in text.splitlines() if line.startswith(("╭", "│", "╰"))]
    assert lines
    widths = {cell_len(line) for line in lines}
    assert len(widths) == 1
    assert widths.pop() <= columns


def test_box_holds_provider_model_and_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _banner(120, monkeypatch).text
    assert "Welcome to Agent Code!" in text
    assert f"cwd: {CWD}" in text
    assert "provider: anthropic" in text
    assert "model: deepseek-v4-flash" in text
    assert "/help" in text
    assert "base_url" not in text


def test_base_url_is_listed_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _banner(120, monkeypatch, base_url="https://api.example.com").text
    assert "base_url: https://api.example.com" in text


def test_long_lines_are_clipped_inside_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "60")
    long_cwd = Path("/tmp") / ("d" * 200)
    chunk = build_banner(long_cwd, "anthropic", "deepseek-v4-flash", None)
    assert str(long_cwd) not in chunk.text
    assert "…" in chunk.text


def test_no_box_below_the_minimum_width(monkeypatch: pytest.MonkeyPatch) -> None:
    narrow = _BOX_MIN_COLUMNS - 4
    lines = _banner(narrow, monkeypatch).text.splitlines()
    assert not any(line.startswith(("╭", "╰")) for line in lines)
    assert any("Welcome to Agent Code!" in line for line in lines)
    # The logo is never wrapped: it must survive as one intact line.
    assert _LOGO_COMPACT[0] in lines


def test_text_is_ansi_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """``text`` feeds transcripts and plain consumers; styling lives in fragments only."""
    chunk: OutputChunk = _banner(120, monkeypatch)
    assert "\x1b" not in chunk.text
    assert chunk.fragments
    assert "".join(piece for _, piece in chunk.fragments) == chunk.text
