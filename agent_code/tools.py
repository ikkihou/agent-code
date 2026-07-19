#!/usr/bin/env python3
"""
##
##       filename: tools.py
##        created: 2026/06/14
##         author: Paul_Bao
##            IDE: Neovim
##       Version : 1.0
##       Contact : paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

from dataclasses import dataclass, field
from turtle import back
from typing import Any, Callable, List
from datetime import datetime
from pathlib import Path
import subprocess
import shutil
import re

from .model import ToolCall, ToolResult
from .fs_safety import (
    SkipPolicy,
    ReadFileState,
    resolve_in_cwd,
    ensure_text_file,
    ensure_within_size,
    should_skip,
    truncate_output,
    apply_single_replace,
)
from .file_history import backup
from .bash_runner import run_sync as _bash_run_sync


@dataclass
class ToolContext:
    cwd: Path
    skip_policy: SkipPolicy = field(default_factory=SkipPolicy.default)
    read_state: ReadFileState = field(default_factory=ReadFileState)


ToolFunc = Callable[
    [dict[str, Any], ToolContext],
    str,
]


@dataclass
class Tool:
    name: str
    description: str
    run: ToolFunc
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )


# ---------------------------------------------


def echo(args: dict[str, Any], context: ToolContext) -> str:
    return str(args.get("text", ""))


def system_date(args: dict[str, Any], context: ToolContext) -> str:
    # system_date 是模型看不到系统时钟时，需要向 harness 请求的能力。
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def read_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path_str = args.get("path", "")
    if not path_str:
        return "error: missing required argument 'path'"
    try:
        path = resolve_in_cwd(ctx.cwd, path_str)
        ensure_text_file(path)
        ensure_within_size(path)
        text = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, ValueError) as exc:
        return f"error: {exc}"
    # 记录"模型看到的版本"，给 Day 4 read-before-edit 留底。
    ctx.read_state.record(path, text)
    return truncate_output(text)


def list_files(args: dict[str, Any], ctx: ToolContext) -> str:
    path_str = args.get("path", ".")
    try:
        base = resolve_in_cwd(ctx.cwd, path_str)
    except ValueError as exc:
        return f"error: {exc}"
    if not base.is_dir():
        return f"error: not a directory: {path_str}"
    entries: list[str] = []
    for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        rel = child.relative_to(ctx.cwd)
        if should_skip(rel, ctx.skip_policy):
            continue
        entries.append(f"{child.name}/" if child.is_dir() else child.name)
    return truncate_output("\n".join(entries) or "(empty)")


def glob(args: dict[str, Any], ctx: ToolContext) -> str:
    pattern = args.get("pattern", "")
    if not pattern:
        return "error: missing required argument 'pattern'"

    matches: list[Path] = []
    try:
        for path in ctx.cwd.rglob(pattern):
            rel = path.relative_to(ctx.cwd)
            if should_skip(rel, ctx.skip_policy):
                continue
            matches.append(path)
    except NotImplementedError as exc:
        return f"error: {exc}"
    # 按 mtime 倒序，让"最近改过的文件"排在前面。
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    matches = matches[:200]

    lines = [str(p.relative_to(ctx.cwd)) for p in matches]
    return truncate_output("\n".join(lines) or "(no matches)")


def grep(args: dict[str, Any], ctx: ToolContext) -> str:
    pattern = args.get("pattern", "")
    if not pattern:
        return "error: missing required argument 'pattern'"
    path_arg = args.get("path", ".")
    glob_arg = args.get("glob")
    ignore_case = bool(args.get("ignore_case", False))

    try:
        base = resolve_in_cwd(ctx.cwd, path_arg)
    except ValueError as exc:
        return f"error: {exc}"

    # 系统装了 ripgrep 就走它，速度差一个数量级；否则退化纯 Python。
    if shutil.which("rg"):
        return _grep_ripgrep(pattern, base, glob_arg, ignore_case, ctx)
    return _grep_python(pattern, base, glob_arg, ignore_case, ctx)


def _grep_ripgrep(
    pattern: str,
    base: Path,
    glob_arg: str | None,
    ignore_case: bool,
    ctx: ToolContext,
) -> str:
    # ripgrep 自带 .gitignore 解析和 VCS 目录跳过，我们只需要追加自定义 skip。
    args: list[str] = ["rg", "--line-number", "--no-heading", "--max-columns", "500"]
    if ignore_case:
        args.append("-i")
    for name in ctx.skip_policy.skip_dirs:
        args.extend(["--glob", f"!{name}/**"])
    if glob_arg:
        args.extend(["--glob", glob_arg])
    args.append(pattern)
    # rg 必须收绝对路径才能让 --glob 的相对规则可预测；
    # 但输出给模型前要把每行的绝对前缀切回相对路径，省 token、和 _grep_python 保持一致。
    args.append(str(base))
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"error: {exc}"

    # ripgrep 没匹配会返回 exit code 1，这不是错；真错才看 stderr。
    if proc.returncode not in (0, 1):
        return f"error: rg: {proc.stderr.strip() or proc.returncode}"
    return truncate_output(
        _relativize_rg_output(proc.stdout, ctx.cwd) or "(no matches)"
    )


def _relativize_rg_output(stdout: str, cwd: Path) -> str:
    # rg 每行形如 "/abs/path:lineno:content"。命中 cwd 前缀的就切成相对路径，
    # 不命中（罕见）原样保留，避免吞掉模型可能想看到的诊断信息。
    cwd_prefix = f"{cwd}/"
    lines = [
        line[len(cwd_prefix) :] if line.startswith(cwd_prefix) else line
        for line in stdout.splitlines()
    ]
    return "\n".join(lines).strip()


def _grep_python(
    pattern: str,
    base: Path,
    glob_arg: str | None,
    ignore_case: bool,
    ctx: ToolContext,
) -> str:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"error: invalid regex: {exc}"

    if base.is_file():
        candidates: list[Path] = [base]
    else:
        candidates = []
        try:
            for path in base.rglob(glob_arg or "*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(ctx.cwd)
                if should_skip(rel, ctx.skip_policy):
                    continue
                candidates.append(path)
        except NotImplementedError as exc:
            return f"error: {exc}"

    hits: list[str] = []
    for path in candidates:
        try:
            ensure_text_file(path)
        except ValueError:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(ctx.cwd)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{rel}:{lineno}:{line}")
    return truncate_output("\n".join(hits) or "(no matches)")


def file_write(args: dict[str, Any], ctx: ToolContext) -> str:
    """整文件覆盖写入。前置校验由 agent.py 的拦截块完成。"""
    path_str = args.get("file_path", "")
    content = args.get("content", "")

    if not path_str:
        return "error: missing required argument 'file_path'"

    try:
        path = resolve_in_cwd(ctx.cwd, path_str)
    except ValueError as e:
        return f"error: {e}"

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            old = path.read_text(encoding="utf-8")
            backup(ctx.cwd, path, old)
        except Exception:
            pass
    path.write_text(content, encoding="utf-8")
    ctx.read_state.record(path, content)

    return f"Wrote {len(content)} chars to {path_str}"


def file_edit(args: dict[str, Any], ctx: ToolContext) -> str:
    """字符串替换编辑。前置校验在 agent.py 拦截块里完成。"""
    path_str = args.get("file_path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))
    if not path_str:
        return "error: missing required argument 'file_path'"
    try:
        path = resolve_in_cwd(ctx.cwd, path_str)
    except ValueError as exc:
        return f"error: {exc}"

    try:
        content = path.read_text(encoding="utf-8")
        backup(ctx.cwd, path, content)
    except (FileNotFoundError, IsADirectoryError) as exc:
        return f"error: {exc}"

    # 防 race：agent.py 已经做过一次 apply_single_replace 算 diff，
    # 如果 confirm 那一刻到现在 old_content 又被外部改过，这里会再兜一次。
    new_content, err = apply_single_replace(
        content, old_string, new_string, replace_all
    )
    if err:
        return err
    assert new_content is not None

    path.write_text(new_content, encoding="utf-8")
    ctx.read_state.record(path, new_content)
    return f"Edited {path_str}: replaced {len(old_string)} chars with {len(new_string)} chars"


def _git_status(args: dict[str, Any], ctx: ToolContext) -> str:
    """薄包装 git status——只读、默认 allow。"""
    return _bash_run_sync("git status", ctx.cwd, timeout=10)


def _git_diff(args: dict[str, Any], ctx: ToolContext) -> str:
    """薄包装 git diff——只读、默认 allow。"""
    return _bash_run_sync("git diff", ctx.cwd, timeout=10)


def bash(args: dict[str, Any], ctx: ToolContext) -> str:
    """执行 shell 命令。前置校验和用户确认在 agent.py 拦截块完成。"""
    command = args.get("command", "")
    if not command:
        return "error: missing required argument 'command'"
    timeout = int(args.get("timeout", 30))
    background = bool(args.get("background", False))

    if background:
        from .bg_manager import start_background

        result = start_background(command, ctx.cwd)
        return (
            f"Command running in background with ID: {result['background_id']}.\n"
            f"Output is being written to: {result['output_file']}\n"
            f"Stderr is being written to: {result['stderr_file']}\n"
            f"PID: {result['pid']}\n\n"
            f"{result['message']}"
        )

    return _bash_run_sync(command, ctx.cwd, timeout=timeout)


def _memory_write(args: dict[str, Any], ctx: ToolContext) -> str:
    """写入一条长期记忆——工具函数只做薄包装。"""
    from .memdir.store import write_memory

    mem_type = args.get("type", "")
    title = args.get("title", "")
    body = args.get("body", "")
    if mem_type not in ("user", "feedback", "project", "reference"):
        return "error: type must be one of: user, feedback, project, reference"
    if not title:
        return "error: missing required argument 'title'"
    if not body:
        return "error: missing required argument 'body'"
    try:
        entry = write_memory(ctx.cwd, mem_type, title, body)
        return f"Memory saved: [{entry.mem_type}] {entry.title} -> {entry.file_path}"
    except Exception as exc:
        return f"error: {exc}"


def _memory_recall(args: dict[str, Any], ctx: ToolContext) -> str:
    """关键字搜索长期记忆——工具函数只做薄包装。"""
    from .memdir.store import recall_memory

    query = args.get("query", "")
    top_k = int(args.get("top_k", 5))
    if not query:
        return "error: missing required argument 'query'"
    try:
        entries = recall_memory(ctx.cwd, query, top_k=top_k)
        if not entries:
            return "(no matching memories found)"
        lines = []
        for e in entries:
            snippet = e.body[:200] + ("..." if len(e.body) > 200 else "")
            lines.append(f"## [{e.mem_type}] {e.title}")
            lines.append(f"  file: {e.file_path}")
            lines.append(f"  {snippet}")
            lines.append("")
        return "\n".join(lines)
    except Exception as exc:
        return f"error: {exc}"


def _ask_user_question(args: dict[str, Any], ctx: ToolContext) -> str:
    """由 agent.py 拦截块处理——工具函数本身不读 stdin。
    拦截块识别 call.name == "ask_user_question"，调 prompt_ui 后把结果作为 observation 返回。"""
    prompt = args.get("prompt", "")
    options = args.get("options", [])
    if not prompt:
        return "error: missing required argument 'prompt'"
    if not options or not isinstance(options, list):
        return "error: options must be a non-empty list"
    # 实际交互在 agent.py 拦截块里完成——这里只返回占位。
    # 正常路径不会走到这里，因为拦截块会先处理。
    return (
        "error: ask_user_question must be handled by the harness, not executed directly"
    )


# ---------------------------------------------


class ToolRegistry:
    def __init__(self) -> None:
        # 注册表是工具名和 Python 函数之间的 harness 边界。
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def list(self) -> List[Tool]:
        return list(self._tools.values())

    def run(self, call: ToolCall, context: ToolContext) -> ToolResult:
        # 未知工具也返回 observation，不让 Agent Loop 崩掉。
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"unknown tool: {call.name}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id, content=tool.run(call.arguments, context)
        )


def default_tools() -> ToolRegistry:
    # Day 1 只有一个工具，后面会在这里加文件和 bash 工具。
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="Return the input text.",
            run=echo,
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )
    )
    registry.register(
        Tool(
            name="system_date",
            description="Return the current system date and time.",
            run=system_date,
        )
    )
    registry.register(
        Tool(
            name="read_file",
            description="Read a text file under the current working directory. Argument 'path' is the file path relative to the working directory.",
            run=read_file,
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
    )
    registry.register(
        Tool(
            name="list_files",
            description="List files in a directory under the current working directory. Argument 'path' is the directory path relative to the working directory, default is current directory.",
            run=list_files,
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        )
    )
    registry.register(
        Tool(
            name="glob",
            description="Find files by glob pattern, e.g. '**/*.py'.",
            run=glob,
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "ignore_case": {"type": "boolean"},
                },
                "required": ["pattern"],
            },
        )
    )
    registry.register(
        Tool(
            name="grep",
            description="Search file contents with a regular expression (ripgrep if available).",
            run=grep,
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression."},
                    "path": {
                        "type": "string",
                        "description": "Relative path; defaults to '.'.",
                        "default": ".",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional file glob filter, e.g. '*.py'.",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive match.",
                        "default": False,
                    },
                },
                "required": ["pattern"],
            },
        )
    )
    registry.register(
        Tool(
            name="file_write",
            description="Write or overwrite a file. Path is relative to cwd.",
            run=file_write,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative path inside cwd.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                },
                "required": ["file_path", "content"],
            },
        )
    )
    registry.register(
        Tool(
            name="file_edit",
            description=(
                "Edit a file by replacing old_string with new_string. "
                "old_string must be unique in the file (or use replace_all=True). "
                "You must read the file before editing."
            ),
            run=file_edit,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative path inside cwd.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact string to replace. Must match including whitespace and indentation.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "String to replace it with.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences. Default false.",
                        "default": False,
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        )
    )
    registry.register(
        Tool(
            name="git_status",
            description="Run git status to see the current state of the working directory.",
            run=_git_status,
            parameters={"type": "object", "properties": {}, "required": []},
        )
    )
    registry.register(
        Tool(
            name="git_diff",
            description="Run git diff to see unstaged changes in the working directory.",
            run=_git_diff,
            parameters={"type": "object", "properties": {}, "required": []},
        )
    )
    registry.register(
        Tool(
            name="bash",
            description=(
                "Execute a shell command. Working directory persists but shell state "
                "does not (each call is a fresh shell). timeout in seconds (default 30). "
                "Avoid cd; use the tool's implicit cwd instead."
            ),
            run=bash,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds, default 30.",
                        "default": 30,
                    },
                    "background": {
                        "type": "boolean",
                        "description": "Run in background. Returns immediately with a background_id. Default false.",
                        "default": False,
                    },
                },
                "required": ["command"],
            },
        )
    )
    registry.register(
        Tool(
            name="ask_user_question",
            description=(
                "Ask the user a structured single-choice question. "
                "Use when you need to decide between multiple approaches "
                "or need user preference before proceeding."
            ),
            run=_ask_user_question,
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The question to ask the user. Should end with ?.",
                    },
                    "options": {
                        "type": "array",
                        "description": "List of option labels (2-4 recommended).",
                        "items": {"type": "string"},
                    },
                },
                "required": ["prompt", "options"],
            },
        )
    )
    registry.register(
        Tool(
            name="memory_write",
            description=(
                "Write a fact to long-term memory. Memories persist across sessions. "
                "Use for: user preferences, project conventions, feedback received, "
                "technical references to external systems."
            ),
            run=_memory_write,
            parameters={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference"],
                        "description": "Memory category.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title for this memory.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Full markdown content of the memory.",
                    },
                },
                "required": ["type", "title", "body"],
            },
        )
    )
    registry.register(
        Tool(
            name="memory_recall",
            description=(
                "Search long-term memory by keywords. Returns matching entries with snippets. "
                "Use when you need to recall facts about the user, project, or past decisions."
            ),
            run=_memory_recall,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to search for.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results to return (default 5).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        )
    )
    return registry
