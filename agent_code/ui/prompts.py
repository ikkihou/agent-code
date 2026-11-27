"""Rendering and option building for prompt (confirm/choice) requests.

Pure functions that turn the worker's structured ``PromptRequest`` into:
- ``render_prompt_request``: Live-area preview body (diff, command, plan panel);
- ``prompt_for_request``: the question header shown in ``QuestionLine``;
- ``prompt_options``: the ``(hotkey, label)`` choices and default index for ``OptionList``.
"""

from __future__ import annotations

from ..runtime import PromptRequest


def render_prompt_request(request: PromptRequest) -> str:
    """Content shown in the Live area *above* the question line: diff, command, or plan
    panel (``body`` / ``detail``).

    Selectable options are rendered in the interactive ``OptionList`` (via
    ``prompt_options``), and the question title lives in ``QuestionLine`` (via
    ``prompt_for_request``), so neither is repeated here.
    """
    parts: list[str] = []

    body = str(request.get("body") or "")
    if body:
        parts.append(body.rstrip())

    detail = str(request.get("detail") or "")
    if detail:
        parts.append(detail.rstrip())

    return "\n".join(parts).rstrip() + ("\n" if parts else "")


def prompt_for_request(request: PromptRequest) -> str:
    """The single-line question header shown in ``QuestionLine`` above the option menu."""
    if request.get("type") == "choice":
        question = str(request.get("question") or "Choose:")
        return f" {question}"
    message = str(request.get("message") or "Confirm?")
    return f"{message}"


def prompt_options(request: PromptRequest) -> tuple[list[tuple[str, str]], int]:
    """Return ``([(hotkey, label), ...], default_index)`` for the interactive menu."""
    if request.get("type") == "choice":
        labels = [str(label) for label in (request.get("labels") or [])]
        options = [(str(i), label) for i, label in enumerate(labels, 1)]
        options.append(("0", "Skip / Other"))
        default_raw = int(request.get("default", 0))
        default_index = default_raw if 0 <= default_raw < len(labels) else 0
        return options, default_index

    options = [
        ("y", "Yes"),
        ("n", "No"),
        ("a", "Yes, and auto-accept edits"),
    ]
    default_index = 0 if bool(request.get("default", False)) else 1
    return options, default_index
