from __future__ import annotations

#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
@File    :   hooks.py
@Time    :   2026/07/19 23:21:01
@Author  :   Paul_Bao
@Version :   1.0
@Contact :   paulbao@mail.ecust.edu.cn
"""

# here put the import lib
import json
import subprocess
from pathlib import Path
from typing import Any

HOOKS_FILE = "hooks.json"


def load_hooks(cwd: Path) -> dict[str, list[dict[str, Any]]]:
    file_path = cwd / HOOKS_FILE
    if not file_path.exists():
        return {}

    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("hooks", data)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[hook warning] failed to load {file_path}: {exc}")
        return {}


def _matches(tool_name: str, matcher: str) -> bool:
    if matcher == "*":
        return True

    if "|" in matcher:
        return tool_name in matcher.split("|")

    return matcher == tool_name


def _run_hook_command(
    command: str, input_data: dict[str, Any], cwd: Path, timeout: int = 30
) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            input=json.dumps(input_data, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = result.stdout.strip() or result.stderr.strip()
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
    """在给定 event 下执行所有匹配 tool_name 的 hooks。
    返回一个 list，每个元素是 {"event": ..., "tool": ..., "success": bool, "output": str}。
    空 list 表示没有匹配到 hook。

    这是 harness 的 hook dispatch 入口——agent.py 在工具前后调用本函数。"""
    config = load_hooks(cwd)
    entries = config.get(event, [])
    results: list[dict[str, Any]] = []
    for entry in entries:
        matcher = entry.get("matcher", "*")
        if not _matches(tool_name, matcher):
            continue
        # 支持两种格式："run" 单命令，或 "hooks"[].command 多命令。
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
