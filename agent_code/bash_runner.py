#!/usr/bin/env python3
"""
##
##       filename: bash_runner.py
##        created: 2026/07/15
##         author: Paul_Bao
##            IDE: Neovim
##       Version : 1.0
##       Contact : paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

import os
import subprocess
from pathlib import Path


from .fs_safety import truncate_output

_MININAL_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", ""),
    "USER": os.environ.get("USER", ""),
    "SHELL": os.environ.get("SHELL", ""),
}


def run_sync(command: str, cwd: Path, timeout: int = 30) -> str:
    """
    同步执行 shell 命令。cwd 锁定项目目录，超时后杀进程。
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            env=_MININAL_ENV,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout}s"

    output = proc.stdout.decode(encoding="utf-8", errors="replace")
    if proc.stderr:
        stderr_text = proc.stderr.decode(encoding="utf-8", errors="replace")
        if stderr_text.strip():
            output += "\n[stderr]\n" + stderr_text

    truncated = truncate_output(output.strip(), max_chars=12000)

    if proc.returncode != 0:
        return f"exit code {proc.returncode}\n{truncated}"
    return truncated if truncated else "(no output)"
