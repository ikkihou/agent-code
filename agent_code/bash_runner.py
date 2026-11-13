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

from .fs_safety import decode_subprocess_output, truncate_output

# Minimal environment: child processes inherit only the four variables a shell
# needs, not the full user environment. bg_manager.py reuses this dict, so changing
# it here affects both the synchronous and background paths.
MINIMAL_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", ""),
    "USER": os.environ.get("USER", ""),
    "SHELL": os.environ.get("SHELL", "/bin/bash"),
}

# bash output cap: command echoes tend to be longer than file contents, so this is
# more generous than the usual observation cap (8000).
_BASH_OUTPUT_MAX_CHARS = 12000


def run_sync(command: str, cwd: Path, timeout: int = 30) -> str:
    """
    Run a shell command synchronously. cwd is pinned to the project directory and the
    process is killed on timeout.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            env=MINIMAL_ENV,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout}s"

    output = decode_subprocess_output(proc.stdout)
    if proc.stderr:
        stderr_text = decode_subprocess_output(proc.stderr)
        if stderr_text.strip():
            output += "\n[stderr]\n" + stderr_text

    truncated = truncate_output(output.strip(), max_chars=_BASH_OUTPUT_MAX_CHARS)

    if proc.returncode != 0:
        return f"exit code {proc.returncode}\n{truncated}"
    return truncated if truncated else "(no output)"
