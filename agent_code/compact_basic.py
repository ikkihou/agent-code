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


def compact(messages: list[dict[str, Any]], keep: int = 8) -> list[dict[str, Any]]:
    """Deterministic compression of the message history. Never calls an LLM.

    Returns three concatenated parts:
    1. pinned: the first 2 messages (they define the session's task; never dropped)
    2. compressed: one summary user message covering the middle
    3. working: the last `keep` messages (recent interaction, kept intact)

    When the total message count is <= keep + 2, nothing is compressed and the
    original list is returned as-is.
    """

    pin_count = 2
    if len(messages) <= keep + pin_count:
        return messages

    pinned = messages[:pin_count]
    working = messages[-keep:]
    middle = messages[pin_count:-keep]
    compressed = _build_compressed_block(middle)
    return pinned + [compressed] + working


def _build_compressed_block(msgs: list[dict[str, Any]]) -> dict[str, Any]:
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
            "</compacted-history>",
        ]
    )
    return {"role": "user", "content": "\n".join(lines)}
