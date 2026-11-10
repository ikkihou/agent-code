"""tools/ 包重构的回归测试：锁定组装结果与公开 API。"""

from __future__ import annotations

from pathlib import Path

from agent_code.fs_safety import decode_subprocess_output
from agent_code.model import ToolCall
from agent_code.tools import ToolContext, ToolRegistry, default_tools

EXPECTED_TOOLS = {
    "echo",
    "system_date",
    "read_file",
    "list_files",
    "glob",
    "grep",
    "project_tree",
    "file_write",
    "file_edit",
    "git_status",
    "git_diff",
    "bash",
    "ask_user_question",
    "memory_write",
    "memory_recall",
    "cron_create",
    "cron_list",
    "cron_cancel",
    "todo_write",
    "todo_read",
    "enter_plan_mode",
    "exit_plan_mode",
}


def test_default_tools_registers_expected_tool_set() -> None:
    registry = default_tools()
    names = {t.name for t in registry.list()}
    assert names == EXPECTED_TOOLS
    assert len(registry.list()) == len(EXPECTED_TOOLS)


def test_default_tools_schemas_are_complete() -> None:
    registry = default_tools()
    for tool in registry.list():
        assert tool.description, f"{tool.name} is missing description"
        assert tool.parameters.get("type") == "object", tool.name


def test_public_api_importable() -> None:
    from agent_code.tools import Tool, ToolFunc, ToolRegistry, default_tools  # noqa: F401

    assert Tool is not None


def test_echo_tool_runs() -> None:
    registry = default_tools()
    result = registry.run(
        ToolCall(id="1", name="echo", arguments={"text": "hi"}),
        ToolContext(cwd=Path(".")),
    )
    assert not result.is_error
    assert result.content == "hi"


def test_tool_registry_run_unknown_tool() -> None:
    registry = ToolRegistry()
    result = registry.run(
        ToolCall(id="1", name="nope", arguments={}),
        ToolContext(cwd=Path(".")),
    )
    assert result.is_error
    assert "unknown tool" in result.content


def test_imports_have_no_circular_dependency() -> None:
    # cron_tools → tools 方向必须先能加载；若 tools 包在加载期 import cron_tools 会失败。
    import agent_code.agent
    import agent_code.cli
    import agent_code.cron_tools
    import agent_code.slash  # noqa: F401

    assert agent_code.agent is not None
    assert agent_code.cli is not None
    assert agent_code.cron_tools.cron_list is not None
    assert agent_code.slash is not None


# ---- 子进程输出解码（Windows GBK 回归）--------------------------------------


def test_decode_subprocess_output_prefers_utf8() -> None:
    # UTF-8 中文必须原样还原，不能按系统编码（GBK）解。
    assert decode_subprocess_output("中文输出".encode("utf-8")) == "中文输出"


def test_decode_subprocess_output_falls_back_to_system_encoding(monkeypatch) -> None:
    # 中文 Windows 上系统命令（dir 等）吐 GBK：UTF-8 解不开时应回退而不是崩。
    import agent_code.fs_safety as fs

    monkeypatch.setattr(fs.locale, "getpreferredencoding", lambda *a: "gbk")
    assert decode_subprocess_output("中文".encode("gbk")) == "中文"


def test_decode_subprocess_output_replaces_indecodable_bytes() -> None:
    # 两种编码都解不开时用替换字符兜底，绝不抛 UnicodeDecodeError。
    out = decode_subprocess_output(b"\x94\x94\xff")
    assert isinstance(out, str)


def test_decode_subprocess_output_handles_empty() -> None:
    assert decode_subprocess_output(None) == ""
    assert decode_subprocess_output(b"") == ""


# ---- GBK 源文件的读/改（中文 Windows 常见）---------------------------------


def test_read_text_file_decodes_gbk_source(tmp_path) -> None:
    from agent_code.fs_safety import read_text_file

    p = tmp_path / "src.py"
    # newline="" 阻止 Windows 把 \n 转成 \r\n，断言才稳定。
    p.write_text("# 中文注释\n", encoding="gbk", newline="")

    text, encoding = read_text_file(p)

    assert text == "# 中文注释\n"
    assert encoding == "gb18030"


def test_file_edit_preserves_gbk_encoding(tmp_path) -> None:
    """编辑 GBK 源文件后必须仍是 GBK，不能被静默改写成 UTF-8。"""
    from agent_code.fs_safety import read_text_file
    from agent_code.tools.edit import file_edit

    p = tmp_path / "src.py"
    p.write_text("# 中文注释\nprint(1)\n", encoding="gbk", newline="")
    ctx = ToolContext(cwd=tmp_path)
    ctx.read_state.record(p, read_text_file(p)[0])

    out = file_edit(
        {"file_path": "src.py", "old_string": "print(1)", "new_string": "print(2)"},
        ctx,
    )

    assert "Edited" in out
    # 中文原样保留、且能用 GBK 严格解码回来（说明写回没改成 UTF-8）。
    assert p.read_bytes().decode("gbk") == "# 中文注释\nprint(2)\n"


def test_read_file_tool_reads_gbk_without_mojibake(tmp_path) -> None:
    from agent_code.fs_safety import SkipPolicy
    from agent_code.tools.read import read_file

    p = tmp_path / "notes.md"
    p.write_text("中文内容\n", encoding="gbk", newline="")
    ctx = ToolContext(cwd=tmp_path, skip_policy=SkipPolicy())

    assert read_file({"path": "notes.md"}, ctx) == "中文内容\n"
