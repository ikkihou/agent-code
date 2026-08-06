"""文件写入/编辑工具：file_write、file_edit。"""

from __future__ import annotations

from typing import Any

from ..file_history import backup
from ..fs_safety import apply_single_replace, resolve_in_cwd
from .core import Tool, ToolContext


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


def tools() -> list[Tool]:
    """本模块工具的注册元数据——与实现就近存放。"""
    return [
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
        ),
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
        ),
    ]
