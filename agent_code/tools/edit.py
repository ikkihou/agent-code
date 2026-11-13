"""File write/edit tools: file_write, file_edit."""

from __future__ import annotations

from typing import Any

from ..file_history import backup
from ..fs_safety import apply_single_replace, read_text_file, resolve_in_cwd, write_text_file
from .core import Tool, ToolContext


def file_write(args: dict[str, Any], ctx: ToolContext) -> str:
    """Overwrite a whole file. Upfront validation happens in agent.py's intercept block."""
    path_str = args.get("file_path", "")
    content = args.get("content", "")

    if not path_str:
        return "error: missing required argument 'file_path'"

    try:
        path = resolve_in_cwd(ctx.cwd, path_str)
    except ValueError as e:
        return f"error: {e}"

    path.parent.mkdir(parents=True, exist_ok=True)
    # The model supplies the content (str), always persisted as UTF-8; existing
    # files are backed up using the tolerant read path.
    if path.exists():
        try:
            old, _ = read_text_file(path)
            backup(ctx.cwd, path, old)
        except Exception:
            pass
    write_text_file(path, content, "utf-8")
    ctx.read_state.record(path, content)

    return f"Wrote {len(content)} chars to {path_str}"


def file_edit(args: dict[str, Any], ctx: ToolContext) -> str:
    """String-replacement edit. Upfront validation happens in agent.py's intercept block."""
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
        # Read tolerantly over the candidate encodings (the file may be GBK) and
        # remember the actual encoding so we write it back unchanged -- otherwise one
        # edit would silently rewrite a GBK file as UTF-8.
        content, encoding = read_text_file(path)
        backup(ctx.cwd, path, content)
    except (FileNotFoundError, IsADirectoryError) as exc:
        return f"error: {exc}"

    # Race guard: agent.py already ran apply_single_replace to build the diff; if an
    # external writer touched old_content between the confirm prompt and now, this
    # catches it once more.
    new_content, err = apply_single_replace(
        content, old_string, new_string, replace_all
    )
    if err:
        return err
    assert new_content is not None

    write_text_file(path, new_content, encoding)
    ctx.read_state.record(path, new_content)
    return f"Edited {path_str}: replaced {len(old_string)} chars with {len(new_string)} chars"


def tools() -> list[Tool]:
    """Registration metadata for this module's tools -- kept next to the implementation."""
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
