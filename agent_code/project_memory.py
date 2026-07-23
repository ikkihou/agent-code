#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   project_memory.py
@Time    :   2026/07/18 19:27:41
@Author  :   Paul_Bao
@Version :   1.0
@Contact :   paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations
from pathlib import Path

_MAX_AGENT_MD_BYTES = 50 * 1024


def load_agent_md(cwd: Path) -> str | None:
    agent_md = cwd / "AGENT.md"
    if not agent_md.exists():
        return None

    content = agent_md.read_text(encoding="utf-8", errors="replace").strip()
    if not content:
        return None

    if len(content.encode("utf-8")) > _MAX_AGENT_MD_BYTES:
        truncated = content.encode("utf-8")[:_MAX_AGENT_MD_BYTES].decode(
            "utf-8", errors="replace"
        )
        content = truncated + "\n\n[... AGENT.md truncated at 50 KB ...]"

    return f"<project-rules>\n{content}\n</project-rules>"
