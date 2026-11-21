from __future__ import annotations

from typing import Any

from agent_code import prompt_ui
from agent_code.runtime import RuntimeState


def test_confirm_command_uses_structured_request_for_interactive_asker() -> None:
    requests: list[dict[str, Any]] = []

    def asker(request: Any) -> bool:
        requests.append(request)
        return True

    state = RuntimeState(asker=asker)
    assert prompt_ui.confirm_command(state, "echo hi") is True

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

    def asker(request: Any) -> str:
        requests.append(request)
        return "2"

    state = RuntimeState(asker=asker)
    assert prompt_ui.prompt_single_choice(state, "Pick one", ["A", "B"]) == "B"

    assert requests == [
        {
            "type": "choice",
            "question": "Pick one",
            "labels": ["A", "B"],
            "default": 0,
        }
    ]


def test_confirm_edit_passes_detail_in_request() -> None:
    requests: list[dict[str, Any]] = []

    def asker(request: Any) -> bool:
        requests.append(request)
        return True

    state = RuntimeState(asker=asker)
    assert prompt_ui.confirm_edit(state, "a.py", detail="--- a/a.py\n+++ b/a.py\n") is True

    assert requests == [
        {
            "type": "confirm",
            "message": "Apply this edit to a.py?",
            "default": False,
            "detail": "--- a/a.py\n+++ b/a.py\n",
        }
    ]
