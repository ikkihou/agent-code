"""Agent-loop integration for token-ratio compaction (MockProvider-driven)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_code.agent import run_agent
from agent_code.context import ContextBudget, TokenUsage, estimate_messages_tokens
from agent_code.model import MockProvider, ModelResponse, ModelStreamEvent, ToolCall
from agent_code.runtime import RuntimeState
from agent_code.session import Session
from agent_code.tools import default_tools
from agent_code.ui.events import event_to_chunks
from agent_code.wire import ContextCompacted, Line, Wire


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def send(self, batch: list[Any]) -> None:
        self.events.extend(batch)


def _collecting_wire() -> tuple[Wire, _RecordingSink]:
    wire = Wire()
    sink = _RecordingSink()
    wire.sink = sink.send
    return wire, sink


def _compaction_count(sink: _RecordingSink) -> int:
    return sum(isinstance(event, ContextCompacted) for event in sink.events)


def _padded_history(count: int, chars: int) -> list[dict[str, Any]]:
    """Pinned task + `count` padded middle messages + one recent user message."""
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "initial task"},
        {"role": "assistant", "content": "acknowledged"},
    ]
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        history.append({"role": role, "content": f"middle {index} " + "x" * chars})
    history.append({"role": "user", "content": "recent working message"})
    return history


class _RecordingMockProvider(MockProvider):
    """MockProvider that records how many compactions the wire showed before a request."""

    def __init__(self, sink: _RecordingSink) -> None:
        self._sink = sink
        self.calls = 0
        self.compactions_before_call: list[int] = []
        self.request_lengths: list[int] = []

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: Any = None,
    ):
        self.calls += 1
        self.compactions_before_call.append(_compaction_count(self._sink))
        self.request_lengths.append(len(messages))
        yield from super().complete_stream(messages, tools, system, signal=signal)


class _ToolThenFinalMockProvider(MockProvider):
    """MockProvider that makes one tool call first, so the loop runs two requests.

    The echoed tool text is large enough that the appended tool result pushes the
    estimate past the threshold after the first usage report -- the usage-driven path
    rather than the initial seed.
    """

    def __init__(self, sink: _RecordingSink, tool_text: str) -> None:
        self._sink = sink
        self._tool_text = tool_text
        self.calls = 0
        self.compactions_before_call: list[int] = []
        self.request_lengths: list[int] = []

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: Any = None,
    ):
        self.calls += 1
        self.compactions_before_call.append(_compaction_count(self._sink))
        self.request_lengths.append(len(messages))
        usage = TokenUsage(input_tokens=estimate_messages_tokens(messages) + 50)
        if self.calls == 1:
            response = ModelResponse(
                tool_calls=[
                    ToolCall(id="call-1", name="echo", arguments={"text": self._tool_text})
                ],
                assistant_content=[
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "echo",
                        "input": {"text": self._tool_text},
                    }
                ],
                usage=usage,
            )
        else:
            response = ModelResponse(text="done", usage=usage)
        yield ModelStreamEvent(type="completed", response=response)


class _NoUsageProvider:
    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: Any = None,
    ):
        yield ModelStreamEvent(type="completed", response=ModelResponse(text="done"))


class _NoUsageTwoTurnProvider:
    """Usage-less provider that forces a second request via a max_tokens continuation."""

    def __init__(self, budget: ContextBudget) -> None:
        self._budget = budget
        self.estimates: list[int] = []

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: Any = None,
    ):
        self.estimates.append(self._budget.estimated_tokens)
        if len(self.estimates) == 1:
            response = ModelResponse(text="cut off", stop_reason="max_tokens")
        else:
            response = ModelResponse(text="done")
        yield ModelStreamEvent(type="completed", response=response)


def test_loop_compacts_before_the_first_request_when_history_is_seeded(tmp_path: Path) -> None:
    """A fresh/resumed session with long history must compact before sending anything."""
    session = Session.create(tmp_path)
    history = _padded_history(count=30, chars=400)
    session.append_messages(history)

    budget = ContextBudget(
        window_tokens=2000,
        compact_threshold=0.80,
        target_ratio=0.55,
        reserve_tokens=0,
    )
    wire, sink = _collecting_wire()
    provider = _RecordingMockProvider(sink)

    result = run_agent(
        "next request",
        provider,
        default_tools(),
        RuntimeState(provider="mock"),
        cwd=tmp_path,
        session=session,
        wire=wire,
        budget=budget,
    )

    # The wire already carried the compaction when the first request went out.
    assert provider.calls == 1
    assert provider.compactions_before_call == [1]
    assert provider.request_lengths[0] < len(history) + 1
    assert _compaction_count(sink) == 1
    assert budget.compactions == 1

    # Pinned first-2 + summary + recent, and the oldest middle message is gone.
    assert result.messages[0] == history[0]
    assert result.messages[1] == history[1]
    assert str(result.messages[2]["content"]).startswith("<compacted-history>")
    assert history[2] not in result.messages


def test_shared_budget_recompacts_at_the_start_of_each_turn(tmp_path: Path) -> None:
    """The REPL shape: one Session + RuntimeState/budget reused across run_agent calls.

    Compaction rewrites only the local list, so the next turn reloads the full
    (append-only) session history. The per-turn reset must let a second compaction
    fire before that turn's first request.
    """
    session = Session.create(tmp_path)
    history = _padded_history(count=57, chars=400)
    session.append_messages(history)

    budget = ContextBudget(
        window_tokens=2000,
        compact_threshold=0.80,
        target_ratio=0.55,
        reserve_tokens=0,
    )
    state = RuntimeState(provider="mock")
    wire, sink = _collecting_wire()
    provider = _RecordingMockProvider(sink)

    run_agent(
        "turn one",
        provider,
        default_tools(),
        state,
        cwd=tmp_path,
        session=session,
        wire=wire,
        budget=budget,
    )
    assert provider.compactions_before_call == [1]
    assert budget.compactions == 1
    full_history_len = len(session.history)

    run_agent(
        "turn two",
        provider,
        default_tools(),
        state,
        cwd=tmp_path,
        session=session,
        wire=wire,
        budget=budget,
    )

    assert provider.compactions_before_call == [1, 2]
    # The second turn's first request carries the compacted list, not the full history.
    assert provider.request_lengths[1] < full_history_len + 1
    assert budget.compactions == 2
    assert _compaction_count(sink) == 2


def test_loop_compacts_once_when_usage_crosses_the_threshold(tmp_path: Path) -> None:
    session = Session.create(tmp_path)
    # Seeded ratio is ~76% (< 80%); the first usage report plus the tool result crosses.
    history = _padded_history(count=20, chars=1200)
    session.append_messages(history)

    budget = ContextBudget(
        window_tokens=8000,
        compact_threshold=0.80,
        target_ratio=0.55,
        reserve_tokens=0,
    )
    wire, sink = _collecting_wire()
    provider = _ToolThenFinalMockProvider(sink, tool_text="y" * 1500)

    result = run_agent(
        "next request",
        provider,
        default_tools(),
        RuntimeState(provider="mock"),
        cwd=tmp_path,
        session=session,
        wire=wire,
        budget=budget,
    )

    # The first request was sent un-compacted; the second one only after a compaction.
    assert provider.compactions_before_call == [0, 1]

    compactions = [event for event in sink.events if isinstance(event, ContextCompacted)]
    assert len(compactions) == 1
    assert compactions[0].before_ratio >= 0.80
    assert compactions[0].after_ratio < compactions[0].before_ratio
    assert compactions[0].summarized_messages > 0
    assert budget.compactions == 1

    # trace carries the same line the event describes.
    assert any(line.startswith("context compacted:") for line in result.trace)

    # Shape after compaction: pinned first-2 + summary + recent messages.
    assert result.messages[0] == history[0]
    assert result.messages[1] == history[1]
    summary = result.messages[2]
    assert summary["role"] == "user"
    assert str(summary["content"]).startswith("<compacted-history>")
    # The oldest middle message was summarized away; the newest one stayed recent.
    assert history[2] not in result.messages
    assert any(
        "recent working message" in str(message.get("content")) for message in result.messages
    )
    assert provider.request_lengths[1] < provider.request_lengths[0]


def test_loop_without_usage_accumulates_heuristic_estimates(tmp_path: Path) -> None:
    """With no usage report, the chars/4 estimate still grows across requests."""
    budget = ContextBudget(window_tokens=10000, compact_threshold=0.8, reserve_tokens=0)
    provider = _NoUsageTwoTurnProvider(budget)
    wire, sink = _collecting_wire()

    result = run_agent(
        "x" * 4000,
        provider,
        default_tools(),
        RuntimeState(provider="mock"),
        cwd=tmp_path,
        wire=wire,
        budget=budget,
    )

    assert result.final == "done"
    assert len(provider.estimates) == 2
    # Seeded from the prompt (4000 chars // 4), then grown by the assistant reply and
    # the max_tokens continuation message.
    assert provider.estimates[0] == 1000
    assert provider.estimates[1] > provider.estimates[0]
    # Far below the threshold: no compaction, and no crash from the missing usage.
    assert budget.compactions == 0
    assert not [event for event in sink.events if isinstance(event, ContextCompacted)]


def test_loop_skips_compaction_when_no_safe_boundary_exists(tmp_path: Path) -> None:
    # The seed alone pushes past the threshold, but a single message has no safe
    # middle to summarize: emit a warning and keep going instead of crashing.
    budget = ContextBudget(window_tokens=1000, compact_threshold=0.5, reserve_tokens=0)
    wire, sink = _collecting_wire()

    result = run_agent(
        "x" * 4000,
        _NoUsageProvider(),
        default_tools(),
        RuntimeState(provider="mock"),
        cwd=tmp_path,
        wire=wire,
        budget=budget,
    )

    assert result.final == "done"
    assert budget.compactions == 0
    assert any("compaction skipped" in line for line in result.trace)
    assert any(
        isinstance(event, Line) and "compaction skipped" in event.text for event in sink.events
    )


def test_context_compacted_event_renders_one_dim_line() -> None:
    event = ContextCompacted(before_ratio=0.94, after_ratio=0.51, summarized_messages=11)
    chunks = event_to_chunks(event)

    assert len(chunks) == 1
    assert "context compacted: 94% → 51% (11 messages summarized)" in chunks[0].text
    assert chunks[0].format == "rich"
    assert "[dim]" in chunks[0].text
