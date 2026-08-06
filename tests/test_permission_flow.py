"""tool_execution 模块的直接单元测试：权限流转、确认分发、plan 边界与并行分组。

全部走纯函数/假 asker，不碰真实用户文件，也不发网络请求。
"""

from __future__ import annotations

from agent_code.model import ToolCall
from agent_code.runtime import RuntimeState
from agent_code.tool_execution import (
    approve_plan,
    execute_one_tool_call,
    execute_plan_boundary_calls,
    partition_tool_calls,
)
from agent_code.tools import Tool, ToolContext, ToolRegistry


def _tool(name: str, *, read_only: bool = False, output: str = "ok") -> Tool:
    return Tool(name, name, run=lambda args, ctx: output, is_read_only=read_only)


def _registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id=f"call-{name}", name=name, arguments=arguments)


def _noop_print(*objects, **kwargs) -> None:
    return None


def _run(call: ToolCall, ctx: ToolContext, state: RuntimeState, reg: ToolRegistry):
    emitted: list[str] = []
    block = execute_one_tool_call(call, ctx, state, reg, emitted.append, _noop_print)
    return block, emitted


# ---- partition_tool_calls -------------------------------------------------


def test_partition_tool_calls_groups_consecutive_read_only():
    reg = _registry(_tool("read_file", read_only=True), _tool("file_write"))
    calls = [_call("read_file"), _call("read_file"), _call("file_write"), _call("read_file")]
    groups = partition_tool_calls(calls, reg)
    assert [g[0].name for g in groups] == ["read_file", "file_write", "read_file"]
    assert [len(g) for g in groups] == [2, 1, 1]


def test_partition_tool_calls_all_read_only_single_group():
    reg = _registry(_tool("read_file", read_only=True))
    calls = [_call("read_file") for _ in range(3)]
    groups = partition_tool_calls(calls, reg)
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_partition_tool_calls_unknown_tool_serial():
    reg = _registry(_tool("read_file", read_only=True))
    calls = [_call("read_file"), _call("unknown_tool")]
    groups = partition_tool_calls(calls, reg)
    assert [len(g) for g in groups] == [1, 1]


# ---- execute_one_tool_call：allow / deny ----------------------------------


def test_execute_one_tool_call_allow_success(tmp_path):
    reg = _registry(_tool("echo", output="ok"))
    block, _ = _run(_call("echo"), ToolContext(cwd=tmp_path), RuntimeState(), reg)
    assert block["is_error"] is False
    assert block["content"] == "ok"
    assert block["tool_use_id"] == "call-echo"


def test_execute_one_tool_call_denies_dangerous_bash(tmp_path):
    reg = _registry(_tool("bash"))
    block, _ = _run(
        _call("bash", command="rm -rf /"), ToolContext(cwd=tmp_path), RuntimeState(), reg
    )
    assert block["is_error"] is True
    assert "Dangerous command blocked" in block["content"]


# ---- execute_one_tool_call：文件写前置校验 ---------------------------------


def test_execute_one_tool_call_file_edit_requires_read_first(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    reg = _registry(_tool("file_edit"))
    block, _ = _run(
        _call("file_edit", file_path="a.txt", old_string="hello", new_string="hi"),
        ToolContext(cwd=tmp_path),
        RuntimeState(),
        reg,
    )
    assert block["is_error"] is True
    assert "error: file has not been read yet" in block["content"]


def test_execute_one_tool_call_edit_rejected_by_user(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    ctx = ToolContext(cwd=tmp_path)
    ctx.read_state.record(p, "hello")  # 标记已读，跳过读前校验
    reg = _registry(_tool("file_edit"))
    state = RuntimeState(asker=lambda request: False)  # 拒绝所有 confirm
    block, _ = _run(
        _call("file_edit", file_path="a.txt", old_string="hello", new_string="hi"),
        ctx,
        state,
        reg,
    )
    assert block["is_error"] is True
    assert "error: edit rejected by user" in block["content"]


# ---- execute_one_tool_call：ask 分发 ---------------------------------------


def test_execute_one_tool_call_bash_rejected_by_user(tmp_path):
    reg = _registry(_tool("bash"))
    state = RuntimeState(asker=lambda request: False)
    block, _ = _run(
        _call("bash", command="echo hi"), ToolContext(cwd=tmp_path), state, reg
    )
    assert block["is_error"] is True
    assert "error: command rejected by user" in block["content"]


def test_execute_one_tool_call_ask_user_question_intercepted(tmp_path):
    reg = _registry()
    state = RuntimeState(asker=lambda request: 1)  # 选第 1 项（choice 请求按索引返回）
    block, _ = _run(
        _call("ask_user_question", prompt="Pick one", options=["Option A", "Option B"]),
        ToolContext(cwd=tmp_path),
        state,
        reg,
    )
    assert block["is_error"] is False
    assert "User selected:" in block["content"]
    assert "Option A" in block["content"]


# ---- approve_plan ----------------------------------------------------------


def test_approve_plan_approves_and_switches_mode():
    state = RuntimeState(permission_mode="plan", asker=lambda request: True)
    assert approve_plan(state, "plan text") is True
    assert state.permission_mode == "acceptEdits"


def test_approve_plan_rejects_keeps_mode():
    state = RuntimeState(permission_mode="plan", asker=lambda request: False)
    assert approve_plan(state, "plan text") is False
    assert state.permission_mode == "plan"


# ---- execute_plan_boundary_calls -------------------------------------------


def test_execute_plan_boundary_calls_skips_other_tools(tmp_path):
    reg = _registry(
        _tool("read_file", read_only=True),
        _tool("exit_plan_mode", output="approved"),
    )
    state = RuntimeState(permission_mode="plan", asker=lambda request: True)
    emitted: list[str] = []
    calls = [_call("read_file"), _call("exit_plan_mode", plan_summary="my plan")]
    blocks = execute_plan_boundary_calls(
        calls, ToolContext(cwd=tmp_path), state, reg, emitted.append, _noop_print
    )
    assert blocks is not None
    # 非 exit_plan_mode 的同轮工具被跳过，返回 error block
    assert blocks[0]["is_error"] is True
    assert "Skipped because exit_plan_mode" in blocks[0]["content"]
    # exit_plan_mode 本身执行，批准后模式切到 acceptEdits
    assert blocks[1]["content"] == "approved"
    assert state.permission_mode == "acceptEdits"
