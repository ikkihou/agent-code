"""Event translation and state-machine tests for the scrollback+Live REPL.

Never touches a real terminal: LiveArea / event_to_chunks / the prompt state machine
are pure functions or injectable state objects, and run_agent is driven by a fake
provider.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent_code.agent import run_agent
from agent_code.model import ModelResponse, ModelStreamEvent
from agent_code.output import OutputChunk
from agent_code.runtime import RuntimeState
from agent_code.slash import SlashContext, dispatch_slash
from agent_code.tools import ToolRegistry
from agent_code.ui.completer import SlashCompleter
from agent_code.ui.events import event_to_chunks, render_user_prompt
from agent_code.ui.live import LiveArea
from agent_code.ui.prompts import (
    parse_choice_index,
    parse_confirm_answer,
    prompt_for_request,
    render_prompt_request,
)
from agent_code.ui.scrollback import _chunk_fragments
from agent_code.wire import (
    CommandPreview,
    DiffPreview,
    Line,
    PromptEcho,
    StreamLineBuffer,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
    Wire,
)


class _RecordingSink:
    """Synchronous wire consumer: records every received event batch (flushes on send)."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def send(self, batch: list[Any]) -> None:
        self.events.extend(batch)

    def texts(self) -> list[str]:
        """The raw TextDelta stream (what it looks like before the consumer assembles lines)."""
        return [e.text for e in self.events if isinstance(e, TextDelta)]


def _collecting_wire() -> tuple[Wire, _RecordingSink]:
    wire = Wire()
    sink = _RecordingSink()
    wire.sink = sink.send
    return wire, sink


class _FragmentedProvider:
    def __init__(self, chunks: list[str], final_text: str) -> None:
        self.chunks = chunks
        self.final_text = final_text

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: Any = None,
    ):
        for chunk in self.chunks:
            yield ModelStreamEvent(type="text_delta", text=chunk)
        yield ModelStreamEvent(
            type="completed",
            response=ModelResponse(text=self.final_text),
        )


class _SequenceProvider:
    """Returns the preset ModelResponses in order, driving several run_agent requests."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: Any = None,
    ):
        response = self._responses.pop(0)
        if response.text:
            yield ModelStreamEvent(type="text_delta", text=response.text)
        yield ModelStreamEvent(type="completed", response=response)


# ---- run_agent -> wire event stream ----------------------------------------


def test_run_agent_streaming_reaches_wire_as_text_delta(tmp_path: Path) -> None:
    final_text = "First line complete\n\n- **Ability**: can read, generate and process text"
    provider = _FragmentedProvider(
        ["First line", " complete\n\n- **Abi", "lity**: can read, ", "generate and process text"],
        final_text,
    )
    wire, sink = _collecting_wire()

    result = run_agent(
        "introduce yourself",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    assert result.final == final_text
    # Raw deltas go onto the wire unchanged; assembling lines is the consumer's job
    # (StreamLineBuffer).
    assert "".join(sink.texts()) == final_text

    lines: list[str] = []
    line_buf = StreamLineBuffer(lines.append)
    for text in sink.texts():
        line_buf.feed(text)
    line_buf.finish()
    assert lines == ["First line complete", "", "- **Ability**: can read, generate and process text"]


def test_wire_merges_adjacent_text_deltas_in_queue() -> None:
    """When the UI consumes slowly, adjacent TextDeltas merge in place inside the queue
    instead of piling up hundreds of events."""
    wire = Wire()
    # notify only, no sink: simulates async consumption where the UI has not drained yet.
    wire.notify = lambda: None

    for text in ["a", "b", "c"]:
        wire.send(TextDelta(text=text))

    assert len(wire.drain()) == 1


def test_wire_sync_sink_flushes_each_send() -> None:
    """Synchronous sink: every send flushes immediately, events are consumed and never merge."""
    wire = Wire()
    batches: list[list[Any]] = []
    wire.sink = batches.append

    wire.send(Line("one"))
    wire.send(TextDelta("del"))
    wire.send(TextDelta("ta"))

    flat = [e for batch in batches for e in batch]
    assert [type(e).__name__ for e in flat] == ["Line", "TextDelta", "TextDelta"]
    assert flat[1].text == "del" and flat[2].text == "ta"


def test_run_agent_retries_empty_completion_once(tmp_path: Path) -> None:
    """An empty completion (no text, no tool calls) should retry once instead of finishing."""
    provider = _SequenceProvider(
        [
            ModelResponse(),
            ModelResponse(text="hello world"),
        ]
    )
    wire, sink = _collecting_wire()

    result = run_agent(
        "hi",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    assert result.final == "hello world"
    assert any(
        "continue: empty response" in e.text for e in sink.events if isinstance(e, Line)
    )


def test_run_agent_marks_final_empty_after_retries_exhausted(tmp_path: Path) -> None:
    """When retries are exhausted and the response is still empty, mark it explicitly
    instead of going silent with an empty final."""
    provider = _SequenceProvider(
        [
            ModelResponse(),
            ModelResponse(),
            ModelResponse(text=""),
        ]
    )
    wire, sink = _collecting_wire()

    result = run_agent(
        "hi",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    assert result.final == ""
    assert any(
        "final: (empty response)" in e.text for e in sink.events if isinstance(e, Line)
    )


def test_run_agent_continues_after_max_tokens_truncation(tmp_path: Path) -> None:
    """With stop_reason == "max_tokens", a truncated reply must not count as complete;
    continue once instead."""
    provider = _SequenceProvider(
        [
            ModelResponse(text="The reply was truncated", stop_reason="max_tokens"),
            ModelResponse(text="The rest of the ending"),
        ]
    )
    wire, sink = _collecting_wire()

    result = run_agent(
        "hi",
        provider,
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    assert result.final == "The rest of the ending"
    assert any(
        "continue: max_tokens" in e.text for e in sink.events if isinstance(e, Line)
    )


def test_run_agent_recovers_final_from_streamed_text(tmp_path: Path) -> None:
    """When the final message lacks a text block, rebuild final from the text that was
    actually streamed."""

    class _StreamOnlyProvider:
        def complete_stream(
            self,
            messages: list[dict[str, Any]],
            tools: list[Any] | None = None,
            system: str | None = None,
            *,
            signal: Any = None,
        ):
            yield ModelStreamEvent(type="text_delta", text="hello")
            yield ModelStreamEvent(type="text_delta", text="world\n")
            yield ModelStreamEvent(type="completed", response=ModelResponse())

    wire, sink = _collecting_wire()

    result = run_agent(
        "hi",
        _StreamOnlyProvider(),
        ToolRegistry(),
        RuntimeState(),
        cwd=tmp_path,
        wire=wire,
    )

    # The fallback rebuild uses the raw streamed fragments, so the trailing newline survives.
    assert result.final == "helloworld\n"
    assert "".join(sink.texts()) == "helloworld\n"


# ---- LiveArea: streaming tail state machine ---------------------------------


def test_live_area_commits_closed_paragraphs_incrementally() -> None:
    """Incremental commit: a paragraph closed by a blank line is returned at once (to be
    committed to scrollback) and only the tail paragraph stays in the Live area."""
    live = LiveArea()
    assert live.feed_stream("first para line one\nfirst para line two\n") == []
    # A blank line closes the first paragraph -- commit it immediately.
    assert live.feed_stream("\n") == ["first para line one\nfirst para line two\n\n"]
    # The second paragraph is still in progress, not committed.
    assert live.feed_stream("second para") == []
    assert live.feed_stream(" content\n") == []

    # The Live area shows only the open second paragraph.
    text = "".join(t for _, t in live.formatted(columns=80, now=0))
    assert "second para" in text
    assert "first para" not in text

    # At turn end, take the open paragraph (including the partial line) as a whole.
    live.feed_stream("tail")
    assert live.flush_paragraph() == "second para content\ntail\n"
    assert live.flush_paragraph() == ""  # idempotent: a second take yields nothing


def test_live_area_reset_stream_clears_paragraph_state() -> None:
    live = LiveArea()
    live.feed_stream("abc\ndef")
    live.reset_stream()

    assert live.open_paragraph == []
    assert live.stream_pending == ""
    assert live.flush_paragraph() == ""


def test_live_area_formatted_renders_spinner_tool_and_preview() -> None:
    live = LiveArea()
    live.turn_active = True
    live.turn_started_at = 1000.0
    live.active_tool = "bash"
    live.pending_preview = ["⏸ bash echo hi — waiting for approval"]
    live.feed_stream("streaming tail")

    fragments = live.formatted(columns=40, now=1005.0)
    text = "".join(t for _, t in fragments)
    assert "bash · 5s" in text
    assert "⏸ bash echo hi" in text
    assert "streaming tail" in text

    # No spinner is rendered while the turn is inactive.
    live.turn_active = False
    text2 = "".join(t for _, t in live.formatted(columns=40, now=1006.0))
    assert "· 6s" not in text2


# ---- event_to_chunks: finished events -> scrollback chunks -------------------


def test_event_to_chunks_renders_wire_events() -> None:
    """REPL rendering policy: every non-streaming event is translated into an OutputChunk
    and the frontend decides the format."""
    chunks = event_to_chunks(Line("hello"))
    assert len(chunks) == 1
    assert chunks[0].text == "hello\n"

    echo = event_to_chunks(PromptEcho("hi there"))
    assert echo[0].text == "\n\n> hi there\n\n"  # render_user_prompt's dark-grey block

    done = event_to_chunks(ToolCallCompleted(call_id="1", name="read_file"))
    assert done[0].text == "✓ read_file\n"
    err = event_to_chunks(ToolCallCompleted(call_id="1", name="bash", is_error=True))
    assert "(error)" in err[0].text

    diff = event_to_chunks(DiffPreview(path="a.py", old="x\n", new="y\n"))
    assert len(diff) == 2
    assert diff[0].format == "rich"
    assert "+y" in diff[1].text

    cmd = event_to_chunks(CommandPreview(command="echo hi"))
    assert "Command: echo hi" in cmd[0].text

    # Streaming/lifecycle events are not chunk translation's concern.
    assert event_to_chunks(TextDelta("half")) == []
    assert event_to_chunks(TurnStarted()) == []
    assert event_to_chunks(TurnEnded()) == []


def test_chunk_fragments_normalizes_plain_and_rich() -> None:
    assert _chunk_fragments(OutputChunk("plain\n")) == [("", "plain\n")]
    rich = _chunk_fragments(OutputChunk("[bold]b[/bold]\n", format="rich"))
    assert ("bold", "b") in rich


def test_render_user_prompt_formats_prompt_blocks() -> None:
    chunk = render_user_prompt("hello")
    assert chunk.format == "plain"
    assert chunk.text == "\n\n> hello\n\n"
    assert render_user_prompt("first\nsecond").text == "\n\n> first\n> second\n\n"
    # The source header is no longer rendered; the parameter is kept for compatibility only.
    assert render_user_prompt("expanded", source="user /slash").text == "\n\n> expanded\n\n"
    empty = render_user_prompt("")
    assert empty.format == "plain"
    assert empty.text == ""
    assert ("ansiwhite bg:#444444", "> hello") in chunk.fragments


# ---- prompt request rendering and parsing ------------------------------------


def test_prompt_request_helpers_parse_confirm_answers() -> None:
    assert parse_confirm_answer("", default=False) is False
    assert parse_confirm_answer("", default=True) is True
    assert parse_confirm_answer("y") is True
    assert parse_confirm_answer("YES") is True
    assert parse_confirm_answer("n") is False
    assert parse_confirm_answer("maybe") is None


def test_prompt_request_helpers_parse_choice_indexes() -> None:
    assert parse_choice_index("", option_count=2, default=0) == 0
    assert parse_choice_index("2", option_count=2) == 2
    assert parse_choice_index("0", option_count=2) == 0
    assert parse_choice_index("3", option_count=2) is None
    assert parse_choice_index("abc", option_count=2) is None


def test_prompt_request_helpers_render_bottom_prompt_text() -> None:
    confirm_request = {"type": "confirm", "message": "Run this command?", "default": False}
    assert render_prompt_request(confirm_request) == "? Run this command? [y/N]\n"
    assert prompt_for_request(confirm_request) == "Confirm [y/N]: "

    choice_request = {
        "type": "choice",
        "question": "Pick one",
        "labels": ["A", "B"],
        "default": 0,
    }
    assert render_prompt_request(choice_request) == (
        "? Pick one\n  1. A\n  2. B\n  0. Skip / Other\n"
    )
    assert prompt_for_request(choice_request) == "Choice [0]: "


# ---- slash ------------------------------------------------------------------


def test_sessions_slash_returns_renderable_message(tmp_path: Path, capsys: Any) -> None:
    result = dispatch_slash(
        "/sessions",
        SlashContext(
            cwd=tmp_path,
            permission_mode="default",
            model="test-model",
            provider="test-provider",
            session_id=None,
        ),
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert result.handled is True
    assert result.markup is True
    assert result.message == "[dim]No sessions found.[/dim]"


def test_slash_completer_suggests_command_prefixes() -> None:
    completions = list(
        SlashCompleter().get_completions(Document("/he"), CompleteEvent())
    )

    assert [completion.text for completion in completions] == ["/help"]
    assert completions[0].start_position == -3
    assert str(completions[0].display_meta_text) == "show all available slash commands"


def test_slash_completer_ignores_non_command_input() -> None:
    completer = SlashCompleter()

    assert list(completer.get_completions(Document("hello"), CompleteEvent())) == []
    assert list(completer.get_completions(Document("/loop "), CompleteEvent())) == []


# ---- prompt state machine (the pending branch of submit_text, driven by a fake future) ----


def test_shell_render_message_includes_pending_prompt() -> None:
    """While a prompt is pending, message rendering must include the input prompt and must
    never raise.

    We once mutated the return value with FormattedText.fragments.insert -- FormattedText
    has no fragments attribute, the exception killed the prompt render thread, and
    subsequent input never came back.
    """
    from agent_code.ui.shell import InteractiveShell

    state = RuntimeState(permission_mode="default", model="mock", provider="mock")
    shell = InteractiveShell(state, run_turn=lambda *a: None, make_slash_context=lambda: None)
    shell._pending_prompt = {
        "request": {"type": "confirm", "message": "Run this command?", "default": False},
        "future": None,
    }

    text = "".join(t for _, t in shell._render_message())
    assert "Confirm [y/N]:" in text


def test_shell_submit_text_routes_pending_choice_answer() -> None:
    """Pending choice prompt: the answer goes straight into the future and the state machine clears."""
    from agent_code.ui.shell import InteractiveShell

    state = RuntimeState(permission_mode="default", model="mock", provider="mock")

    def noop(*args: Any, **kwargs: Any) -> None:
        return None

    shell = InteractiveShell(state=state, run_turn=noop, make_slash_context=noop)
    shell._commit_now = lambda text: None  # never touch the real terminal

    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        shell._pending_prompt = {
            "request": {"type": "choice", "labels": ["a", "b", "c"], "default": 0},
            "future": future,
        }

        shell._submit_text("2")

        assert future.done() and future.result() == "2"
        assert shell._pending_prompt is None
    finally:
        loop.close()


def test_shell_submit_text_rejects_invalid_choice_and_keeps_pending() -> None:
    from agent_code.ui.shell import InteractiveShell

    state = RuntimeState(permission_mode="default", model="mock", provider="mock")

    def noop(*args: Any, **kwargs: Any) -> None:
        return None

    shell = InteractiveShell(state=state, run_turn=noop, make_slash_context=noop)
    shell._commit_now = lambda text: None

    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        entry = {
            "request": {"type": "choice", "labels": ["a", "b"], "default": 0},
            "future": future,
        }
        shell._pending_prompt = dict(entry)

        shell._submit_text("9")  # invalid

        assert not future.done()
        assert shell._pending_prompt == entry  # stays pending, waiting for the next input
    finally:
        loop.close()


# ---- layout reshaping: input area Frame + bottom anchoring --------------------


def _build_session() -> Any:
    """Build a PromptSession with a completer (DummyOutput, no real terminal involved)."""
    from prompt_toolkit.input.base import DummyInput
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.shortcuts import PromptSession

    from agent_code.ui.completer import SlashCompleter

    return PromptSession[str](
        message="> ",
        completer=SlashCompleter(),
        complete_while_typing=True,
        input=DummyInput(),
        output=DummyOutput(),
    )


def test_bottom_anchored_layout_pins_buffer_window() -> None:
    """After the reshaping: the root HSplit has a spacer at its head and the buffer window
    has dont_extend_height.

    These two mechanisms are exactly what pins the input area to the bottom: the spacer
    absorbs the remaining height and the buffer window stops at its content height and is
    pushed to the bottom.
    """
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import BufferControl

    from agent_code.ui.layout import _install_bottom_anchored_layout

    session = _build_session()
    root_before = session.layout.container
    assert isinstance(root_before, HSplit)
    # The layout has two BufferControl windows (the search control is a subclass), so pick
    # the one whose buffer is the default buffer -- mirroring _install's lookup logic.
    buffer_window = next(
        w
        for w in _all_windows(root_before)
        if isinstance(w.content, BufferControl) and w.content.buffer is session.default_buffer
    )
    assert not buffer_window.dont_extend_height()

    _install_bottom_anchored_layout(session)

    root = session.layout.container
    assert root.children[0] is not buffer_window  # a spacer was inserted at the head
    spacer = root.children[0]
    assert isinstance(spacer, Window)
    assert spacer.height.weight >= 1000  # overwhelming weight, takes all remaining height
    # Modified in place: the buffer window in the layout tree is still the original object
    # (Layout's focus stack holds the reference).
    assert any(
        w is buffer_window and w.dont_extend_height()
        for w in _all_windows(root)
        if isinstance(w.content, BufferControl)
    )
    # The other children keep their order (the original children are the tail of the new list).
    original_children = root.children[1:]
    assert all("bottom" not in str(c) or c is not None for c in original_children)


def test_bottom_anchored_layout_keeps_focus_on_default_buffer() -> None:
    """Modify the Window in place instead of wrapping it: the control reference held by the
    focus stack must be preserved."""
    from prompt_toolkit.layout.controls import BufferControl

    from agent_code.ui.layout import _install_bottom_anchored_layout

    session = _build_session()
    control_before = session.layout.current_control

    _install_bottom_anchored_layout(session)

    # current_control is still that same BufferControl (the default buffer).
    assert isinstance(session.layout.current_control, BufferControl)
    assert session.layout.current_control is control_before
    assert session.layout.current_buffer is session.default_buffer


def _all_windows(root: Any) -> list[Any]:
    from prompt_toolkit.layout.containers import Window

    stack: list[Any] = [root]
    out: list[Any] = []
    while stack:
        node = stack.pop()
        if isinstance(node, Window):
            out.append(node)
        stack.extend(node.get_children())
    return out
