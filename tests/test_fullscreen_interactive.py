from __future__ import annotations

from pathlib import Path
from typing import Any

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent_code.agent import run_agent
from agent_code.interactive import (
    OutputTranscript,
    SlashCompleter,
    append_output_text,
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


def test_append_output_text_keeps_plain_transcript() -> None:
    transcript = OutputTranscript("first\n")

    append_output_text(transcript, "second\n")

    assert transcript.text == "first\nsecond\n"
    assert transcript.line_count == 2
    assert transcript.fragments == [("", "first\nsecond\n")]


def test_append_output_text_preserves_ansi_style_fragments() -> None:
    transcript = OutputTranscript()

    append_output_text(transcript, OutputChunk("\x1b[1mBold\x1b[0m plain\n", format="ansi"))

    assert transcript.text == "Bold plain\n"
    assert ("bold", "Bold") in transcript.fragments
    assert ("", " plain\n") in transcript.fragments


def test_render_user_prompt_formats_prompt_blocks() -> None:
    assert render_user_prompt("hello") == "\n[user]\n> hello\n\n"
    assert render_user_prompt("first\nsecond") == "\n[user]\n> first\n> second\n\n"
    assert render_user_prompt("expanded", source="user /slash") == (
        "\n[user /slash]\n> expanded\n\n"
    )
    assert render_user_prompt("") == ""


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
