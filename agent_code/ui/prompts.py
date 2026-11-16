"""Rendering and answer parsing for prompt (confirm/choice) requests.

A set of pure functions: they turn the worker's structured PromptRequest into Live
area preview text / an input prompt, and parse the user's input back into a structured
answer.
"""

from __future__ import annotations

from ..runtime import PromptRequest


def parse_confirm_answer(text: str, default: bool = False) -> bool | None:
    normalized = text.strip().lower()
    if not normalized:
        return default
    if normalized in {"y", "yes"}:
        return True
    if normalized in {"n", "no"}:
        return False
    return None


def parse_choice_index(text: str, option_count: int, default: int = 0) -> int | None:
    normalized = text.strip()
    if not normalized:
        return default
    try:
        idx = int(normalized)
    except ValueError:
        return None
    if 0 <= idx <= option_count:
        return idx
    return None


def render_prompt_request(request: PromptRequest) -> str:
    """Content shown in the Live area *below* the question line: options, diff,
    command, or the plan panel (``body``).

    The question line itself (``? ... [y/N]``) comes from ``prompt_for_request`` and
    lives in the input-box message -- this function must not repeat it, or the
    question renders twice. An empty string (no content) is valid: a bare confirm
    shows only the question line.
    """
    request_type = request.get("type")
    parts: list[str] = []

    body = str(request.get("body") or "")
    if body:
        parts.append(body.rstrip())

    if request_type == "choice":
        labels = request.get("labels") or []
        parts.extend(f"  {i}. {label}" for i, label in enumerate(labels, 1))
        parts.append("  0. Skip / Other")
    else:
        detail = str(request.get("detail") or "")
        if detail:
            parts.append(detail.rstrip())

    return "\n".join(parts).rstrip() + ("\n" if parts else "")


def prompt_for_request(request: PromptRequest) -> str:
    """The single-line prompt shown in the input box, cursor right after it.

    Rendered as the prompt ``message`` while a question is pending, so the question
    and the input line are one unit (Claude-Code style) instead of the question
    floating in the Live area above an unrelated ``> ``.
    """
    if request.get("type") == "choice":
        question = str(request.get("question") or "Choose:")
        labels = request.get("labels") or []
        default = int(request.get("default", 0))
        return f"? {question} (1-{max(len(labels), 1)}, default {default + 1}): "
    default = bool(request.get("default", False))
    message = str(request.get("message") or "Confirm?")
    suffix = "[Y/n]" if default else "[y/N]"
    return f"? {message} {suffix}: "
