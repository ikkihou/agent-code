"""Tests for the Textual frontend.

The pure logic (chunk rendering, completion, the transcript state machine) is tested
without a live app; the headless ``run_test`` pilot exercises the real app wiring
(compose -> submit -> commit to the log).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

from rich.color import Color
from textual.driver import Driver
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import OptionList

from agent_code.output import OutputChunk
from agent_code.runtime import RuntimeState
from agent_code.slash import SlashContext, default_slash_registry
from agent_code.tui.app import AgentApp
from agent_code.tui.completion import CompletionSource
from agent_code.tui.render import chunk_to_text, render_status
from agent_code.tui.transcript import TranscriptSink
from agent_code.tui.widgets import OutputLog, PromptInput
from agent_code.ui.events import render_user_prompt
from agent_code.wire import (
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
)

# ---- render ----------------------------------------------------------------


def test_chunk_to_text_pads_full_width_prompt() -> None:
    chunk = render_user_prompt("hello")
    text = chunk_to_text(chunk, width=40)

    lines = text.plain.split("\n")
    body = next(line for line in lines if "> hello" in line)
    assert body == "> hello" + " " * (40 - len("> hello"))


def test_chunk_to_text_plain_passthrough() -> None:
    text = chunk_to_text(OutputChunk("plain\n"))
    assert text.plain == "plain\n"


def test_render_status_shows_mode_model_and_context() -> None:
    state = RuntimeState(permission_mode="plan", model="mock")
    plain = render_status(state).plain
    assert "plan" in plain
    assert "mock" in plain


# ---- completion ------------------------------------------------------------


def test_completion_slash_suggestions() -> None:
    source = CompletionSource(default_slash_registry())
    suggestions, replace_len = source.suggestions("/he")

    assert [s.insert for s in suggestions] == ["/help"]
    assert replace_len == len("/he")


def test_completion_mention_suggestions(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x\n", encoding="utf-8")

    source = CompletionSource(cwd=tmp_path)
    suggestions, replace_len = source.suggestions("read @main")

    assert [s.insert for s in suggestions] == ["src/main.py"]
    assert replace_len == len("@main")


def test_completion_no_suggestions_for_plain_text(tmp_path: Path) -> None:
    source = CompletionSource(default_slash_registry(), tmp_path)
    assert source.suggestions("plain words here") == ([], 0)


# ---- transcript state machine ----------------------------------------------


def test_transcript_folds_tool_burst_into_one_line() -> None:
    sink = TranscriptSink()
    chunks = sink.handle_batch(
        [
            TurnStarted(),
            ToolCallStarted(call_id="1", name="bash", args_preview="{}"),
            ToolCallCompleted(call_id="1", name="bash"),
            ToolCallCompleted(call_id="2", name="bash"),
            TurnEnded(),
        ]
    )

    assert [chunk.text for chunk in chunks] == ["✓ 2 tools: bash ×2\n"]


def test_transcript_commits_prose_before_the_tool_that_followed_it() -> None:
    sink = TranscriptSink()
    assert sink.handle_batch([TurnStarted(), TextDelta("Let me check.")]) == []

    second = sink.handle_batch(
        [
            ToolCallStarted(call_id="1", name="git_status", args_preview="{}"),
            ToolCallCompleted(call_id="1", name="git_status"),
        ]
    )

    assert [chunk.text for chunk in second] == ["Let me check.\n\n"]
    assert sink.tool_fold == [("git_status", "ok")]


# ---- headless app ----------------------------------------------------------


def _make_app(
    tmp_path: Path,
    *,
    initial_output: OutputChunk | str = "",
    initial_transcript: list[OutputChunk] | None = None,
    on_transcript: Callable[[OutputChunk], None] | None = None,
) -> AgentApp:
    state = RuntimeState(permission_mode="default", model="mock", provider="mock")
    registry = default_slash_registry()

    def make_ctx() -> SlashContext:
        return SlashContext(
            cwd=tmp_path,
            permission_mode=state.permission_mode,
            model=state.model,
            provider=state.provider,
            session_id=None,
            state=state,
            registry=registry,
            context_window=0,
        )

    return AgentApp(
        state,
        run_turn=lambda line, wire: None,
        make_slash_context=make_ctx,
        initial_output=initial_output,
        initial_transcript=initial_transcript,
        on_transcript=on_transcript,
        slash_registry=registry,
        cwd=tmp_path,
    )


def _log_text(app: AgentApp) -> str:
    return "\n".join(str(line) for line in app.query_one(OutputLog).lines)


def test_app_opens_slash_completion_menu(tmp_path: Path) -> None:
    """Typing a slash prefix shows the completion menu; Tab accepts the suggestion.

    Regression: the Changed handler was misnamed (on_prompt_input_changed instead of
    on_text_area_changed, the name Textual derives from TextArea.Changed), so the menu
    never opened at all.
    """

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press(*"/he")
            await pilot.pause()
            completion = app.query_one("#completion", OptionList)
            assert completion.display is True
            assert app._suggestions and app._suggestions[0].insert == "/help"

            await pilot.press("tab")
            await pilot.pause()
            assert app._prompt.text == "/help"
            # Tab accepts the command and closes the menu (no one-row leftover).
            assert completion.display is False
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_accepts_completion_with_enter(tmp_path: Path) -> None:
    """With the menu open, Enter accepts the highlighted suggestion instead of
    submitting the half-typed command."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press(*"/he")
            await pilot.pause()
            assert app.query_one("#completion", OptionList).display is True

            await pilot.press("enter")
            await pilot.pause()
            assert app._prompt.text == "/help"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_submits_slash_command(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press(*"/help")
            await pilot.press("enter")
            await pilot.pause()
            assert "Available commands" in _log_text(app)
            assert "/model" in _log_text(app)
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_echoes_user_prompt(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press(*"hello")
            await pilot.press("enter")
            await pilot.pause()
            assert "> hello" in _log_text(app)
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_tab_accepts_slash_completion_and_closes_menu(tmp_path: Path) -> None:
    """Tab closes the menu instead of reopening a one-row list of the accepted command."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause()
            menu = app.query_one(OptionList)
            assert menu.display is True
            expected = app._suggestions[0].insert

            await pilot.press("tab")
            await pilot.pause()
            assert app._prompt.text == expected
            assert menu.display is False
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_shift_tab_cycles_permission_mode(tmp_path: Path) -> None:
    """Regression: Screen's shift+tab -> focus_previous shadowed the app binding.

    The app binding must be priority so shift+tab cycles the mode instead of moving
    focus.
    """

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app._state.permission_mode == "default"
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._state.permission_mode == "acceptEdits"
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._state.permission_mode == "plan"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_does_not_reappend_replayed_history(tmp_path: Path) -> None:
    """The banner and resumed transcript are drawn, not written back to the sidecar.

    Regression: on_mount used to route initial chunks through the live commit path,
    which appended the banner + entire history to the transcript on every resume.
    """

    async def run() -> None:
        persisted: list[OutputChunk] = []
        app = _make_app(
            tmp_path,
            initial_output=OutputChunk("BANNER\n"),
            initial_transcript=[OutputChunk("old line\n")],
            on_transcript=persisted.append,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert persisted == []
            text = _log_text(app)
            assert "BANNER" in text
            assert "old line" in text
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_replays_history_at_the_log_width(tmp_path: Path) -> None:
    """Resume replay must use the laid-out log width, not the 80-column fallback."""

    async def run() -> None:
        app = _make_app(
            tmp_path,
            initial_transcript=[render_user_prompt("hello")],
        )
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            log = app.query_one(OutputLog)
            width = log._content_width()
            assert width > 80
            prompt_lines = [line for line in log.lines if "hello" in str(line)]
            assert prompt_lines
            assert prompt_lines[0].cell_length == width
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_persists_live_chunks(tmp_path: Path) -> None:
    """Only live output is appended to the transcript, and it still is."""

    async def run() -> None:
        persisted: list[OutputChunk] = []
        app = _make_app(tmp_path, on_transcript=persisted.append)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press(*"hello")
            await pilot.press("enter")
            await pilot.pause()
            assert any("> hello" in chunk.text for chunk in persisted)
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


# ---- output-log selection --------------------------------------------------


def _write_log(app: AgentApp) -> OutputLog:
    log = app.query_one(OutputLog)
    log.write_chunk(OutputChunk("hello world\nsecond line\n"))
    return log


def test_output_log_selects_text(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            log = _write_log(app)
            await pilot.pause()

            app.screen.selections = {
                log: Selection.from_offsets(Offset(0, 0), Offset(5, 0))
            }
            await pilot.pause()

            assert app.screen.get_selected_text() == "hello"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_output_log_selects_across_lines(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            log = _write_log(app)
            await pilot.pause()

            app.screen.selections = {
                log: Selection.from_offsets(Offset(6, 0), Offset(6, 1))
            }
            await pilot.pause()

            assert app.screen.get_selected_text() == "world\nsecond"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_output_log_reports_content_offsets(tmp_path: Path) -> None:
    """Mouse hit-testing needs offset metadata, otherwise nothing is selectable."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            log = _write_log(app)
            await pilot.pause()

            offsets = {
                segment.style.meta["offset"]
                for segment in log.render_line(0)
                if segment.style is not None and "offset" in segment.style.meta
            }
            assert offsets
            assert all(offset[1] == 0 for offset in offsets)
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_output_log_paints_the_selection(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            log = _write_log(app)
            await pilot.pause()

            before = log.render_line(0)
            app.screen.selections = {
                log: Selection.from_offsets(Offset(0, 0), Offset(5, 0))
            }
            await pilot.pause()
            after = log.render_line(0)

            assert after != before
            highlighted = next(seg for seg in after if seg.text == "hello")
            assert highlighted.style is not None
            assert highlighted.style.bgcolor == Color.parse("#264f78")
            # The rest of the line keeps its normal styling.
            rest = next(seg for seg in after if seg.text == " world")
            assert rest.style is not None
            assert rest.style.bgcolor != Color.parse("#264f78")
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_selection_highlight_is_readable(tmp_path: Path) -> None:
    """Foreground must differ from background, or selected text is invisible."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            style = app.screen.get_component_rich_style("screen--selection")
            assert style.color != style.bgcolor
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_ctrl_c_copies_the_selection_instead_of_quitting(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            log = _write_log(app)
            await pilot.pause()
            app.screen.selections = {
                log: Selection.from_offsets(Offset(0, 0), Offset(5, 0))
            }
            await pilot.pause()

            app.action_abort_or_quit()

            assert app.clipboard == "hello"
            assert app.screen.get_selected_text() is None
            assert app.is_running
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


# ---- hardware cursor (IME anchoring) ---------------------------------------


class _RecordingDriver:
    """Minimal driver stub that records the escape sequences written to it."""

    is_headless = False
    is_inline = False
    is_web = False

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, data: str) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass


def test_focusing_the_prompt_shows_the_hardware_cursor(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            original = app._driver
            driver = _RecordingDriver()
            app._driver = cast(Driver, driver)
            try:
                prompt = app.query_one(PromptInput)

                app.query_one(OutputLog).focus()
                await pilot.pause()
                assert "\x1b[?25l" in driver.written

                prompt.focus()
                await pilot.pause()
                assert "\x1b[?25h" in driver.written
            finally:
                app._driver = original
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_set_terminal_cursor_is_a_no_op_when_headless(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.set_terminal_cursor(True)  # headless driver: must not raise
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_terminal_cursor_tracks_the_prompt_caret(tmp_path: Path) -> None:
    """The IME window is anchored to ``App.cursor_position``, so it must follow typing."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            prompt = app.query_one(PromptInput)

            await pilot.press(*"abc")
            await pilot.pause()
            assert app.cursor_position == prompt.cursor_screen_offset

            await pilot.press("enter")
            await pilot.pause()
            assert app.cursor_position == prompt.cursor_screen_offset
        app._job_queue.put("__EXIT__")

    asyncio.run(run())
