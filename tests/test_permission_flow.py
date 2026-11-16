"""Direct unit tests for the tool_execution module: permission flow, confirmation
routing, the plan boundary and parallel grouping.

Everything goes through pure functions / a fake asker; no real user files and no network
requests.
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
from agent_code.wire import CommandPreview, DiffPreview, ToolCallStarted, Wire


def _tool(name: str, *, read_only: bool = False, output: str = "ok") -> Tool:
    return Tool(name, name, run=lambda args, ctx: output, is_read_only=read_only)


def _registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id=f"call-{name}", name=name, arguments=arguments)


def _collecting_wire() -> tuple[Wire, list]:
    events: list = []
    wire = Wire()
    wire.sink = events.extend
    return wire, events


def _run(call: ToolCall, ctx: ToolContext, state: RuntimeState, reg: ToolRegistry):
    wire, events = _collecting_wire()
    block = execute_one_tool_call(call, ctx, state, reg, wire)
    return block, events


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
    block, events = _run(_call("echo"), ToolContext(cwd=tmp_path), RuntimeState(), reg)
    assert block["is_error"] is False
    assert block["content"] == "ok"
    assert block["tool_use_id"] == "call-echo"
    # The tool layer must emit the ToolCallStarted display event (the basis for the
    # frontend's tool_call line)
    assert any(isinstance(e, ToolCallStarted) and e.name == "echo" for e in events)


def test_execute_one_tool_call_bash_confirmation_sends_command_preview(tmp_path):
    """ask-mode bash: CommandPreview must be emitted before confirmation (the basis for the
    frontend's command line)."""
    reg = _registry(_tool("bash"))
    state = RuntimeState(asker=lambda request: True)
    wire, events = _collecting_wire()
    block = execute_one_tool_call(
        _call("bash", command="echo hi"), ToolContext(cwd=tmp_path), state, reg, wire
    )
    assert block["is_error"] is False
    assert any(
        isinstance(e, CommandPreview) and e.command == "echo hi" for e in events
    )


def test_execute_one_tool_call_edit_confirmation_sends_diff_preview(tmp_path):
    """ask-mode file_edit: DiffPreview must be emitted before confirmation (the basis for
    the frontend's diff rendering)."""
    p = tmp_path / "a.txt"
    p.write_text("hello\n", encoding="utf-8")
    ctx = ToolContext(cwd=tmp_path)
    ctx.read_state.record(p, "hello\n")
    reg = _registry(_tool("file_edit"))
    state = RuntimeState(asker=lambda request: True)
    wire, events = _collecting_wire()
    block = execute_one_tool_call(
        _call("file_edit", file_path="a.txt", old_string="hello", new_string="hi"),
        ctx,
        state,
        reg,
        wire,
    )
    assert block["is_error"] is False
    assert any(
        isinstance(e, DiffPreview) and e.path == "a.txt" and "hi" in e.new for e in events
    )


def test_execute_one_tool_call_edit_passes_diff_in_confirm_detail(tmp_path):
    """ask-mode file_edit: the confirmation request must carry the diff in ``detail`` so
    the user sees the actual change instead of approving blindly."""
    p = tmp_path / "a.txt"
    p.write_text("hello\n", encoding="utf-8")
    ctx = ToolContext(cwd=tmp_path)
    ctx.read_state.record(p, "hello\n")
    reg = _registry(_tool("file_edit"))
    requests: list = []
    state = RuntimeState(asker=lambda request: requests.append(request) or True)
    wire, _ = _collecting_wire()
    block = execute_one_tool_call(
        _call("file_edit", file_path="a.txt", old_string="hello", new_string="hi"),
        ctx,
        state,
        reg,
        wire,
    )
    assert block["is_error"] is False
    assert len(requests) == 1
    assert requests[0]["type"] == "confirm"
    assert "-hello" in requests[0]["detail"]
    assert "+hi" in requests[0]["detail"]


def test_truncate_diff_caps_line_count():
    from agent_code.tool_execution import _truncate_diff

    diff = "\n".join(f"line {i}" for i in range(50))
    out = _truncate_diff(diff, max_lines=30)
    assert "line 29" in out
    assert "line 30" not in out
    assert "20 lines omitted" in out

    assert _truncate_diff("short\n", max_lines=30) == "short\n"


def test_edit_confirm_detail_is_plain_text_without_rich_markup(tmp_path):
    """The ask-prompt body renders the diff as plain text, so no [red]/[green] wrappers
    leak into the REPL Live area."""
    p = tmp_path / "a.txt"
    p.write_text("hello\n", encoding="utf-8")
    ctx = ToolContext(cwd=tmp_path)
    ctx.read_state.record(p, "hello\n")
    reg = _registry(_tool("file_edit"))
    requests: list = []
    state = RuntimeState(asker=lambda request: requests.append(request) or True)
    wire, _ = _collecting_wire()
    execute_one_tool_call(
        _call("file_edit", file_path="a.txt", old_string="hello", new_string="hi"),
        ctx,
        state,
        reg,
        wire,
    )
    assert len(requests) == 1
    assert "[red]" not in requests[0]["detail"]
    assert "[green]" not in requests[0]["detail"]
    assert "-hello" in requests[0]["detail"]
    assert "+hi" in requests[0]["detail"]


def test_execute_one_tool_call_denies_dangerous_bash(tmp_path):
    reg = _registry(_tool("bash"))
    block, _ = _run(
        _call("bash", command="rm -rf /"), ToolContext(cwd=tmp_path), RuntimeState(), reg
    )
    assert block["is_error"] is True
    assert "Dangerous command blocked" in block["content"]


# ---- execute_one_tool_call: file-write preflight ----------------------------


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
    ctx.read_state.record(p, "hello")  # mark as read to skip the read-before-edit check
    reg = _registry(_tool("file_edit"))
    state = RuntimeState(asker=lambda request: False)  # reject every confirm
    block, _ = _run(
        _call("file_edit", file_path="a.txt", old_string="hello", new_string="hi"),
        ctx,
        state,
        reg,
    )
    assert block["is_error"] is True
    assert "error: edit rejected by user" in block["content"]


# ---- execute_one_tool_call: ask dispatch -----------------------------------


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
    state = RuntimeState(asker=lambda request: 1)  # pick item 1 (choice requests return an index)
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


# ---- fullAccess mode --------------------------------------------------------


def test_full_access_allows_bash_even_dangerous_patterns(tmp_path):
    """fullAccess bypasses the dangerous-command deny list: the mode is an explicit
    blanket approval, so even rm -rf runs without prompting."""
    reg = _registry(_tool("bash"))
    block, _ = _run(
        _call("bash", command="rm -rf /tmp/x"),
        ToolContext(cwd=tmp_path),
        RuntimeState(permission_mode="fullAccess"),
        reg,
    )
    assert block["is_error"] is False


def test_full_access_allows_file_write_without_confirmation(tmp_path):
    reg = _registry(_tool("file_write"))
    state = RuntimeState(permission_mode="fullAccess")
    # No asker injected: a confirmation attempt would go through the typer fallback and
    # fail on the null stdin -- reaching "ok" proves no ask happened.
    block, _ = _run(
        _call("file_write", file_path="new.txt", content="hi"),
        ToolContext(cwd=tmp_path),
        state,
        reg,
    )
    assert block["is_error"] is False
    assert block["content"] == "ok"


def test_full_access_still_asks_ask_user_question(tmp_path):
    """ask_user_question's ask is the interaction itself, not a gate: even fullAccess
    routes it through the confirmation UI."""
    reg = _registry()
    state = RuntimeState(permission_mode="fullAccess", asker=lambda request: 1)
    block, _ = _run(
        _call("ask_user_question", prompt="Pick one", options=["A", "B"]),
        ToolContext(cwd=tmp_path),
        state,
        reg,
    )
    assert block["is_error"] is False
    assert "User selected" in block["content"]
    assert '"A"' in block["content"]


def test_cycle_permission_mode_includes_full_access():
    state = RuntimeState(permission_mode="plan")
    assert state.cycle_permission_mode() == "fullAccess"
    assert state.cycle_permission_mode() == "default"  # wraps around



def test_execute_plan_boundary_calls_skips_other_tools(tmp_path):
    reg = _registry(
        _tool("read_file", read_only=True),
        _tool("exit_plan_mode", output="approved"),
    )
    state = RuntimeState(permission_mode="plan", asker=lambda request: True)
    wire, _ = _collecting_wire()
    calls = [_call("read_file"), _call("exit_plan_mode", plan_summary="my plan")]
    blocks = execute_plan_boundary_calls(calls, ToolContext(cwd=tmp_path), state, reg, wire)
    assert blocks is not None
    # Same-turn tools other than exit_plan_mode are skipped and get an error block
    assert blocks[0]["is_error"] is True
    assert "Skipped because exit_plan_mode" in blocks[0]["content"]
    # exit_plan_mode itself runs; approval switches the mode to acceptEdits
    assert blocks[1]["content"] == "approved"
    assert state.permission_mode == "acceptEdits"
