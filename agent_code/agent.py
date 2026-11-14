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
from typing import Any

from .compact_basic import compact
from .fs_safety import SkipPolicy, load_gitignore
from .model import ModelProvider, ModelRequestAborted, ModelResponse
from .project_memory import load_agent_md
from .runtime import RuntimeState
from .session import Session
from .tool_execution import (
    approve_plan,
    execute_one_tool_call,
    execute_plan_boundary_calls,
    partition_tool_calls,
)
from .tools import ToolContext, ToolRegistry
from .wire import Line, TextDelta, Wire

# Worker count for the parallel read-only tool batch. Read-only calls are safe to run
# concurrently; write tools form their own serial batch and are not affected.
_PARALLEL_READONLY_WORKERS = 4


@dataclass
class AgentResult:
    final: str
    trace: list[str]
    messages: list[dict[str, Any]]


_SYSTEM_CORE = (
    "You are an AI coding agent running inside a CLI harness. "
    "You have access to tools for reading/writing files, running shell commands, "
    "searching the web, and asking the user questions. "
    "Use tools when needed; respond directly when you can."
)


def build_system_prompt(cwd: Path) -> str:
    """Assemble the system prompt: core guidance + AGENT.md + the MEMORY.md index.
    Injection order: core prompt -> project rules -> cross-session memory index."""
    from .memdir.store import load_index as load_memory_index

    parts: list[str] = [_SYSTEM_CORE]
    agent_md = load_agent_md(cwd)
    if agent_md:
        parts.append(agent_md)

    memory_idx = load_memory_index(cwd)
    if memory_idx:
        parts.append(f"<project-memory>\n{memory_idx}\n</project-memory>")

    return "\n\n".join(parts)


# Convert the internal ModelResponse back into Anthropic Messages API assistant
# content blocks.
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
    wire: Wire | None = None,
) -> AgentResult:
    ## Resolve the cwd
    resolved_cwd = cwd or Path.cwd()
    ## Build the tool context
    ctx = ToolContext(
        cwd=resolved_cwd,
        skip_policy=SkipPolicy.default(gitignore=load_gitignore(resolved_cwd)),
        state=state,
    )

    ## Load the conversation history
    if session and session.history:
        messages = list(session.history)
        messages.append({"role": "user", "content": prompt})
    else:
        messages = [{"role": "user", "content": prompt}]

    if session:
        session.append_messages([messages[-1]])

    # Event channel: wire is the only boundary between the loop and the frontend. When
    # none is supplied, use an empty channel whose events simply go nowhere
    # (fire-and-forget); the loop has no notion of whether anyone is listening.
    if wire is None:
        wire = Wire()

    def emit(text: str, *, markup: bool = False) -> None:
        """Emit a notification event: one complete line of text (no trailing newline).

        markup=True means text is Rich markup for each frontend to render; trace always
        records the original text. trace is still maintained by run_agent -- it is part
        of AgentResult, used by tests and --resume auditing, and is not part of the
        event stream.
        """
        trace.append(text)
        wire.send(Line(text, markup=markup))

    def interrupted_result() -> AgentResult:
        emit("interrupted by user")
        return AgentResult(final="interrupted", trace=trace, messages=messages)

    trace: list[str] = []
    continuation_count = 0
    response: ModelResponse | None = None

    def continue_with(reason: str, prompt: str) -> None:
        """Append a continue user message; the call site is followed by a bare continue
        back to the top of the loop."""
        nonlocal continuation_count
        continuation_count += 1
        emit(f"continue: {reason}")
        messages.append({"role": "user", "content": prompt})
        if session:
            session.append_messages(messages[-2:])

    for step in range(max_steps):
        # The user may press ESC while a tool runs. Checking before the request avoids
        # sending one more model request.
        if state.abort_event.is_set():
            return interrupted_result()

        ## 1. sending api request
        if len(messages) > 40:
            messages = compact(messages, keep=8)
            emit(f"compacted: {len(messages)} messages remaining", markup=True)

        response = None
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
                    # Raw deltas go straight onto the wire; line buffering is a frontend
                    # rendering concern, left to the consumer via StreamLineBuffer.
                    wire.send(TextDelta(text=event.text))
                elif event.type == "completed":
                    response = event.response
        except ModelRequestAborted:
            return interrupted_result()

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
            # The upstream final message occasionally lacks its text block (even though
            # streaming clearly produced output). Fall back to the text actually streamed
            # so final never turns into an empty string.
            final = response.text or "".join(streamed_parts) or ""
            if state.permission_mode == "plan" and final.strip():
                if approve_plan(state, final):
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

            # max_tokens continuation: the upstream ran out of token budget mid-sentence
            # (stop_reason == "max_tokens"), the main source of "incomplete final".
            # Append a continue message so the model keeps writing instead of the long
            # answer being hard-truncated.
            if response.stop_reason == "max_tokens" and continuation_count < 2:
                continue_with(
                    "max_tokens reached — asking the model to continue",
                    (
                        "Your previous message was cut off (max_tokens reached). "
                        "Continue from where you left off exactly; do not repeat "
                        "anything you already wrote."
                    ),
                )
                continue

            # Empty-completion retry: neither text nor tool calls usually means a
            # transient empty upstream response. Do not finish here (the user would see
            # nothing after "final:"); give it one more chance to answer.
            if not final.strip() and continuation_count < 2:
                continue_with(
                    "empty response — asking the model to retry",
                    (
                        "Your previous message was empty (no text and no tool calls). "
                        "Reply with your actual answer or the next tool call."
                    ),
                )
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
                continue_with(forced, f"continue: {forced}")
                continue  # Back to the top of the loop for one more round.
            if streamed_parts:
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
            wire,
        )
        if tool_result_blocks is None:
            tool_result_blocks = []
            for batch in partition_tool_calls(response.tool_calls, tools):
                if len(batch) == 1:
                    tool_result_blocks.append(
                        execute_one_tool_call(batch[0], ctx, state, tools, wire)
                    )
                else:
                    # Parallel read-only group. ex.map returns results in input order, so
                    # tool_result order lines up with tool_use order for free -- a protocol
                    # constraint we must preserve.
                    with ThreadPoolExecutor(max_workers=_PARALLEL_READONLY_WORKERS) as ex:
                        results = list(
                            ex.map(
                                lambda c: execute_one_tool_call(c, ctx, state, tools, wire),
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

