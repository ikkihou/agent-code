"""
##
##       filename: agent.py
##        created: 2026/06/14
##         author: Paul_Bao
##            IDE: Neovim
##       Version : 1.0
##       Contact : paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from .compact_basic import compact
from .fs_safety import SkipPolicy, load_gitignore
from .model import ModelProvider, ModelRequestAborted, ModelResponse
from .output import OutputWriter, render_console_chunk
from .project_memory import load_agent_md
from .prompt_ui import confirm_plan
from .runtime import RuntimeState
from .session import Session
from .tool_execution import (
    execute_one_tool_call,
    execute_plan_boundary_calls,
    partition_tool_calls,
)
from .tools import ToolContext, ToolRegistry

console = Console()
PrintFunc = Callable[..., None]

# 只读工具并行批的 worker 数。只读调用并发安全；写工具自成串行组，不受此限。
_PARALLEL_READONLY_WORKERS = 4


@dataclass
class AgentResult:
    final: str
    trace: list[str]
    messages: list[dict[str, Any]]


@dataclass
class _LineBufferedStreamRenderer:
    """Render complete lines so prompt-toolkit cannot overwrite partial chunks."""

    write_line: Callable[[str], None]
    pending: str = ""
    started: bool = False

    def feed(self, text: str) -> None:
        if not text:
            return

        self.started = True
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.write_line(line.removesuffix("\r"))

    def finish(self) -> None:
        if self.pending:
            self.write_line(self.pending)
            self.pending = ""


def _make_printer(output: OutputWriter | None = None) -> PrintFunc:
    if output is None:
        return console.print

    def print_to_output(*objects: Any, **kwargs: Any) -> None:
        styled = (
            kwargs.get("markup", True) is not False
            or kwargs.get("style") is not None
            or any(not isinstance(obj, str) for obj in objects)
        )
        output(render_console_chunk(*objects, styled=styled, **kwargs))

    return print_to_output


_SYSTEM_CORE = (
    "You are an AI coding agent running inside a CLI harness. "
    "You have access to tools for reading/writing files, running shell commands, "
    "searching the web, and asking the user questions. "
    "Use tools when needed; respond directly when you can."
)


def build_system_prompt(cwd: Path) -> str:
    """组装 system prompt：核心指南 + AGENT.md + MEMORY.md 索引。
    注入顺序：core prompt → 项目规则 → 跨 session 记忆索引。"""
    from .memdir.store import load_index as load_memory_index

    parts: list[str] = [_SYSTEM_CORE]
    agent_md = load_agent_md(cwd)
    if agent_md:
        parts.append(agent_md)

    memory_idx = load_memory_index(cwd)
    if memory_idx:
        parts.append(f"<project-memory>\n{memory_idx}\n</project-memory>")

    return "\n\n".join(parts)


# 把内部 ModelResponse 还原成 Anthropic Messages API 的 assistant content blocks格式
def _assistant_message(response: ModelResponse) -> dict[str, Any]:
    if response.assistant_content:
        return {"role": "assistant", "content": response.assistant_content}

    content: list[dict[str, Any]] = []
    if response.text:
        content.append({"type": "text", "text": response.text})
    for call in response.tool_calls or []:
        content.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    return {"role": "assistant", "content": content}


def run_agent(  ## AGENT LOOP
    prompt: str,
    provider: ModelProvider,
    tools: ToolRegistry,
    state: RuntimeState,
    max_steps: int = 8,
    cwd: Path | None = None,
    session: Session | None = None,
    system_prompt: str | None = None,
    output: OutputWriter | None = None,
) -> AgentResult:
    ## 解析 cwd
    resolved_cwd = cwd or Path.cwd()
    ## 构建工具上下文
    ctx = ToolContext(
        cwd=resolved_cwd,
        skip_policy=SkipPolicy.default(gitignore=load_gitignore(resolved_cwd)),
        state=state,
    )

    ## 加载历史对话
    if session and session.history:
        messages = list(session.history)
        messages.append({"role": "user", "content": prompt})
    else:
        messages = [{"role": "user", "content": prompt}]

    if session:
        session.append_messages([messages[-1]])

    print_output = _make_printer(output)

    def emit(line: str) -> None:
        # 工具结果可能很长：完整内容只通过 tool_result 回填给模型，终端只看工具调用/最终回答。
        if line.startswith("observation:"):
            return
        trace.append(line)
        print_output(line, markup=False, highlight=False)

    def interrupted_result() -> AgentResult:
        emit("interrupted by user")
        return AgentResult(final="interrupted", trace=trace, messages=messages)

    def write_stream_line(line: str) -> None:
        print_output(
            line,
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    trace: list[str] = []
    continuation_count = 0
    response: ModelResponse | None = None
    for step in range(max_steps):
        # 用户可能在工具执行期间按下 ESC。请求前检查可避免再多发一轮模型请求。
        if state.abort_event.is_set():
            return interrupted_result()

        ## 1. sending api request
        if len(messages) > 40:
            messages = compact(messages, keep=8)
            print_output(f"[dim]compacted: {len(messages)} messages remaining[/dim]")

        response = None
        stream_renderer = _LineBufferedStreamRenderer(write_stream_line)
        streamed_parts: list[str] = []
        try:
            for event in provider.complete_stream(
                messages,
                tools.list(),
                system_prompt,
                signal=state.abort_event,
            ):
                if event.type == "text_delta" and event.text:
                    streamed_parts.append(event.text)
                    stream_renderer.feed(event.text)
                elif event.type == "completed":
                    response = event.response
        except ModelRequestAborted:
            stream_renderer.finish()
            return interrupted_result()

        stream_renderer.finish()
        streamed_text = stream_renderer.started
        if response is None:
            raise RuntimeError("provider stream ended without a completed response")

        # Cancellation wins the race with stream completion. In that case do not
        # persist the assistant response or execute any tool calls it contains.
        if state.abort_event.is_set():
            return interrupted_result()

        ## 2. add complete LLM response to history
        messages.append(_assistant_message(response))

        ## 3(1). tool call or end execution
        if not response.tool_calls:
            # 上游 final message 偶尔会缺 text block（但流式时明明有输出）。
            # 用实际流过的文本兜底，避免 final 变成空字符串。
            final = response.text or "".join(streamed_parts) or ""
            if state.permission_mode == "plan" and final.strip():
                if confirm_plan(state, final):
                    state.permission_mode = "acceptEdits"
                    messages.append({"role": "user", "content": "Plan approved. Implement it now."})
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Plan not approved. Revise the plan and present it again.",
                        }
                    )
                if session:
                    session.append_messages(messages[-2:])
                continue

            # max_tokens 截断续写：上游在 token 预算内没把话说完（stop_reason ==
            # "max_tokens"，回答断在句中），是"final 不完整"的主要来源。追加一条
            # continue 让模型接着写，避免长回答被硬截断。
            if response.stop_reason == "max_tokens" and continuation_count < 2:
                continuation_count += 1
                emit("continue: max_tokens reached — asking the model to continue")
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous message was cut off (max_tokens reached). "
                            "Continue from where you left off exactly; do not repeat "
                            "anything you already wrote."
                        ),
                    }
                )
                if session:
                    session.append_messages(messages[-2:])
                continue

            # 空完成重试：既没文本也没工具调用，多半是上游偶发空响应。
            # 别就这样收尾（会看到 "final:" 后面什么都没有），给一次续写机会。
            if not final.strip() and continuation_count < 2:
                continuation_count += 1
                emit("continue: empty response — asking the model to retry")
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous message was empty (no text and no tool calls). "
                            "Reply with your actual answer or the next tool call."
                        ),
                    }
                )
                if session:
                    session.append_messages(messages[-2:])
                continue

            from .hooks import run_hooks_raw

            forced: str | None = None
            if continuation_count < 2:
                payload = {
                    "event": "Stop",
                    "final_text": final,
                    "cwd": str(ctx.cwd),
                    "continuation_count": continuation_count,
                }
                for h in run_hooks_raw("Stop", payload, ctx.cwd):
                    if not h["success"] and h["output"].strip():
                        forced = h["output"].strip()
                        break
            if forced is not None:
                continuation_count += 1
                emit(f"continue: {forced}")
                messages.append({"role": "user", "content": f"continue: {forced}"})
                if session:
                    session.append_messages(messages[-2:])
                continue  # 回到 loop 顶，再跑一轮
            if streamed_text:
                trace.append(f"final: {final}")
            else:
                emit(f"final: {final}" if final.strip() else "final: (empty response)")
            if session:
                session.append_messages([messages[-1]])
            return AgentResult(final=final, trace=trace, messages=messages)

        ## 3(2). execute all tool calls
        tool_result_blocks = execute_plan_boundary_calls(
            response.tool_calls,
            ctx,
            state,
            tools,
            emit,
            print_output,
        )
        if tool_result_blocks is None:
            tool_result_blocks = []
            for batch in partition_tool_calls(response.tool_calls, tools):
                if len(batch) == 1:
                    tool_result_blocks.append(
                        execute_one_tool_call(batch[0], ctx, state, tools, emit, print_output)
                    )
                else:
                    # 只读组并行。ex.map 按输入顺序返回结果，
                    # 所以 tool_result 顺序天然对齐 tool_use 顺序——这是必须守的协议约束。
                    with ThreadPoolExecutor(max_workers=_PARALLEL_READONLY_WORKERS) as ex:
                        results = list(
                            ex.map(
                                lambda c: execute_one_tool_call(
                                    c, ctx, state, tools, emit, print_output
                                ),
                                batch,
                            )
                        )
                    tool_result_blocks.extend(results)

        messages.append({"role": "user", "content": tool_result_blocks})
        if session:
            session.append_messages(messages[-2:])

    if response is None:
        final = f"Agent reached max steps ({max_steps}) without making a request."
        emit(f"final: {final}")
        return AgentResult(final=final, trace=trace, messages=messages)

    final = f"Agent reached max steps ({max_steps}) without finishing. Last response: {response}"
    emit(f"final: {final}")
    return AgentResult(final=final, trace=trace, messages=messages)

