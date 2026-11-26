"""Small, timeout-bounded git probe for the footer (branch + dirty marker)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitInfo:
    """The footer-relevant slice of a repository's state."""

    branch: str | None = None
    dirty: bool = False


def _parse_branch(header: str) -> str | None:
    if not header.startswith("## "):
        return None
    name = header[3:].split("...", 1)[0].strip()
    if name.startswith("No commits yet on "):
        name = name.removeprefix("No commits yet on ").strip()
    if not name or name.startswith("HEAD "):
        return None
    return name


def read_git(cwd: Path | None, *, timeout: float = 0.4) -> GitInfo:
    """Return the current branch and whether tracked files have changes.

    The footer runs on the UI thread, so this is one short-lived command capped by
    ``timeout``; any failure (not a repository, git missing, huge index) degrades to
    "no branch" instead of blocking or raising.
    """
    if cwd is None:
        return GitInfo()
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=no"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return GitInfo()
    if result.returncode != 0:
        return GitInfo()

    lines = result.stdout.splitlines()
    branch = _parse_branch(lines[0]) if lines else None
    dirty = any(line.strip() for line in lines[1:])
    return GitInfo(branch=branch, dirty=dirty)
