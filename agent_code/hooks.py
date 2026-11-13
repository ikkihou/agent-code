"""
@File    :   hooks.py
@Time    :   2026/07/19 23:21:01
@Author  :   Paul_Bao
@Version :   1.0
@Contact :   paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from rich.console import Console

from .fs_safety import decode_subprocess_output

HOOKS_FILE = "hooks.json"
console = Console()


def _decode_hook_output(data: bytes | None) -> str:
    return decode_subprocess_output(data)


def load_hooks(cwd: Path) -> dict[str, list[dict[str, Any]]]:
    file_path = cwd / HOOKS_FILE
    if not file_path.exists():
        return {}

    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("hooks", data)
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"[yellow][hook warning] failed to load {file_path}: {exc}[/yellow]")
        return {}


def _matches(tool_name: str, matcher: str) -> bool:
    """How matcher is matched against tool_name:
    - "*" matches every tool
    - a single value matches exactly
    - "a|b" matches any of several values
    No regex or glob support -- keep it simple and predictable."""
    if matcher == "*":
        return True

    if "|" in matcher:
        return tool_name in matcher.split("|")

    return matcher == tool_name


def _run_hook_command(
    command: str, input_data: dict[str, Any], cwd: Path, timeout: int = 30
) -> tuple[bool, str]:
    try:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            input=json.dumps(input_data, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )

        output = _decode_hook_output(result.stdout).strip() or _decode_hook_output(
            result.stderr
        ).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "hook time out"
    except Exception as e:
        return False, f"hook execution error: {e}"


def run_hooks(
    event: str,
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: Path,
    tool_result: str = "",
) -> list[dict[str, Any]]:
    """Run every hook matching tool_name for the given event.
    Returns a list whose elements are
    {"event": ..., "tool": ..., "success": bool, "output": str}.
    An empty list means no hook matched.

    This is the harness hook dispatch entry point -- agent.py calls it before and
    after each tool run."""
    config = load_hooks(cwd)
    entries = config.get(event, [])
    results: list[dict[str, Any]] = []
    for entry in entries:
        matcher = entry.get("matcher", "*")
        if not _matches(tool_name, matcher):
            continue
        # Two accepted shapes: a single "run" command, or "hooks"[].command entries.
        commands: list[str] = []
        if "run" in entry:
            commands = [entry["run"]]
        elif "hooks" in entry:
            for h in entry["hooks"]:
                if isinstance(h, dict) and h.get("type") == "command":
                    cmd = h.get("command", "")
                    if cmd:
                        commands.append(cmd)
        for cmd in commands:
            input_data = {
                "event": event,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_result": tool_result,
                "cwd": str(cwd),
            }
            success, output = _run_hook_command(cmd, input_data, cwd)
            results.append(
                {
                    "event": event,
                    "tool": tool_name,
                    "command": cmd,
                    "success": success,
                    "output": output,
                }
            )
    return results


def run_hooks_raw(
    event: str, payload: dict[str, Any], cwd: Path
) -> list[dict[str, Any]]:
    """Run hooks that have no tool context (e.g. Stop). The whole payload is passed to
    the hook as stdin JSON."""
    config = load_hooks(cwd)
    results: list[dict[str, Any]] = []
    for entry in config.get(event, []):
        matcher = entry.get("matcher", "*")
        if matcher not in ("*", ""):  # Stop has no tool_name; only * / empty match
            continue
        commands: list[str] = []
        if "run" in entry:
            commands = [entry["run"]]
        else:
            for h in entry.get("hooks", []):
                if (
                    isinstance(h, dict)
                    and h.get("type") == "command"
                    and h.get("command")
                ):
                    commands.append(h["command"])
        for cmd in commands:
            success, output = _run_hook_command(cmd, payload, cwd)
            results.append(
                {"event": event, "command": cmd, "success": success, "output": output}
            )
    return results
