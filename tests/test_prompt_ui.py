from __future__ import annotations

from typing import Any

from agent_code import prompt_ui


def test_confirm_command_uses_structured_request_for_interactive_asker() -> None:
    requests: list[dict[str, Any]] = []

    def asker(request: dict[str, Any]) -> bool:
        requests.append(request)
        return True

    prompt_ui.set_terminal_asker(asker)
    try:
        assert prompt_ui.confirm_command("echo hi") is True
    finally:
        prompt_ui.set_terminal_asker(None)

    assert requests == [
        {
            "type": "confirm",
            "message": "Run this command?",
            "default": False,
            "detail": "echo hi",
        }
    ]


def test_prompt_single_choice_uses_structured_request_for_interactive_asker() -> None:
    requests: list[dict[str, Any]] = []

    def asker(request: dict[str, Any]) -> str:
        requests.append(request)
        return "2"

    prompt_ui.set_terminal_asker(asker)
    try:
        assert prompt_ui.prompt_single_choice("Pick one", ["A", "B"]) == "B"
    finally:
        prompt_ui.set_terminal_asker(None)

    assert requests == [
        {
            "type": "choice",
            "question": "Pick one",
            "labels": ["A", "B"],
            "default": 0,
        }
    ]
