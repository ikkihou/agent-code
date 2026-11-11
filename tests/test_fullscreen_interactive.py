"""scrollback+Live 式 REPL 的事件翻译与状态机测试。

不触碰真实终端：LiveArea / event_to_chunks / 提问状态机都是纯函数或
可注入驱动的状态对象；run_agent 用假 provider 驱动 wire 事件流。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent_code.agent import run_agent
from agent_code.interactive import (
    LiveArea,
    SlashCompleter,
    _chunk_fragments,
    event_to_chunks,
    parse_choice_index,
    parse_confirm_answer,
    prompt_for_request,
    render_prompt_request,
    render_user_prompt,
)
from agent_code.model import ModelResponse, ModelStreamEvent
from agent_code.output import OutputChunk
from agent_code.runtime import RuntimeState
from agent_code.slash import SlashContext, dispatch_slash
from agent_code.tools import ToolRegistry
from agent_code.wire import (
    CommandPreview,
    DiffPreview,
    Line,
    PromptEcho,
    StreamLineBuffer,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
    Wire,
)


class _RecordingSink:
    """同步 wire 消费者：记录收到的全部事件批（send 时立刻 flush）。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def send(self, batch: list[Any]) -> None:
        self.events.extend(batch)

    def texts(self) -> list[str]:
        """TextDelta 原文流（模拟消费端攒行前的样子）。"""
        return [e.text for e in self.events if isinstance(e, TextDelta)]


def _collecting_wire() -> tuple[Wire, _RecordingSink]:
    wire = Wire()
    sink = _RecordingSink()
    wire.sink = sink.send
    return wire, sink


class _FragmentedProvider:
    def __init__(self, chunks: list[str], final_text: str) -> None:
        self.chunks = chunks
        self.final_text = final_text

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: Any = None,
    ):
        for chunk in self.chunks:
            yield ModelStreamEvent(type="text_delta", text=chunk)
        yield ModelStreamEvent(
            type="completed",
            response=ModelResponse(text=self.final_text),
        )


class _SequenceProvider:
    """依次返回预设的 ModelResponse，用于驱动 run_agent 的多次请求。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: Any = None,
    ):
        response = self._responses.pop(0)
        if response.text:
            yield ModelStreamEvent(type="text_delta", text=response.text)
        yield ModelStreamEvent(type="completed", response=response)


# ---- run_agent → wire 事件流 ------------------------------------------------


def test_run_agent_streaming_reaches_wire_as_text_delta(tmp_path: Path) -> None:
    final_text = "第一行完整内容\n\n- **能力**：可以理解、生成和处理文本"
    provider = _FragmentedProvider(
        ["第一行", "完整内容\n\n- **能", "力**：可以理解、", "生成和处理文本"],
        final_text,
    )
    wire, sink = _collecting_wire()

    result = run_agent(
        "介绍一下自己",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    assert result.final == final_text
    # 原始 delta 原样上 wire；攒成整行是消费端 StreamLineBuffer 的职责。
    assert "".join(sink.texts()) == final_text

    lines: list[str] = []
    line_buf = StreamLineBuffer(lines.append)
    for text in sink.texts():
        line_buf.feed(text)
    line_buf.finish()
    assert lines == ["第一行完整内容", "", "- **能力**：可以理解、生成和处理文本"]


def test_wire_merges_adjacent_text_deltas_in_queue() -> None:
    """UI 消费不及时时，相邻 TextDelta 在队列内原地合并，不积压成百条事件。"""
    wire = Wire()
    # 只 notify、不设 sink：模拟 UI 还没来 drain 的异步消费模式。
    wire.notify = lambda: None

    for text in ["a", "b", "c"]:
        wire.send(TextDelta(text=text))

    assert len(wire.drain()) == 1


def test_wire_sync_sink_flushes_each_send() -> None:
    """同步 sink：每次 send 立刻 flush，事件已被消费、不发生合并。"""
    wire = Wire()
    batches: list[list[Any]] = []
    wire.sink = batches.append

    wire.send(Line("one"))
    wire.send(TextDelta("del"))
    wire.send(TextDelta("ta"))

    flat = [e for batch in batches for e in batch]
    assert [type(e).__name__ for e in flat] == ["Line", "TextDelta", "TextDelta"]
    assert flat[1].text == "del" and flat[2].text == "ta"


def test_run_agent_retries_empty_completion_once(tmp_path: Path) -> None:
    """空完成（无文本无工具调用）不应直接收尾，而应重试一次。"""
    provider = _SequenceProvider(
        [
            ModelResponse(),
            ModelResponse(text="hello world"),
        ]
    )
    wire, sink = _collecting_wire()

    result = run_agent(
        "hi",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    assert result.final == "hello world"
    assert any(
        "continue: empty response" in e.text for e in sink.events if isinstance(e, Line)
    )


def test_run_agent_marks_final_empty_after_retries_exhausted(tmp_path: Path) -> None:
    """重试次数耗尽后仍是空响应时，明确标记而不是静默空 final。"""
    provider = _SequenceProvider(
        [
            ModelResponse(),
            ModelResponse(),
            ModelResponse(text=""),
        ]
    )
    wire, sink = _collecting_wire()

    result = run_agent(
        "hi",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    assert result.final == ""
    assert any(
        "final: (empty response)" in e.text for e in sink.events if isinstance(e, Line)
    )


def test_run_agent_continues_after_max_tokens_truncation(tmp_path: Path) -> None:
    """stop_reason == "max_tokens" 时，不应把截断的回复当完整回答收尾，应续写一次。"""
    provider = _SequenceProvider(
        [
            ModelResponse(text="回复被截断", stop_reason="max_tokens"),
            ModelResponse(text="补充的结尾"),
        ]
    )
    wire, sink = _collecting_wire()

    result = run_agent(
        "hi",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    assert result.final == "补充的结尾"
    assert any(
        "continue: max_tokens" in e.text for e in sink.events if isinstance(e, Line)
    )


def test_run_agent_recovers_final_from_streamed_text(tmp_path: Path) -> None:
    """final message 缺 text block 时，用实际流过的文本兜底重建 final。"""

    class _StreamOnlyProvider:
        def complete_stream(
            self,
            messages: list[dict[str, Any]],
            tools: list[Any] | None = None,
            system: str | None = None,
            *,
            signal: Any = None,
        ):
            yield ModelStreamEvent(type="text_delta", text="你好")
            yield ModelStreamEvent(type="text_delta", text="世界\n")
            yield ModelStreamEvent(type="completed", response=ModelResponse())

    wire, sink = _collecting_wire()

    result = run_agent(
        "hi",
        _StreamOnlyProvider(),
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    # 兜底重建用的就是流式收到的原始分片，所以保留了末尾换行。
    assert result.final == "你好世界\n"
    assert "".join(sink.texts()) == "你好世界\n"


# ---- LiveArea：流式尾部状态机 ------------------------------------------------


def test_live_area_commits_closed_paragraphs_incrementally() -> None:
    """增量提交：空行闭合的段落立刻返回（提交 scrollback），Live 区只留尾段。"""
    live = LiveArea()
    assert live.feed_stream("第一段第一行\n第一段第二行\n") == []
    # 空行闭合第一段 → 立刻可提交。
    assert live.feed_stream("\n") == ["第一段第一行\n第一段第二行\n\n"]
    # 第二段仍在进行中，不提交。
    assert live.feed_stream("第二段") == []
    assert live.feed_stream("内容\n") == []

    # Live 区只显示未闭合的第二段。
    text = "".join(t for _, t in live.formatted(columns=80, now=0))
    assert "第二段" in text
    assert "第一段" not in text

    # turn 收尾把未闭合段落（含半行）整段取出。
    live.feed_stream("尾巴")
    assert live.flush_paragraph() == "第二段内容\n尾巴\n"
    assert live.flush_paragraph() == ""  # 幂等：再取为空


def test_live_area_reset_stream_clears_paragraph_state() -> None:
    live = LiveArea()
    live.feed_stream("abc\ndef")
    live.reset_stream()

    assert live.open_paragraph == []
    assert live.stream_pending == ""
    assert live.flush_paragraph() == ""


def test_live_area_formatted_renders_spinner_tool_and_preview() -> None:
    live = LiveArea()
    live.turn_active = True
    live.turn_started_at = 1000.0
    live.active_tool = "bash"
    live.pending_preview = ["⏸ bash echo hi — waiting for approval"]
    live.feed_stream("streaming tail")

    fragments = live.formatted(columns=40, now=1005.0)
    text = "".join(t for _, t in fragments)
    assert "bash · 5s" in text
    assert "⏸ bash echo hi" in text
    assert "streaming tail" in text

    # turn 不活跃时不渲染 spinner。
    live.turn_active = False
    text2 = "".join(t for _, t in live.formatted(columns=40, now=1006.0))
    assert "· 6s" not in text2


# ---- event_to_chunks：已完成事件 → scrollback chunk --------------------------


def test_event_to_chunks_renders_wire_events() -> None:
    """REPL 渲染策略：非流式事件全部翻译成 OutputChunk，格式由前端决定。"""
    chunks = event_to_chunks(Line("hello"))
    assert len(chunks) == 1
    assert chunks[0].text == "hello\n"

    echo = event_to_chunks(PromptEcho("hi there"))
    assert echo[0].text == "\n\n> hi there\n\n"  # render_user_prompt 的深灰底块

    done = event_to_chunks(ToolCallCompleted(call_id="1", name="read_file"))
    assert done[0].text == "✓ read_file\n"
    err = event_to_chunks(ToolCallCompleted(call_id="1", name="bash", is_error=True))
    assert "(error)" in err[0].text

    diff = event_to_chunks(DiffPreview(path="a.py", old="x\n", new="y\n"))
    assert len(diff) == 2
    assert diff[0].format == "rich"
    assert "+y" in diff[1].text

    cmd = event_to_chunks(CommandPreview(command="echo hi"))
    assert "Command: echo hi" in cmd[0].text

    # 流式/生命周期事件不是 chunk 翻译的职责。
    assert event_to_chunks(TextDelta("half")) == []
    assert event_to_chunks(TurnStarted()) == []
    assert event_to_chunks(TurnEnded()) == []


def test_chunk_fragments_normalizes_all_formats() -> None:
    assert _chunk_fragments(OutputChunk("plain\n")) == [("", "plain\n")]
    rich = _chunk_fragments(OutputChunk("[bold]b[/bold]\n", format="rich"))
    assert ("bold", "b") in rich
    ansi = _chunk_fragments(OutputChunk("\x1b[1mB\x1b[0m\n", format="ansi"))
    assert ("bold", "B") in ansi


def test_render_user_prompt_formats_prompt_blocks() -> None:
    chunk = render_user_prompt("hello")
    assert chunk.format == "plain"
    assert chunk.text == "\n\n> hello\n\n"
    assert render_user_prompt("first\nsecond").text == "\n\n> first\n> second\n\n"
    # source 头当前不再渲染，参数仅作兼容保留。
    assert render_user_prompt("expanded", source="user /slash").text == "\n\n> expanded\n\n"
    empty = render_user_prompt("")
    assert empty.format == "plain"
    assert empty.text == ""
    assert ("ansiwhite bg:#444444", "> hello") in chunk.fragments


# ---- 提问请求的渲染与解析 -----------------------------------------------------


def test_prompt_request_helpers_parse_confirm_answers() -> None:
    assert parse_confirm_answer("", default=False) is False
    assert parse_confirm_answer("", default=True) is True
    assert parse_confirm_answer("y") is True
    assert parse_confirm_answer("YES") is True
    assert parse_confirm_answer("n") is False
    assert parse_confirm_answer("maybe") is None


def test_prompt_request_helpers_parse_choice_indexes() -> None:
    assert parse_choice_index("", option_count=2, default=0) == 0
    assert parse_choice_index("2", option_count=2) == 2
    assert parse_choice_index("0", option_count=2) == 0
    assert parse_choice_index("3", option_count=2) is None
    assert parse_choice_index("abc", option_count=2) is None


def test_prompt_request_helpers_render_bottom_prompt_text() -> None:
    confirm_request = {"type": "confirm", "message": "Run this command?", "default": False}
    assert render_prompt_request(confirm_request) == "? Run this command? [y/N]\n"
    assert prompt_for_request(confirm_request) == "Confirm [y/N]: "

    choice_request = {
        "type": "choice",
        "question": "Pick one",
        "labels": ["A", "B"],
        "default": 0,
    }
    assert render_prompt_request(choice_request) == (
        "? Pick one\n  1. A\n  2. B\n  0. Skip / Other\n"
    )
    assert prompt_for_request(choice_request) == "Choice [0]: "


# ---- slash ------------------------------------------------------------------


def test_sessions_slash_returns_renderable_message(tmp_path: Path, capsys: Any) -> None:
    result = dispatch_slash(
        "/sessions",
        SlashContext(
            cwd=tmp_path,
            permission_mode="default",
            model="test-model",
            provider="test-provider",
            session_id=None,
        ),
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert result.handled is True
    assert result.markup is True
    assert result.message == "[dim]没有找到会话。[/dim]"


def test_slash_completer_suggests_command_prefixes() -> None:
    completions = list(
        SlashCompleter().get_completions(Document("/he"), CompleteEvent())
    )

    assert [completion.text for completion in completions] == ["/help"]
    assert completions[0].start_position == -3
    assert str(completions[0].display_meta_text) == "显示所有可用 slash command"


def test_slash_completer_ignores_non_command_input() -> None:
    completer = SlashCompleter()

    assert list(completer.get_completions(Document("hello"), CompleteEvent())) == []
    assert list(completer.get_completions(Document("/loop "), CompleteEvent())) == []


# ---- 提问状态机（submit_text 的 pending 分支，注入 fake future 驱动）----------


def test_shell_render_message_includes_pending_prompt() -> None:
    """提问期间 message 渲染必须带上提示符，且绝不能抛异常。

    曾经用 FormattedText.fragments.insert 修改返回值——FormattedText 没有
    fragments 属性，异常会让 prompt 渲染线程死掉、后续输入永不返回。
    """
    from agent_code.interactive import InteractiveShell

    state = RuntimeState(permission_mode="default", model="mock", provider="mock")
    shell = InteractiveShell(state, run_turn=lambda *a: None, make_slash_context=lambda: None)
    shell._pending_prompt = {
        "request": {"type": "confirm", "message": "Run this command?", "default": False},
        "future": None,
    }

    text = "".join(t for _, t in shell._render_message())
    assert "Confirm [y/N]:" in text


def test_shell_submit_text_routes_pending_choice_answer() -> None:
    """pending choice 提问：回答直接喂给 future，状态机清空。"""
    from agent_code.interactive import InteractiveShell

    state = RuntimeState(permission_mode="default", model="mock", provider="mock")

    def noop(*args: Any, **kwargs: Any) -> None:
        return None

    shell = InteractiveShell(state=state, run_turn=noop, make_slash_context=noop)
    shell._commit_now = lambda text: None  # 不碰真实终端

    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        shell._pending_prompt = {
            "request": {"type": "choice", "labels": ["a", "b", "c"], "default": 0},
            "future": future,
        }

        shell._submit_text("2")

        assert future.done() and future.result() == "2"
        assert shell._pending_prompt is None
    finally:
        loop.close()


def test_shell_submit_text_rejects_invalid_choice_and_keeps_pending() -> None:
    from agent_code.interactive import InteractiveShell

    state = RuntimeState(permission_mode="default", model="mock", provider="mock")

    def noop(*args: Any, **kwargs: Any) -> None:
        return None

    shell = InteractiveShell(state=state, run_turn=noop, make_slash_context=noop)
    shell._commit_now = lambda text: None

    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        entry = {
            "request": {"type": "choice", "labels": ["a", "b"], "default": 0},
            "future": future,
        }
        shell._pending_prompt = dict(entry)

        shell._submit_text("9")  # 非法

        assert not future.done()
        assert shell._pending_prompt == entry  # 保持 pending，等下一次输入
    finally:
        loop.close()
