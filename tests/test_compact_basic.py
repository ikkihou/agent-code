"""Pairing-safe deterministic compaction.

The hard invariant: a ``tool_use`` block is never separated from its ``tool_result``,
no matter where the greedy cut lands. Anthropic-compatible endpoints reject orphaned
tool results, so these are protocol tests, not cosmetic ones.
"""

from __future__ import annotations

from typing import Any

from agent_code.compact_basic import (
    compact,
    compact_to_budget,
    is_safe_boundary,
)
from agent_code.context import estimate_messages_tokens

# ---- history builders ------------------------------------------------------


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _assistant_tool(tool_id: str, name: str = "read_file") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "reading"},
            {"type": "tool_use", "id": tool_id, "name": name, "input": {"path": "a.txt"}},
        ],
    }


def _tool_result(tool_id: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
    }


def _assert_no_orphans(messages: list[dict[str, Any]]) -> None:
    """Every tool_result answers a tool_use before it; no tool_use is left pending."""
    pending: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                pending.add(str(block.get("id")))
            elif block.get("type") == "tool_result":
                tool_id = str(block.get("tool_use_id"))
                assert tool_id in pending, f"orphaned tool_result: {tool_id}"
                pending.discard(tool_id)
    assert not pending, f"orphaned tool_use: {pending}"


def _tool_heavy_history() -> list[dict[str, Any]]:
    """12 messages whose greedy keep=2 cut lands on a bare tool_result."""
    return [
        _user("task"),
        _assistant("sure"),
        _user("one"),
        _assistant("two"),
        _user("three"),
        _assistant("four"),
        _user("five"),
        _assistant("six"),
        _user("seven"),
        _assistant_tool("a"),
        _tool_result("a"),  # messages[-2]: the greedy cut starts here
        _user("next"),
    ]


# ---- boundary detection ----------------------------------------------------


def test_boundary_between_tool_use_and_result_is_unsafe() -> None:
    messages = [_user("task"), _assistant_tool("a"), _tool_result("a"), _user("next")]

    assert not is_safe_boundary(messages, 2)  # splits tool_use from its result
    assert is_safe_boundary(messages, 1)  # before the assistant tool_use
    assert is_safe_boundary(messages, 3)  # after the pair is complete
    assert is_safe_boundary(messages, 0)
    assert is_safe_boundary(messages, len(messages))


def test_boundary_is_safe_after_a_complete_pair() -> None:
    messages = [
        _user("task"),
        _assistant("ok"),
        _assistant_tool("a"),
        _tool_result("a"),
        _user("next"),
    ]
    assert is_safe_boundary(messages, 4)


# ---- compact(): compatibility wrapper --------------------------------------


def test_compact_returns_input_when_history_is_short() -> None:
    messages = [_user("a"), _assistant("b"), _user("c")]
    assert compact(messages, keep=8) is messages


def test_compact_moves_the_cut_back_to_keep_a_tool_pair() -> None:
    messages = _tool_heavy_history()
    compacted = compact(messages, keep=2)

    _assert_no_orphans(compacted)
    # The cut moved from index 10 back to 9: the whole tool exchange is kept.
    assert compacted[-3]["role"] == "assistant"
    assert compacted[-2]["role"] == "user"
    assert any(
        "<compacted-history>" in str(message.get("content"))
        for message in compacted
        if message["role"] == "user"
    )


def test_compact_regression_previously_orphaned_a_tool_result() -> None:
    """The old count-based cut produced ``[tool_result, ...]`` with no tool_use.

    This is the regression the safe-cut logic fixes: the result survives and so does
    the assistant message that requested it.
    """
    messages = _tool_heavy_history()
    old_style_working = messages[-2:]

    assert old_style_working[0]["content"][0]["type"] == "tool_result"
    _assert_no_orphans(messages[-3:])  # the repaired window keeps the pair

    compacted = compact(messages, keep=2)
    _assert_no_orphans(compacted)
    # The tool exchange survived even though the greedy cut started after it.
    assert compacted[-3] == messages[-3]
    assert compacted[-2] == messages[-2]


# ---- compact_to_budget -----------------------------------------------------


def test_compact_to_budget_returns_none_for_short_history() -> None:
    messages = [_user("task"), _assistant("ok"), _user("more")]
    assert compact_to_budget(messages, target_tokens=100, keep_recent_tokens=50) is None


def test_compact_to_budget_summarizes_the_middle_and_keeps_recent() -> None:
    messages = [
        _user("task"),
        _assistant("acknowledged"),
        *[_user(f"middle {i} " + "x" * 400) for i in range(6)],
        _user("most recent"),
    ]
    compacted = compact_to_budget(messages, target_tokens=200, keep_recent_tokens=60)

    assert compacted is not None
    assert compacted[0] == messages[0]
    assert compacted[1] == messages[1]
    assert compacted[2]["role"] == "user"
    summary = str(compacted[2]["content"])
    assert summary.startswith("<compacted-history>")
    assert "<context-note>" in summary
    assert "tokens freed" in summary
    assert compacted[-1] == messages[-1]
    assert len(compacted) < len(messages)
    assert estimate_messages_tokens(compacted) < estimate_messages_tokens(messages)


def test_compact_to_budget_keeps_previous_summary_when_recompacting() -> None:
    messages = [
        _user("task"),
        _assistant("acknowledged"),
        *[_user(f"middle {i} " + "x" * 400) for i in range(6)],
        _user("most recent"),
    ]
    first = compact_to_budget(messages, target_tokens=200, keep_recent_tokens=60)
    assert first is not None

    grown = [
        *first,
        *[_user(f"later {i} " + "y" * 400) for i in range(4)],
        _user("most recent"),
    ]
    second = compact_to_budget(grown, target_tokens=200, keep_recent_tokens=60)
    assert second is not None
    summary = str(second[2]["content"])
    assert "<previous-summary>" in summary


def test_compact_to_budget_returns_none_when_no_safe_cut_exists() -> None:
    # Everything after the pin is one unbreakable tool exchange; moving the cut back
    # to the only safe boundary would leave no middle to summarize.
    messages = [
        _user("task"),
        _assistant_tool("a"),
        _tool_result("a"),
        _assistant_tool("b"),
        _tool_result("b"),
    ]
    assert compact_to_budget(messages, target_tokens=100, keep_recent_tokens=10) is None


def test_compact_to_budget_does_not_orphan_a_tool_result() -> None:
    messages = [
        _user("task"),
        _assistant("acknowledged"),
        *[_user(f"middle {i} " + "x" * 400) for i in range(4)],
        _assistant_tool("a"),
        _tool_result("a"),
        _user("next"),
    ]
    compacted = compact_to_budget(messages, target_tokens=200, keep_recent_tokens=10)

    assert compacted is not None
    _assert_no_orphans(compacted)
