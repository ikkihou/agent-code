from __future__ import annotations

from pathlib import Path
from typing import Any

from prompt_toolkit.widgets import TextArea

from agent_code.agent import run_agent
from agent_code.interactive import (
    append_output_text,
    parse_choice_index,
    parse_confirm_answer,
    prompt_for_request,
    render_prompt_request,
)
from agent_code.model import ModelResponse, ModelStreamEvent
from agent_code.runtime import RuntimeState
from agent_code.tools import ToolRegistry


def test_append_output_text_keeps_transcript_cursor_at_bottom() -> None:
    output_area = TextArea(text="first\n", read_only=True)

    append_output_text(output_area, "second\n")

    assert output_area.text == "first\nsecond\n"
    assert output_area.buffer.cursor_position == len(output_area.text)


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
    output: list[str] = []

    result = run_agent(
        "介绍一下自己",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        output=output.append,
    )

    assert result.final == final_text
    assert "".join(output) == final_text + "\n"
