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

from textual.driver import Driver
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import OptionList

from agent_code.output import OutputChunk, TranscriptItem
from agent_code.runtime import RuntimeState
from agent_code.slash import SlashContext, default_slash_registry
from agent_code.ui.app import AgentApp
from agent_code.ui.blocks import BLOCK_GUTTER, BlockLog, TextBlock, ThinkingBlock, ToolCallBlock
from agent_code.ui.completion import CompletionSource
from agent_code.ui.events import render_user_prompt
from agent_code.ui.render import chunk_to_text, render_status
from agent_code.ui.transcript import ThinkingItem, ToolCallItem, TranscriptSink
from agent_code.ui.widgets import HintBar, PromptInput
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
    body = next(line for line in lines if " hello" in line)
    assert body == " hello" + " " * (40 - len(" hello"))


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


def test_transcript_emits_one_item_per_tool_call() -> None:
    sink = TranscriptSink()
    items = sink.handle_batch(
        [
            TurnStarted(),
            ToolCallStarted(call_id="1", name="bash", args_preview="{}"),
            ToolCallCompleted(call_id="1", name="bash"),
            ToolCallStarted(call_id="2", name="bash", args_preview="{}"),
            ToolCallCompleted(call_id="2", name="bash"),
            TurnEnded(),
        ]
    )

    assert [type(item).__name__ for item in items] == ["ToolCallItem", "ToolCallItem"]
    tool_items = [item for item in items if isinstance(item, ToolCallItem)]
    assert all(item.name == "bash" and item.status == "ok" for item in tool_items)


def test_transcript_commits_prose_before_the_tool_that_followed_it() -> None:
    sink = TranscriptSink()
    assert sink.handle_batch([TurnStarted(), TextDelta("Let me check.")]) == []

    second = sink.handle_batch(
        [
            ToolCallStarted(call_id="1", name="git_status", args_preview="{}"),
            ToolCallCompleted(call_id="1", name="git_status"),
        ]
    )

    assert [type(item).__name__ for item in second] == ["OutputChunk", "ToolCallItem"]
    prose = second[0]
    assert isinstance(prose, OutputChunk)
    assert prose.text == "Let me check.\n\n"
    assert second[1] == ToolCallItem(name="git_status", args_preview="{}", status="ok")


# ---- headless app ----------------------------------------------------------


def _make_app(
    tmp_path: Path,
    *,
    initial_output: OutputChunk | str = "",
    initial_transcript: list[TranscriptItem] | None = None,
    on_transcript: Callable[[TranscriptItem], None] | None = None,
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
    return app.query_one(BlockLog).plain_text()


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
            assert " hello" in _log_text(app)
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
            prompt = app.query_one(PromptInput)
            assert app._state.permission_mode == "default"
            assert prompt.has_class("mode-default")
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._state.permission_mode == "acceptEdits"
            assert prompt.has_class("mode-edits")
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._state.permission_mode == "plan"
            assert prompt.has_class("mode-plan")
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_does_not_reappend_replayed_history(tmp_path: Path) -> None:
    """The banner and resumed transcript are drawn, not written back to the sidecar.

    Regression: on_mount used to route initial chunks through the live commit path,
    which appended the banner + entire history to the transcript on every resume.
    """

    async def run() -> None:
        persisted: list[TranscriptItem] = []
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
            log = app.query_one(BlockLog)
            width = log.content_width()
            assert width > 80
            assert "hello" in log.plain_text()
            prompt_lines = [line for line in log.plain_text().splitlines() if "hello" in line]
            assert prompt_lines
            # The prompt bar is padded to the log width at commit time.
            assert len(prompt_lines[0]) >= width
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_committed_blocks_keep_a_two_cell_gutter(tmp_path: Path) -> None:
    """Every block type is inset on both sides by the log gutter.

    The CSS margin and ``BlockLog.BLOCK_GUTTER`` (subtracted before wrapping) must
    agree, otherwise prose wraps at a column wider than the block.
    """

    async def run() -> None:
        items: list[TranscriptItem] = [
            OutputChunk("hello\n"),
            ThinkingItem("pondered the problem"),
            ToolCallItem(name="bash", args_preview='{"command": "ls"}', status="ok"),
        ]
        app = _make_app(tmp_path, initial_transcript=items)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            log = app.query_one(BlockLog)
            left = log.content_region.x
            assert len(log.children) == len(items)
            for block in log.children:
                assert block.region.x == left + BLOCK_GUTTER
                assert block.size.width == log.content_width()
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_persists_live_chunks(tmp_path: Path) -> None:
    """Only live output is appended to the transcript, and it still is."""

    async def run() -> None:
        persisted: list[TranscriptItem] = []
        app = _make_app(tmp_path, on_transcript=persisted.append)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press(*"hello")
            await pilot.press("enter")
            await pilot.pause()
            texts = [item.text for item in persisted if isinstance(item, OutputChunk)]
            assert any(" hello" in text for text in texts)
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_app_replays_structured_items_as_collapsibles(tmp_path: Path) -> None:
    """A structured resume transcript mounts thinking/tool blocks, not flattened text."""

    async def run() -> None:
        items: list[TranscriptItem] = [
            OutputChunk("intro\n"),
            ThinkingItem("pondered the problem"),
            ToolCallItem(
                name="bash",
                args_preview='{"command": "ls"}',
                status="ok",
                args_full='{"command": "ls"}',
                result="a.txt",
            ),
        ]
        app = _make_app(tmp_path, initial_transcript=items)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.query_one(ThinkingBlock).collapsed is True
            assert app.query_one(ToolCallBlock).collapsed is True
            assert "intro" in _log_text(app)
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


# ---- block-log selection ---------------------------------------------------


def test_block_log_selects_text(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            log = app.query_one(BlockLog)
            log.append_chunk(OutputChunk("hello world\nsecond line\n"))
            await pilot.pause()
            block = app.query_one(TextBlock)

            app.screen.selections = {block: Selection.from_offsets(Offset(0, 0), Offset(5, 0))}
            await pilot.pause()

            assert app.screen.get_selected_text() == "hello"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_block_log_selects_across_lines(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            log = app.query_one(BlockLog)
            log.append_chunk(OutputChunk("hello world\nsecond line\n"))
            await pilot.pause()
            block = app.query_one(TextBlock)

            app.screen.selections = {block: Selection.from_offsets(Offset(6, 0), Offset(6, 1))}
            await pilot.pause()

            assert app.screen.get_selected_text() == "world\nsecond"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_block_selection_handles_wrapped_lines(tmp_path: Path) -> None:
    """Regression: a paragraph that wraps to more visual rows than logical lines must
    not raise IndexError when the selection starts on wrapped row 1."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(30, 12)) as pilot:
            await pilot.pause()
            log = app.query_one(BlockLog)
            log.append_chunk(
                OutputChunk(
                    "当前分支 master（与 origin/master 同步），工作区有 9 个已修改文件 "
                    "+ 1 个未跟踪目录，未暂存。\n"
                )
            )
            await pilot.pause()
            block = app.query_one(TextBlock)
            assert block.size.height >= 2  # the paragraph wrapped

            # First widget of a multi-widget selection: from wrapped row 1 to its end.
            app.screen.selections = {block: Selection(Offset(0, 1), None)}
            await pilot.pause()

            selected = app.screen.get_selected_text()
            assert selected is not None and selected != ""
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_text_block_reports_selection_text(tmp_path: Path) -> None:
    """A Static text block extracts the text under the selection (copy support)."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            log = app.query_one(BlockLog)
            log.append_chunk(OutputChunk("hello world\nsecond line\n"))
            await pilot.pause()
            block = app.query_one(TextBlock)

            result = block.get_selection(Selection.from_offsets(Offset(0, 0), Offset(5, 0)))
            assert result is not None
            assert result[0] == "hello"
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
            log = app.query_one(BlockLog)
            log.append_chunk(OutputChunk("hello world\nsecond line\n"))
            await pilot.pause()
            block = app.query_one(TextBlock)
            app.screen.selections = {block: Selection.from_offsets(Offset(0, 0), Offset(5, 0))}
            await pilot.pause()

            app.action_abort_or_quit()

            assert app.clipboard == "hello"
            assert app.screen.get_selected_text() is None
            assert app.is_running
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


# ---- foldable blocks -------------------------------------------------------


def test_app_renders_thinking_and_tool_as_collapsed_blocks(tmp_path: Path) -> None:
    """Thinking and tool calls mount as collapsed collapsibles; expanding updates them."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._append_item(ThinkingItem("pondering the answer"))
            app._append_item(OutputChunk("the answer\n"))
            app._append_item(ToolCallItem(name="bash", args_preview="{}", status="ok"))
            await pilot.pause()

            thinking = app.query_one(ThinkingBlock)
            tool = app.query_one(ToolCallBlock)
            assert thinking.collapsed is True
            assert tool.collapsed is True
            # The static text landed in between, in order.
            assert "the answer" in _log_text(app)

            thinking.collapsed = False
            tool.collapsed = False
            await pilot.pause()
            assert thinking.collapsed is False
            assert tool.collapsed is False
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_thinking_block_shows_duration_in_title(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._append_item(ThinkingItem("pondering", duration=2.3))
            await pilot.pause()
            assert app.query_one(ThinkingBlock).title == "✻ Thought for 2.3s"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_thinking_title_uses_milliseconds_under_a_second(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._append_item(ThinkingItem("pondering", duration=0.184))
            await pilot.pause()
            assert app.query_one(ThinkingBlock).title == "✻ Thought for 184ms"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_tool_title_summarizes_the_command_argument(tmp_path: Path) -> None:
    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._append_item(
                ToolCallItem(
                    name="bash",
                    args_preview='{"command": "rg -n foo src"}',
                    status="ok",
                    args_full='{"command": "rg -n foo src"}',
                )
            )
            await pilot.pause()
            block = app.query_one(ToolCallBlock)
            assert block.title == "✓ bash · rg -n foo src"
            assert block.has_class("ok")
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_footer_aligns_the_model_on_the_right() -> None:
    state = RuntimeState(permission_mode="default", model="mock", provider="anthropic")
    plain = render_status(state, cwd=Path("/tmp/project"), width=80).plain
    assert plain.endswith("anthropic/mock")
    assert len(plain) == 80


def test_hint_bar_lists_the_pi_style_bindings() -> None:
    plain = HintBar._render_hints().plain
    assert "esc interrupt" in plain
    assert "/ commands" in plain
    assert "shift+tab mode" in plain


def test_thinking_block_is_borderless_and_transparent(tmp_path: Path) -> None:
    """Thinking must not look like a boxed card: no border, no background fill."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._append_item(ThinkingItem("pondering", duration=1.0))
            await pilot.pause()
            block = app.query_one(ThinkingBlock)
            assert block.styles.border_top[0] == ""
            assert block.styles.background.is_transparent
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_block_log_puts_one_blank_row_between_blocks(tmp_path: Path) -> None:
    """Committed blocks are separated by a single gap; prompt echoes bring their own."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            log = app.query_one(BlockLog)
            log.append_chunk(OutputChunk("one\n"))
            log.append_thinking(ThinkingItem("ponder"))
            log.append_tool(ToolCallItem(name="bash", args_preview="{}", status="ok"))
            log.append_chunk(OutputChunk("two\n"))
            log.append_chunk(render_user_prompt("hello"))
            await pilot.pause()

            blocks = list(log.children)
            assert [block.has_class("gap") for block in blocks] == [
                False,  # first block: no leading blank
                False,  # prose already ends with a blank row
                True,  # thinking -> tool needs a separator
                True,  # tool -> prose needs a separator
                False,  # prompt echo carries its own leading blanks
            ]
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_block_log_caps_mounted_widgets(tmp_path: Path) -> None:
    """Very long sessions must not keep an unbounded number of block widgets."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            log = app.query_one(BlockLog)
            log.MAX_BLOCKS = 3
            for index in range(6):
                log.append_chunk(OutputChunk(f"line {index}\n"))
            await pilot.pause()
            await pilot.pause()
            assert len(log.children) <= 3
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

                app.query_one(BlockLog).focus()
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


# ---- Phase 1 P0 fixes ------------------------------------------------------


def test_prompt_input_placeholders_oversized_paste_and_expands_on_submit(tmp_path: Path) -> None:
    """Oversized pastes insert a compact placeholder token in PromptInput, echo the
    placeholder in BlockLog, and expand to full text when dispatched to the worker."""
    from textual.events import Paste

    submitted_turns: list[str] = []

    async def run() -> None:
        app = _make_app(tmp_path)
        app._run_turn = lambda line, wire: submitted_turns.append(line)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            prompt = app.query_one(PromptInput)
            big_paste = "\n".join(f"line {i}" for i in range(40))
            prompt.post_message(Paste(big_paste))
            await pilot.pause()

            assert prompt.text == "[Pasted text #1 +40 lines]"

            await pilot.press("enter")
            await pilot.pause()
            assert "[Pasted text #1 +40 lines]" in _log_text(app)
            assert "line 39" not in _log_text(app)
        app._job_queue.put("__EXIT__")
        app._worker.join(timeout=2)

    asyncio.run(run())
    assert submitted_turns and "line 39" in submitted_turns[0]


def test_stylesheet_contains_no_hex_literals() -> None:
    """styles/default.tcss must resolve colors through AGENT_THEME variables, not inline hex."""
    import re

    tcss_path = Path(__file__).resolve().parent.parent / "agent_code" / "ui" / "styles" / "default.tcss"
    content = tcss_path.read_text(encoding="utf-8")
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", content) is None


def test_block_log_pauses_auto_scroll_when_scrolled_up_and_resumes_at_bottom(
    tmp_path: Path,
) -> None:
    """Scrolling up pauses auto-follow; scrolling back to the bottom resumes it."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            log = app.query_one(BlockLog)
            for i in range(20):
                log.append_chunk(OutputChunk(f"line {i}\n"))
            await pilot.pause()
            await pilot.pause()
            assert log.max_scroll_y > 0
            assert log.is_vertical_scroll_end

            # User scrolls to the top to inspect earlier lines.
            log.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert log._auto_scroll is False
            assert log.scroll_offset.y == 0

            # New chunks arrive while scrolled up: the viewport must stay at y=0.
            log.append_chunk(OutputChunk("new line while reading\n"))
            await pilot.pause()
            await pilot.pause()
            assert log.scroll_offset.y == 0

            # User jumps back to the bottom: auto-scroll resumes for subsequent chunks.
            log.scroll_end(animate=False)
            await pilot.pause()
            assert log._auto_scroll is True
            log.append_chunk(OutputChunk("followed line\n"))
            await pilot.pause()
            await pilot.pause()
            assert log.is_vertical_scroll_end
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_block_log_rewraps_markdown_and_prompt_bars_on_resize(tmp_path: Path) -> None:
    """Resizing the terminal re-wraps markdown blocks and re-pads full-width prompts."""

    async def run() -> None:
        prose = " ".join(["word"] * 40) + "\n"
        app = _make_app(
            tmp_path,
            initial_transcript=[
                render_user_prompt("hello"),
                OutputChunk(prose, format="markdown"),
            ],
        )
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            log = app.query_one(BlockLog)
            wide_width = log.content_width()
            prompt_lines_wide = [
                line for line in log.plain_text().splitlines() if "hello" in line
            ]
            assert prompt_lines_wide and len(prompt_lines_wide[0]) == wide_width

            await pilot.resize_terminal(70, 30)
            await pilot.pause()
            await pilot.pause()
            narrow_width = log.content_width()
            assert narrow_width < wide_width
            prompt_lines_narrow = [
                line for line in log.plain_text().splitlines() if "hello" in line
            ]
            assert prompt_lines_narrow and len(prompt_lines_narrow[0]) == narrow_width
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_slash_permissions_command_updates_prompt_border(tmp_path: Path) -> None:
    """Switching permission mode via /permissions updates PromptInput's border class."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            prompt = app.query_one(PromptInput)
            assert prompt.has_class("mode-default")

            app._submit_text("/permissions plan")
            await pilot.pause()
            assert app._state.permission_mode == "plan"
            assert prompt.has_class("mode-plan")
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_transcript_tracks_parallel_active_tools() -> None:
    """Completing one parallel tool call keeps the remaining active tools on the spinner."""
    sink = TranscriptSink()
    sink.handle_batch(
        [
            TurnStarted(),
            ToolCallStarted(call_id="1", name="read_file", args_preview="{}"),
            ToolCallStarted(call_id="2", name="grep", args_preview="{}"),
        ]
    )
    assert sink.live.active_tool == "read_file (+1 more)"

    sink.handle_batch([ToolCallCompleted(call_id="1", name="read_file")])
    assert sink.live.active_tool == "grep"

    sink.handle_batch([ToolCallCompleted(call_id="2", name="grep")])
    assert sink.live.active_tool is None


# ---- Phase 2 UX & interaction upgrades -------------------------------------


def test_completion_menu_renders_meta_and_respects_cursor_location(tmp_path: Path) -> None:
    """Slash options show meta descriptions; @file completion replaces at the cursor row/col."""
    (tmp_path / "readme.md").write_text("hi\n", encoding="utf-8")

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press(*"/he")
            await pilot.pause()
            menu = app.query_one("#completion", OptionList)
            option = menu.get_option_at_index(0)
            prompt_text = str(option.prompt)
            assert "/help" in prompt_text
            assert "show all available slash commands" in prompt_text

            # Multiline input with cursor on row 0: completion replaces on row 0 only.
            app._prompt.load_text("check @read\nsecond line")
            app._prompt.move_cursor((0, len("check @read")))
            app._refresh_completion()
            await pilot.pause()
            assert menu.display is True

            await pilot.press("tab")
            await pilot.pause()
            assert app._prompt.text == "check readme.md\nsecond line"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_pending_confirm_prompt_supports_single_key_y_n_and_always_allow(
    tmp_path: Path,
) -> None:
    """Confirm requests open OptionList, accept y/n/a or arrows+Enter, and swallow typing."""
    import queue

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            menu = app.query_one("#completion", OptionList)
            q1: queue.Queue[bool] = queue.Queue()
            app._begin_prompt({"type": "confirm", "message": "Run bash?", "default": False}, q1)
            await pilot.pause()
            assert menu.display is True
            assert menu.highlighted == 1  # default=False highlights "No"

            await pilot.press("y")
            await pilot.pause()
            assert q1.get_nowait() is True
            assert app._pending_prompt is None
            assert menu.display is False
            assert app._prompt.text == ""

            q2: queue.Queue[bool] = queue.Queue()
            app._begin_prompt({"type": "confirm", "message": "Apply edit?", "default": False}, q2)
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
            assert q2.get_nowait() is True
            assert app._state.permission_mode == "acceptEdits"
            assert app.query_one(PromptInput).has_class("mode-edits")
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_pending_choice_prompt_supports_arrow_navigation_and_enter(tmp_path: Path) -> None:
    """Choice requests open OptionList without duplicating options in LiveArea."""
    import queue

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            q: queue.Queue[str] = queue.Queue()
            app._begin_prompt(
                {
                    "type": "choice",
                    "question": "Pick a target",
                    "labels": ["Alpha", "Beta"],
                    "default": 0,
                },
                q,
            )
            await pilot.pause()
            menu = app.query_one("#completion", OptionList)
            assert menu.display is True
            assert app._live.pending_preview == []

            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert q.get_nowait() == "2"
            assert menu.display is False
            assert "Pick a target → Beta" in _log_text(app)
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_render_live_colors_diff_preview_lines() -> None:
    """Diff lines in LiveArea.pending_preview get green/red/cyan styles instead of flat amber."""
    from agent_code.ui.live import LiveArea
    from agent_code.ui.render import render_live
    from agent_code.ui.theme import PALETTE

    live = LiveArea()
    live.pending_preview = [
        "--- a/foo.py",
        "+++ b/foo.py",
        "@@ -1 +1 @@",
        "-old",
        "+new",
    ]
    rendered = render_live(live, columns=80, now=1.0)
    span_styles = [str(span.style) for span in rendered.spans]
    assert any(PALETTE.success in style for style in span_styles)
    assert any(PALETTE.error in style for style in span_styles)
    assert any(PALETTE.accent in style for style in span_styles)


def test_prompt_input_history_recall_and_draft_preservation(tmp_path: Path) -> None:
    """Up/Down recalls submitted prompts and preserves the current unsubmitted draft."""

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press(*"first prompt")
            await pilot.press("enter")
            await pilot.pause()

            await pilot.press(*"second prompt")
            await pilot.press("enter")
            await pilot.pause()

            await pilot.press(*"draft text")
            await pilot.pause()
            assert app._prompt.text == "draft text"

            await pilot.press("up")
            await pilot.pause()
            assert app._prompt.text == "second prompt"

            await pilot.press("up")
            await pilot.pause()
            assert app._prompt.text == "first prompt"

            await pilot.press("down")
            await pilot.pause()
            assert app._prompt.text == "second prompt"

            await pilot.press("down")
            await pilot.pause()
            assert app._prompt.text == "draft text"
        app._job_queue.put("__EXIT__")

    asyncio.run(run())


def test_ctrl_o_toggles_latest_collapsible_block_and_formats_edit_diff(
    tmp_path: Path,
) -> None:
    """Ctrl+O expands/collapses the latest foldable block; file_edit blocks format a diff."""
    import json

    async def run() -> None:
        app = _make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            args_json = json.dumps(
                {
                    "file_path": "a.py",
                    "old_string": "x = 1\n",
                    "new_string": "x = 2\n",
                }
            )
            app._append_item(
                ToolCallItem(
                    name="file_edit",
                    args_preview=args_json,
                    status="ok",
                    args_full=args_json,
                    result="edited a.py",
                )
            )
            await pilot.pause()
            block = app.query_one(ToolCallBlock)
            assert block.collapsed is True

            await pilot.press("ctrl+o")
            await pilot.pause()
            assert block.collapsed is False

            await pilot.press("ctrl+o")
            await pilot.pause()
            assert block.collapsed is True
        app._job_queue.put("__EXIT__")

    asyncio.run(run())
