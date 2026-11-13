from __future__ import annotations

#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   types.py
@Time    :   2026/07/19 11:06:22
@Author  :   Paul_Bao
@Version :   1.0
@Contact :   paulbao@mail.ecust.edu.cn
"""

# here put the import lib
import hashlib
import re
from dataclasses import dataclass

# Four memory types: user = durable facts about the user, feedback = how the user
# wants work done, project = what is happening in the project right now,
# reference = pointers to external systems.
MEMORY_TYPES = ("user", "feedback", "project", "reference")


@dataclass
class MemoryEntry:
    """A complete memory record."""

    mem_type: str  # user / feedback / project / reference
    title: str  # human-readable title
    slug: str  # filename-safe identifier
    body: str  # body (markdown)
    file_path: str  # path relative to cwd, e.g. .agent/memory/user/my-role.md


def make_slug(title: str, max_len: int = 64) -> str:
    """Turn a title into a filename-safe slug: keep ASCII alnum plus dashes.

    When the title has no ASCII at all (e.g. pure Chinese/Japanese), fall back to a
    hash slug so the name stays stable across platforms."""
    slug = title.lower().strip()
    # Keep only a-z / 0-9 / spaces / dashes; CJK, emoji and the like are dropped.
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[-\s]+", "-", slug)
    slug = slug.strip("-")[:max_len]
    if not slug:
        # No ASCII at all in the title (e.g. pure Chinese): use the first 8 sha1 chars.
        slug = "mem-" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    return slug
