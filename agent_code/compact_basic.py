"""
@File    :   compact_basic.py
@Time    :   2026/07/19 10:22:14
@Author  :   Paul_Bao
@Version :   1.0
@Contact :   paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

from typing import Any

from .context import estimate_message_tokens, estimate_messages_tokens

# The first messages define the session's task; compaction never drops them.
_PIN_COUNT = 2


def compact_to_budget(
    messages: list[dict[str, Any]],
    target_tokens: int,
    keep_recent_tokens: int,
) -> list[dict[str, Any]] | None:
    """Compaction entry point for token-budget-driven history management.

    Walks backwards from the end accumulating recent messages up to
    ``keep_recent_tokens``, then picks the closest safe cut at or before that point
    (moving back only -- keeping more is always preferable to orphaning a tool pair).
    Everything between the pinned prefix and the cut is replaced by one summary.

    Returns ``None`` when no safe compaction can free tokens; the caller then skips
    compaction instead of corrupting the history.
    """

    if len(messages) <= _PIN_COUNT + 1:
        return None

    start = _safe_start(messages, _PIN_COUNT)
    if start >= len(messages) - 1:
        return None

    # Leave room for the pinned prefix and the summary, then keep the most recent
    # messages that fit the rest of the target budget. keep_recent_tokens is the
    # caller's preferred tail size; target_tokens caps it overall.
    pinned_tokens = estimate_messages_tokens(messages[:start])
    summary_reserve = min(512, max(64, target_tokens // 10))
    recent_budget = max(
        1,
        min(max(1, keep_recent_tokens), target_tokens - pinned_tokens - summary_reserve),
    )

    cut_hint = _recent_cut(messages, start, recent_budget)
    cut = _safe_cut_at_or_before(messages, cut_hint, start + 1)
    if cut is None:
        return None

    middle = messages[start:cut]
    if not middle:
        return None

    compacted = messages[:start] + [_summarize(middle)] + messages[cut:]
    if estimate_messages_tokens(compacted) >= estimate_messages_tokens(messages):
        return None
    return compacted


# ---------------------------------------------------------------------------
# Safe cut helpers
# ---------------------------------------------------------------------------


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _tool_use_ids(message: dict[str, Any]) -> set[str]:
    if message.get("role") != "assistant":
        return set()
    return {
        str(block["id"])
        for block in _blocks(message)
        if block.get("type") == "tool_use" and block.get("id")
    }


def _tool_result_ids(message: dict[str, Any]) -> set[str]:
    if message.get("role") != "user":
        return set()
    return {
        str(block["tool_use_id"])
        for block in _blocks(message)
        if block.get("type") == "tool_result" and block.get("tool_use_id")
    }


def is_safe_boundary(messages: list[dict[str, Any]], index: int) -> bool:
    """True when splitting *before* ``messages[index]`` leaves every tool pair intact.

    A boundary is safe iff no ``tool_use`` in the prefix ``messages[:index]`` is still
    waiting for its ``tool_result``. That single condition covers both failure modes:
    an assistant message with ``tool_use`` ending the prefix, and a bare
    ``tool_result`` user message starting the suffix.
    """
    if index <= 0 or index >= len(messages):
        return True

    pending: set[str] = set()
    for message in messages[:index]:
        pending |= _tool_use_ids(message)
        pending -= _tool_result_ids(message)
    return not pending


def _safe_start(messages: list[dict[str, Any]], pin_count: int) -> int:
    """Advance the pinned prefix past any tool exchange it would otherwise split."""
    start = pin_count
    while start < len(messages) and not is_safe_boundary(messages, start):
        start += 1
    return start


def _safe_cut_at_or_before(
    messages: list[dict[str, Any]],
    cut: int,
    min_cut: int,
) -> int | None:
    """Move ``cut`` backwards until it is a safe boundary; None if none exists."""
    cut = min(cut, len(messages))
    while cut > min_cut and not is_safe_boundary(messages, cut):
        cut -= 1
    if cut < min_cut or not is_safe_boundary(messages, cut):
        return None
    return cut


def _recent_cut(
    messages: list[dict[str, Any]],
    start: int,
    keep_recent_tokens: int,
) -> int:
    """Index of the earliest recent message that fits within ``keep_recent_tokens``."""
    total = 0
    cut = len(messages)
    for index in range(len(messages) - 1, start - 1, -1):
        tokens = estimate_message_tokens(messages[index])
        if total + tokens > keep_recent_tokens and cut < len(messages):
            break
        total += tokens
        cut = index
    return max(cut, start + 1)


# ---------------------------------------------------------------------------
# Summary block
# ---------------------------------------------------------------------------


def _summarize(msgs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the summary message, with an approximate count of the tokens it freed."""
    before = estimate_messages_tokens(msgs)
    draft = _build_compressed_block(msgs, tokens_freed=0)
    freed = max(0, before - estimate_message_tokens(draft))
    return _build_compressed_block(msgs, tokens_freed=freed)


def _build_compressed_block(
    msgs: list[dict[str, Any]],
    tokens_freed: int | None = None,
) -> dict[str, Any]:
    """Scan the messages being compressed and extract structured statistics."""
    total = len(msgs)
    tool_names: set[str] = set()
    tool_count = 0
    files_read: set[str] = set()
    files_edited: set[str] = set()
    commands: list[str] = []
    previous_summary: str | None = None

    for msg in msgs:
        content = msg.get("content")
        # Detect a <compacted-history> block written by a previous compact pass and
        # keep it verbatim.
        if isinstance(content, str) and content.startswith("<compacted-history>"):
            previous_summary = content
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "tool_use":
                # A tool-use request inside an assistant message.
                tool_count += 1
                name = block.get("name", "")
                if name:
                    tool_names.add(name)
                args = block.get("input", {}) or {}
                # Bucket file operations and commands separately.
                if name in ("read_file",) and args.get("path"):
                    files_read.add(str(args["path"]))
                elif name in ("file_write", "file_edit") and args.get("file_path"):
                    files_edited.add(str(args["file_path"]))
                elif name == "bash" and args.get("command"):
                    cmd = str(args["command"])
                    commands.append(cmd[:80] + "..." if len(cmd) > 80 else cmd)
            elif btype == "tool_result":
                # Tool results in user messages need no extra stats -- tools-used
                # already covers them.
                pass

    lines = ["<compacted-history>"]
    # Embed the previous summary verbatim so a reader can see at a glance that this
    # is compaction round N and earlier context still exists.
    if previous_summary:
        lines.append("  <previous-summary>")
        for ln in previous_summary.splitlines():
            lines.append("    " + ln)
        lines.append("  </previous-summary>")
    lines.extend(
        [
            f"  <message-count>{total}</message-count>",
            f"  <tool-calls>{tool_count}</tool-calls>",
            f"  <tools-used>{', '.join(sorted(tool_names)) if tool_names else '(none)'}</tools-used>",
            f"  <files-read>{', '.join(sorted(files_read)) if files_read else '(none)'}</files-read>",
            f"  <files-edited>{', '.join(sorted(files_edited)) if files_edited else '(none)'}</files-edited>",
            f"  <commands-run>{', '.join(commands[:20]) if commands else '(none)'}</commands-run>",
            "  <conclusions>(not yet supported — see Day 11)</conclusions>",
        ]
    )
    if tokens_freed is not None:
        lines.append(f"  <context-note>approximately {tokens_freed} tokens freed</context-note>")
    lines.append("</compacted-history>")
    return {"role": "user", "content": "\n".join(lines)}
