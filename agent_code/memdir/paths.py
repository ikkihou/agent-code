from __future__ import annotations

#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   paths.py
@Time    :   2026/07/19 10:56:32
@Author  :   Paul_Bao
@Version :   1.0
@Contact :   paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from pathlib import Path

MEMORY_DIR = ".agent/memory"
INDEX_FILE = "MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024


def get_memdir(cwd: Path) -> Path:
    return cwd / MEMORY_DIR


def ensure_memdir(cwd: Path) -> Path:
    memdir = get_memdir(cwd)
    memdir.mkdir(parents=True, exist_ok=True)
    for sub in ("user", "feedback", "project", "reference"):
        (memdir / sub).mkdir(exist_ok=True)

    return memdir


def index_path(cwd: Path) -> Path:
    return get_memdir(cwd) / INDEX_FILE


def topic_path(cwd: Path, mem_type: str, slug: str) -> Path:
    return get_memdir(cwd) / mem_type / f"{slug}.md"
