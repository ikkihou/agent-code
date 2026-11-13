"""提问（confirm/choice）请求的渲染与答案解析。

纯函数集合：把 worker 的结构化 PromptRequest 变成 Live 区预览文本 /
输入提示符，并把用户输入解析回结构化答案。
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
    request_type = request.get("type")
    body = str(request.get("body") or "")
    parts: list[str] = []
    if body:
        parts.append(body.rstrip())

    if request_type == "choice":
        question = str(request.get("question") or "")
        labels = request.get("labels") or []
        parts.append(f"? {question}")
        parts.extend(f"  {i}. {label}" for i, label in enumerate(labels, 1))
        parts.append("  0. Skip / Other")
    else:
        message = str(request.get("message") or "Confirm?")
        default = bool(request.get("default", False))
        suffix = "[Y/n]" if default else "[y/N]"
        parts.append(f"? {message} {suffix}")

    return "\n".join(parts).rstrip() + "\n"


def prompt_for_request(request: PromptRequest) -> str:
    if request.get("type") == "choice":
        default = int(request.get("default", 0))
        return f"Choice [{default}]: "
    default = bool(request.get("default", False))
    return "Confirm [Y/n]: " if default else "Confirm [y/N]: "
