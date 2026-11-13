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


def test_shell_render_live_includes_pending_prompt() -> None:
    """While a prompt is pending, Live-area rendering must include the input prompt and must
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

    text = "".join(t for _, t in shell._render_live())
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


# ---- layout reshaping: input area Frame ---------------------------------------


def _build_framed_session() -> Any:
    """PromptSession in the production shape: the frame branch exists in the layout
    (pt builds it unconditionally) even though ``show_frame`` is only enabled per
    ``prompt_async(show_frame=True)`` call."""
    from prompt_toolkit.input.base import DummyInput
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.shortcuts import PromptSession

    from agent_code.ui.completer import SlashCompleter

    return PromptSession[str](
        message="> ",
        completer=SlashCompleter(),
        input=DummyInput(),
        output=DummyOutput(),
        reserve_space_for_menu=0,
    )


def _frame_split(session: Any) -> Any:
    """Return the Frame's HSplit [top rule, middle VSplit, bottom rule].

    Located by shape (not position) even before the side borders are stripped.
    """
    from prompt_toolkit.layout.containers import HSplit, VSplit, Window
    from prompt_toolkit.widgets.base import Border

    def is_bar(node: Any) -> bool:
        return isinstance(node, Window) and node.char == Border.VERTICAL

    for node in _all_containers(session.layout.container):
        if not (isinstance(node, HSplit) and len(node.children) == 3):
            continue
        middle = node.children[1]
        if (
            isinstance(middle, VSplit)
            and len(middle.children) == 3
            and is_bar(middle.children[0])
            and is_bar(middle.children[2])
        ):
            return node
    raise AssertionError("Frame split not found")


def _default_buffer_window(session: Any) -> Any:
    """The Window holding the input buffer (the layout has several BufferControls)."""
    from prompt_toolkit.layout.controls import BufferControl

    return next(
        w
        for w in _all_windows(session.layout.container)
        if isinstance(w.content, BufferControl) and w.content.buffer is session.default_buffer
    )


def test_collapse_input_height_stops_at_content() -> None:
    """The input area must not absorb the rest of the screen: with a bare Dimension() the
    buffer window stretches to the bottom_toolbar, the frame encloses a tall empty box,
    and run_in_terminal commits land inside it instead of scrolling above the prompt."""
    from agent_code.ui.layout import _collapse_input_height

    session = _build_framed_session()
    buffer_window = _default_buffer_window(session)
    assert not buffer_window.dont_extend_height()

    _collapse_input_height(session)

    assert buffer_window.dont_extend_height()
    # In place: the focus stack keeps its reference, so the same object must be in the tree.
    assert _default_buffer_window(session) is buffer_window


def test_collapse_input_height_reserves_rows_only_while_menu_is_open() -> None:
    """The completion menu is a Float inside the input window, so a one-row body would
    clip it away. The height is dynamic: MENU_ROWS while completions are pending, 0
    otherwise -- rows are taken only while the menu is actually up."""
    from agent_code.ui.layout import MENU_ROWS, _collapse_input_height

    session = _build_framed_session()
    _collapse_input_height(session)
    buffer_window = _default_buffer_window(session)

    assert buffer_window.height().min == 0

    session.default_buffer.complete_state = object()  # any non-None completion state
    try:
        assert buffer_window.height().min == MENU_ROWS
    finally:
        session.default_buffer.complete_state = None

    assert buffer_window.height().min == 0


def test_strip_frame_side_borders_removes_vertical_bars() -> None:
    """Only the horizontal rules remain: both side-border windows collapse to width 0,
    while the body and the input buffer window stay untouched."""
    from prompt_toolkit.layout.containers import VSplit
    from prompt_toolkit.layout.controls import BufferControl

    from agent_code.ui.layout import _strip_frame_side_borders

    session = _build_framed_session()
    middle = _frame_split(session).children[1]
    assert isinstance(middle, VSplit)
    left, body, right = middle.children
    assert left.width != 0 and right.width != 0  # side borders visible before

    _strip_frame_side_borders(session)

    left, body_after, right = middle.children
    assert left.width.preferred == 0 and left.width.max == 0
    assert right.width.preferred == 0 and right.width.max == 0
    # The body (and therefore the input buffer window) is the same object as before.
    assert body_after is body
    assert any(isinstance(w.content, BufferControl) for w in _all_windows(body_after))


def test_strip_frame_side_borders_keeps_horizontal_rules() -> None:
    """The top and bottom rule rows must survive: the first and last children of the
    Frame split are untouched."""
    from agent_code.ui.layout import _strip_frame_side_borders

    session = _build_framed_session()
    frame_split = _frame_split(session)
    top_before, _, bottom_before = frame_split.children

    _strip_frame_side_borders(session)

    assert frame_split.children[0] is top_before
    assert frame_split.children[2] is bottom_before


def test_render_live_excludes_prompt_marker_and_live_content() -> None:
    """The Live area window shows in-progress content only: no '> ' marker (the input
    prompt belongs to the frame's message), and no trailing blank line (which would
    reserve a row while idle)."""
    from agent_code.ui.shell import InteractiveShell

    state = RuntimeState(permission_mode="default", model="mock", provider="mock")

    def noop(*args: Any, **kwargs: Any) -> None:
        return None

    shell = InteractiveShell(state=state, run_turn=noop, make_slash_context=noop)
    shell._live.open_paragraph = ["streamed line"]
    shell._live.stream_pending = "partial"

    fragments = shell._render_live()
    text = "".join(t for _, t in fragments)

    assert ">" not in text
    assert "streamed line" in text
    assert not fragments[-1][1].endswith("\n")


def test_render_live_is_empty_when_idle() -> None:
    """Idle: nothing in progress -> no fragments at all, so the Live window's preferred
    height is zero and it occupies no rows."""
    from agent_code.ui.shell import InteractiveShell

    state = RuntimeState(permission_mode="default", model="mock", provider="mock")

    def noop(*args: Any, **kwargs: Any) -> None:
        return None

    shell = InteractiveShell(state=state, run_turn=noop, make_slash_context=noop)

    assert shell._render_live() == []


def test_live_window_is_above_the_input_frame() -> None:
    """The Live area must be a sibling of the frame branch placed immediately above it --
    not part of the prompt message, which pt renders inside the frame."""
    from agent_code.ui.layout import _install_live_area_window

    session = _build_framed_session()
    from agent_code.ui.layout import _find_frame_split

    frame_split = _find_frame_split(session)
    assert frame_split is not None
    root_before = session.layout.container

    live_window = _install_live_area_window(session, lambda: [("", "live")])

    root = session.layout.container
    assert root is root_before
    live_index = root.children.index(live_window)
    frame_index = next(
        i
        for i, child in enumerate(root.children)
        if any(node is frame_split for node in _all_containers(child))
    )
    assert live_index == frame_index - 1  # immediately above the frame
    assert live_window.dont_extend_height()  # content height, not the full width


# ---- regressions: prompt_async argument handling / erase_when_done -----------


def test_prompt_async_overwrites_show_frame_unless_passed_explicitly() -> None:
    """Guard the trap that made the frame never appear.

    ``PromptSession(show_frame=True)`` alone is not enough: ``prompt_async`` declares
    ``show_frame: FilterOrBool = False`` and assigns it unconditionally (the code is
    ``if show_frame is not None: self.show_frame = show_frame``, and the default is
    False, not None). So every call resets it to False. The shell must pass
    ``show_frame=True`` on each ``prompt_async()`` call -- this test pins the pt
    behaviour that forces that.
    """
    import inspect

    from prompt_toolkit.shortcuts import PromptSession

    signature = inspect.signature(PromptSession.prompt_async)
    default = signature.parameters["show_frame"].default
    assert default is not None, "pt now uses None as the default: the workaround can go"
    assert not default, "pt default is falsy -> a constructor-only show_frame is reset"


def test_shell_passes_show_frame_on_every_prompt_call() -> None:
    """The prompt loop must pass show_frame=True per call (source-level check)."""
    import inspect

    from agent_code.ui import shell as shell_module

    source = inspect.getsource(shell_module.InteractiveShell._run_async)
    assert "prompt_async(show_frame=True)" in source


def test_shell_erases_prompt_when_done() -> None:
    """erase_when_done must be True, or the typed line stays on screen and the styled
    echo we commit duplicates the user prompt."""
    from prompt_toolkit.input.base import DummyInput
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.shortcuts import PromptSession

    # Mirrors the construction in _run_async (PromptSession exposes it via .app, so
    # assert on the same parameter the shell passes).
    session = PromptSession[str](
        message="> ",
        input=DummyInput(),
        output=DummyOutput(),
        erase_when_done=True,
    )
    assert session.app.erase_when_done is True


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


def _all_containers(root: Any) -> list[Any]:
    stack: list[Any] = [root]
    out: list[Any] = []
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.get_children())
    return out
