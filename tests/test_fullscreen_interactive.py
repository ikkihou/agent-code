from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_ANSI_ESC = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESC.sub("", text)

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import InMemoryClipboard
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.selection import SelectionState

from agent_code.agent import run_agent
from agent_code.interactive import (
    OutputTranscript,
    SlashCompleter,
    TranscriptLexer,
    _copy_output,
    append_output_text,
    output_follow_position,
    parse_choice_index,
    parse_confirm_answer,
    prompt_for_request,
    render_prompt_request,
    render_user_prompt,
    split_fragments_by_lines,
)
from agent_code.model import ModelResponse, ModelStreamEvent
from agent_code.output import OutputChunk
from agent_code.runtime import RuntimeState
from agent_code.slash import SlashContext, dispatch_slash
from agent_code.tools import ToolRegistry


def test_append_output_text_keeps_plain_transcript() -> None:
    transcript = OutputTranscript("first\n")

    display = append_output_text(transcript, "second\n")

    assert display == "second\n"
    assert transcript.text == "first\nsecond\n"
    assert transcript.line_count == 2
    assert transcript.fragments == [("", "first\nsecond\n")]


def test_append_output_text_returns_plain_text_of_ansi_chunk() -> None:
    transcript = OutputTranscript()

    display = append_output_text(
        transcript, OutputChunk("\x1b[1mBold\x1b[0m plain\n", format="ansi")
    )

    assert display == "Bold plain\n"


def test_append_output_text_preserves_ansi_style_fragments() -> None:
    transcript = OutputTranscript()

    append_output_text(transcript, OutputChunk("\x1b[1mBold\x1b[0m plain\n", format="ansi"))

    assert transcript.text == "Bold plain\n"
    assert ("bold", "Bold") in transcript.fragments
    assert ("", " plain\n") in transcript.fragments


def test_split_fragments_by_lines_splits_styles_across_newlines() -> None:
    transcript = OutputTranscript()
    append_output_text(
        transcript,
        OutputChunk("\x1b[1mA\x1b[0m b\nplain\n", format="ansi"),
    )

    lines = split_fragments_by_lines(transcript.fragments)

    assert lines == [[("bold", "A"), ("", " b")], [("", "plain")], []]


def test_transcript_lexer_returns_styled_lines_and_empty_fallback() -> None:
    transcript = OutputTranscript()
    append_output_text(transcript, OutputChunk("\x1b[31mred\x1b[0m\nnext\n", format="ansi"))

    get_line = TranscriptLexer(transcript).lex_document(Document(transcript.text))

    assert get_line(0) == [("ansired", "red")]
    assert get_line(1) == [("", "next")]
    assert get_line(2) == []
    assert get_line(999) == []


def test_transcript_lexer_pads_user_prompt_lines_across_pane_width() -> None:
    transcript = OutputTranscript()
    append_output_text(transcript, render_user_prompt("hi"))

    class FakeWindow:
        class FakeRenderInfo:
            window_width = 12

        render_info = FakeRenderInfo()

    get_line = TranscriptLexer(  # type: ignore[arg-type]
        transcript, output_window=FakeWindow()
    ).lex_document(Document(transcript.text))

    # transcript.text == "\n\n> hi\n\n"（当前渲染不再带 [user] 头）
    assert get_line(0) == []
    assert get_line(1) == []
    body = get_line(2)  # "> hi" 占 4 列
    # 补到 window_width - 1(留 1 格给 BufferControl 自动追加的尾随空格)
    assert body[-1] == ("bg:#444444", " " * (12 - 1 - 4))
    assert get_line(3) == []
    assert get_line(4) == []


def test_render_user_prompt_formats_prompt_blocks() -> None:
    chunk = render_user_prompt("hello")
    assert chunk.format == "ansi"
    assert _strip_ansi(chunk.text) == "\n\n> hello\n\n"
    assert _strip_ansi(render_user_prompt("first\nsecond").text) == (
        "\n\n> first\n> second\n\n"
    )
    # source 头当前不再渲染，参数仅作兼容保留。
    assert _strip_ansi(render_user_prompt("expanded", source="user /slash").text) == (
        "\n\n> expanded\n\n"
    )
    empty = render_user_prompt("")
    assert empty.format == "plain"
    assert empty.text == ""


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


def test_run_agent_output_callback_receives_complete_lines(tmp_path: Path) -> None:
    final_text = "第一行完整内容\n\n- **能力**：可以理解、生成和处理文本"
    provider = _FragmentedProvider(
        ["第一行", "完整内容\n\n- **能", "力**：可以理解、", "生成和处理文本"],
        final_text,
    )
    output: list[str | OutputChunk] = []

    result = run_agent(
        "介绍一下自己",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        output=output.append,
    )

    assert result.final == final_text
    assert [chunk.text if isinstance(chunk, OutputChunk) else chunk for chunk in output] == [
        "第一行完整内容\n",
        "\n",
        "- **能力**：可以理解、生成和处理文本\n",
    ]


def test_output_follow_position_follows_only_at_bottom() -> None:
    # 在底部 → 光标推到文本末尾（自动跟随）。
    assert output_follow_position(at_bottom=True, cursor_position=3, new_text_len=100) == 100
    # 不在底部 → 光标保持不动，避免把用户正在看的视口拽走。
    assert output_follow_position(at_bottom=False, cursor_position=3, new_text_len=100) == 3
    # 光标超过新文本长度时钳制到末尾。
    assert output_follow_position(at_bottom=False, cursor_position=200, new_text_len=100) == 100


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


def test_run_agent_retries_empty_completion_once(tmp_path: Path) -> None:
    """空完成（无文本无工具调用）不应直接收尾，而应重试一次。"""
    provider = _SequenceProvider(
        [
            ModelResponse(),
            ModelResponse(text="hello world"),
        ]
    )
    output: list[str | OutputChunk] = []

    result = run_agent(
        "hi",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        output=output.append,
    )

    assert result.final == "hello world"
    texts = [chunk.text if isinstance(chunk, OutputChunk) else chunk for chunk in output]
    assert any("continue: empty response" in text for text in texts)


def test_run_agent_marks_final_empty_after_retries_exhausted(tmp_path: Path) -> None:
    """重试次数耗尽后仍是空响应时，明确标记而不是静默空 final。"""
    provider = _SequenceProvider(
        [
            ModelResponse(),
            ModelResponse(),
            ModelResponse(text=""),
        ]
    )
    output: list[str | OutputChunk] = []

    result = run_agent(
        "hi",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        output=output.append,
    )

    assert result.final == ""
    texts = [chunk.text if isinstance(chunk, OutputChunk) else chunk for chunk in output]
    assert any("final: (empty response)" in text for text in texts)


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

    output: list[str | OutputChunk] = []

    result = run_agent(
        "hi",
        _StreamOnlyProvider(),
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        output=output.append,
    )

    # 兜底重建用的就是流式收到的原始分片，所以保留了末尾换行。
    assert result.final == "你好世界\n"
    assert [chunk.text if isinstance(chunk, OutputChunk) else chunk for chunk in output] == [
        "你好世界\n"
    ]


def _fake_event(clipboard: InMemoryClipboard) -> Any:
    class _FakeApp:
        def __init__(self) -> None:
            self.clipboard = clipboard

    class _FakeEvent:
        app = _FakeApp()

    return _FakeEvent()


def test_copy_output_copies_whole_transcript_when_no_selection() -> None:
    transcript = OutputTranscript("line one\nline two\n")
    output_buffer = Buffer(read_only=True)
    output_buffer.set_document(
        Document(transcript.text, cursor_position=len(transcript.text)),
        bypass_readonly=True,
    )
    clipboard = InMemoryClipboard()
    copied: list[int] = []

    ok = _copy_output(_fake_event(clipboard), output_buffer, transcript, lambda n: copied.append(n))

    assert ok is True
    assert clipboard.get_data().text == "line one\nline two\n"
    assert copied == [len("line one\nline two\n")]


def test_copy_output_copies_selection_when_present() -> None:
    transcript = OutputTranscript("hello world\n")
    output_buffer = Buffer(read_only=True)
    output_buffer.set_document(
        Document(transcript.text, cursor_position=len(transcript.text)),
        bypass_readonly=True,
    )
    # 选中 "world"：original_cursor_position=6，光标到 11。
    output_buffer.selection_state = SelectionState(original_cursor_position=6)
    output_buffer.cursor_position = 11
    clipboard = InMemoryClipboard()

    ok = _copy_output(_fake_event(clipboard), output_buffer, transcript)

    assert ok is True
    assert clipboard.get_data().text == "world"
