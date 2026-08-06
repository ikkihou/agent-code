"""tools/ 包重构的回归测试：锁定组装结果与公开 API。"""

from __future__ import annotations

from pathlib import Path

from agent_code.model import ToolCall
from agent_code.tools import ToolContext, ToolRegistry, default_tools

EXPECTED_TOOLS = {
    "echo",
    "system_date",
    "read_file",
    "list_files",
    "glob",
    "grep",
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
