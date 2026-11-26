"""Event translation, transcript state-machine, and rendering tests (frontend-agnostic).

Never touches a real terminal: LiveArea / event_to_chunks / TranscriptSink / the
output fragment pipeline are pure functions or injectable state objects, and
run_agent is driven by a fake provider. These are the pieces the one-shot sink and
the Textual frontend share.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.cells import cell_len

from agent_code.agent import run_agent
from agent_code.model import ModelResponse, ModelStreamEvent
from agent_code.output import (
    OutputChunk,
    chunk_fragments,
    markdown_fragments,
    pad_full_width,
)
from agent_code.runtime import RuntimeState
from agent_code.slash import SlashContext, dispatch_slash
from agent_code.tools import ToolRegistry
from agent_code.tui.transcript import TranscriptSink
from agent_code.ui.completer import SlashCompleter
from agent_code.ui.events import event_to_chunks, render_user_prompt
from agent_code.ui.live import LiveArea, cap_preview_lines
from agent_code.ui.prompts import (
    parse_choice_index,
    parse_confirm_answer,
    prompt_for_request,
    render_prompt_request,
)
from agent_code.wire import (
    CommandPreview,
    DiffPreview,
    Line,
    PromptEcho,
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


def _sink_for_batch() -> TranscriptSink:
    """Fresh TranscriptSink for consume-batch ordering tests."""
    return TranscriptSink()


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
    # Raw deltas go onto the wire unchanged; assembling lines/paragraphs is the
    # consumer's job (LiveArea.feed_stream in the REPL).
    assert "".join(sink.texts()) == final_text


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
    assert any("continue: empty response" in e.text for e in sink.events if isinstance(e, Line))


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
    assert any("final: (empty response)" in e.text for e in sink.events if isinstance(e, Line))


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
    assert any("continue: max_tokens" in e.text for e in sink.events if isinstance(e, Line))


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


def test_live_area_code_fence_survives_blank_lines() -> None:
    """Blank lines INSIDE a fenced code block must not close the block: a markdown
    code sample with empty lines is committed as ONE block, not split mid-fence."""
    live = LiveArea()
    assert live.feed_stream("para\n```python\n\nx = 1\n\n```\n") == []
    # Only the blank line AFTER the closing fence closes the block.
    assert live.feed_stream("\nnext\n") == ["para\n```python\n\nx = 1\n\n```\n\n"]

    # An UNTERMINATED fence keeps accumulating (until the closer or turn end).
    live2 = LiveArea()
    assert live2.feed_stream("```py\n\ncode\n") == []
    assert live2.flush_paragraph() == "```py\n\ncode\n"


def test_markdown_fragments_style_real_markdown() -> None:
    """markdown_fragments produces real Rich styles: bold heading, bold text, styled
    code block -- and the plain text survives verbatim (no markup parsing)."""
    frags = markdown_fragments("# Title\n\n**bold** text\n")
    text = "".join(t for _, t in frags)
    assert "Title" in text and "bold" in text and "text" in text
    styles = {s for s, _ in frags}
    assert any("bold" in s for s in styles)  # heading + **bold** got styled
    # Markdown control chars are consumed by the renderer: "#" and "**" are gone.
    assert "#" not in text.replace("Title", "")
    assert "**" not in text


def test_fragment_styles_are_rich_vocabulary_and_parseable() -> None:
    """Fragment styles are Rich inline style strings: every one must parse.

    Regression for: code fences rendered all-underlined because rich's pygments theme
    emits Style(underline=False). The style string must keep the negation
    ("not underline"), never a bare "underline".
    """
    from rich.style import Style

    sample = (
        "# Title\n\n**bold** and *italic* with `inline` code\n\n"
        "```python\ndef foo():\n    return 1  # comment\n```\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n[link](http://x)\n"
    )
    fragments = markdown_fragments(sample)
    assert fragments
    for style, _ in fragments:
        if not style:
            continue
        Style.parse(style)  # raises on anything Rich cannot consume

    # Headings and links legitimately use underline; the regression is that code
    # tokens acquire it. Check the code fence content specifically.
    code_fragments = markdown_fragments("```python\ndef foo():\n    return 1\n```\n")
    code_styles = [style for style, _ in code_fragments]
    # The pygments theme disables underline explicitly; the negation must survive.
    assert any("not underline" in style for style in code_styles)
    # Every literal underline token must be preceded by "not" (rich splits attributes
    # into separate tokens, so check the token sequence, not the substring).
    for style in code_styles:
        tokens = style.split()
        for index, token in enumerate(tokens):
            if token == "underline":
                assert index > 0 and tokens[index - 1] == "not", style


def test_markdown_fragments_verbatim_content_with_brackets() -> None:
    """Content like lst[0] or [link] must survive verbatim -- markdown rendering does
    not parse Rich markup, so no injection issue."""
    frags = markdown_fragments("use `lst[0]` and [not-a-style] here\n")
    text = "".join(t for _, t in frags)
    assert "lst[0]" in text
    assert "[not-a-style]" in text


def test_live_area_reset_stream_clears_paragraph_state() -> None:
    live = LiveArea()
    live.feed_stream("abc\ndef")
    live.reset_stream()

    assert live.open_paragraph == []
    assert live.stream_pending == ""
    assert live.flush_paragraph() == ""


def test_live_area_mid_turn_flush_keeps_fence_state() -> None:
    """A prose flush before a tool call must not close an unterminated code fence: the
    blank line that follows would otherwise split the block in two."""
    live = LiveArea()
    live.feed_stream("```python\n")
    assert live.open_paragraph == ["```python"]

    flushed = live.flush_paragraph(close_fence=False)
    assert flushed == "```python\n"
    assert live.open_paragraph == []

    # Still inside the fence: a blank line does not emit a block.
    assert live.feed_stream("\n") == []
    assert live.feed_stream("x = 1\n") == []
    assert live.feed_stream("```\n\n") == ["x = 1\n```\n\n"]


# ---- consume-batch ordering: scrollback must read in event order -------------


def test_consume_batch_commits_prose_before_the_tool_that_followed_it() -> None:
    """The reported bug: prose streamed before a tool call stayed in the Live area until
    a blank line closed it, so the tool lines (committed immediately) appeared ABOVE
    their own preamble."""
    sink = _sink_for_batch()

    first = sink.handle_batch([TurnStarted(), TextDelta("Let me check git first.")])
    assert first == []  # still the open paragraph in the Live area

    second = sink.handle_batch(
        [
            ToolCallStarted(call_id="1", name="git_status", args_preview="{}"),
            ToolCallCompleted(call_id="1", name="git_status"),
        ]
    )
    # Prose is committed at once; the tool burst stays open (the model may call more
    # tools) and its summary lands when the burst closes.
    assert [chunk.text for chunk in second] == ["Let me check git first.\n\n"]
    assert sink.tool_fold == [("git_status", "ok")]

    third = sink.handle_batch([TextDelta("Done. Nothing to report."), TurnEnded()])
    assert [chunk.text for chunk in third] == [
        "✓ git_status\n",
        "Done. Nothing to report.\n",
    ]
    assert sink.live.open_paragraph == []
    assert sink.tool_fold == []


def test_consume_batch_folds_a_tool_burst_into_one_line() -> None:
    """Twenty tool calls must not occupy twenty rows: one burst -> one summary line."""
    sink = _sink_for_batch()
    calls: list[Any] = [TurnStarted()]
    for index in range(4):
        calls.append(ToolCallStarted(call_id=str(index), name="bash", args_preview="{}"))
        calls.append(ToolCallCompleted(call_id=str(index), name="bash"))
    calls.append(TurnEnded())

    chunks = sink.handle_batch(calls)

    assert [chunk.text for chunk in chunks] == ["✓ 4 tools: bash ×4\n"]
    assert sink.tool_fold == []


def test_consume_batch_tool_summary_before_resumed_prose() -> None:
    """The burst closes when the model speaks again, so the summary precedes that prose."""
    sink = _sink_for_batch()
    sink.handle_batch(
        [
            TurnStarted(),
            ToolCallStarted(call_id="1", name="read_file", args_preview="{}"),
            ToolCallCompleted(call_id="1", name="read_file"),
        ]
    )

    chunks = sink.handle_batch([TextDelta("The file says hi."), TurnEnded()])
    assert [chunk.text for chunk in chunks] == ["✓ read_file\n", "The file says hi.\n"]


def test_consume_batch_records_declined_tools_in_the_fold() -> None:
    """A rejected confirmation settles the call (no hanging spinner) and is visible in
    the folded summary."""
    sink = _sink_for_batch()
    chunks = sink.handle_batch(
        [
            TurnStarted(),
            ToolCallStarted(call_id="1", name="bash", args_preview="{}"),
            ToolCallCompleted(call_id="1", name="bash", is_error=True, outcome="declined"),
            TurnEnded(),
        ]
    )

    assert [chunk.text for chunk in chunks] == ["⊘ bash (declined)\n"]


def test_tool_fold_formatting() -> None:
    from agent_code.ui.events import format_tool_fold

    assert format_tool_fold([]) == ""
    assert format_tool_fold([("bash", "ok")]) == "✓ bash"
    assert format_tool_fold([("bash", "declined")]) == "⊘ bash (declined)"
    assert format_tool_fold([("read_file", "skipped")]) == "⊘ read_file (skipped)"
    assert (
        format_tool_fold([("bash", "ok"), ("bash", "ok"), ("git_diff", "error")])
        == "✗ 3 tools: bash ×2, git_diff (error)"
    )
    # Distinct names beyond the cap are elided, keeping the line terminal-width sized.
    elided = format_tool_fold([(f"t{index}", "ok") for index in range(9)])
    assert elided == "✓ 9 tools: t0, t1, t2, t3, t4, t5, …"


def test_live_area_formatted_renders_gap_spinner_tool_and_preview() -> None:
    """The Live window starts with one blank gap line, then the spinner."""
    live = LiveArea()
    live.turn_active = True
    live.turn_started_at = 1000.0
    live.active_tool = "bash"
    live.pending_preview = ["⏸ bash echo hi — waiting for approval"]
    live.feed_stream("streaming tail")

    fragments = live.formatted(columns=40, now=1005.0)
    assert fragments[0] == ("", "\n")  # exactly one blank separator line
    assert fragments[1][0] == "class:spinner"
    text = "".join(t for _, t in fragments)
    assert "bash · 5s" in text
    assert "⏸ bash echo hi" in text
    assert "streaming tail" in text

    # No spinner or gap is rendered while the turn is inactive.
    live.turn_active = False
    text2 = "".join(t for _, t in live.formatted(columns=40, now=1006.0))
    assert "· 6s" not in text2
    assert not text2.startswith("\n")


# ---- event_to_chunks: finished events -> scrollback chunks -------------------


def test_event_to_chunks_renders_wire_events() -> None:
    """REPL rendering policy: every non-streaming event is translated into an OutputChunk
    and the frontend decides the format."""
    chunks = event_to_chunks(Line("hello"))
    assert len(chunks) == 1
    assert chunks[0].text == "hello\n"

    echo = event_to_chunks(PromptEcho("hi there"))
    assert echo[0].text == "\n\n> hi there\n"  # render_user_prompt's dark-grey block

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
    assert chunk_fragments(OutputChunk("plain\n")) == [("", "plain\n")]
    rich = chunk_fragments(OutputChunk("[bold]b[/bold]\n", format="rich"))
    assert ("bold", "b") in rich
    # Explicit fragments are adopted as-is.
    explicit = OutputChunk("x", fragments=(("bold", "x"),))
    assert chunk_fragments(explicit) == [("bold", "x")]


def test_cap_preview_lines_marks_the_elision() -> None:
    lines = [f"line {i}" for i in range(30)]
    capped = cap_preview_lines(lines, max_lines=5)
    assert capped[:5] == lines[:5]
    assert capped[5] == "... (25 lines omitted)"
    assert len(capped) == 6
    assert cap_preview_lines(["only"], max_lines=5) == ["only"]


def test_render_user_prompt_formats_prompt_blocks() -> None:
    chunk = render_user_prompt("hello")
    assert chunk.format == "plain"
    assert chunk.full_width is True
    assert chunk.text == "\n\n> hello\n"
    assert render_user_prompt("first\nsecond").text == "\n\n> first\n> second\n"
    empty = render_user_prompt("")
    assert empty.format == "plain"
    assert empty.text == ""
    assert ("bold white on #444444", "> hello") in chunk.fragments
    assert chunk.fragments[-1] == ("", "\n")


def test_prompt_echo_and_live_spinner_have_exactly_one_blank_gap() -> None:
    """Prompt echo ends with one newline; the Live area adds one blank line."""
    chunk = render_user_prompt("hello")
    assert chunk.text.endswith("\n") and not chunk.text.endswith("\n\n")

    live = LiveArea()
    live.turn_active = True
    live.turn_started_at = 1000.0
    fragments = live.formatted(columns=80, now=1001.0)
    assert fragments[0] == ("", "\n")


def test_prompt_chunk_keeps_its_background_and_pads() -> None:
    """Prompt echoes carry their background style and pad to the commit width."""
    chunk = render_user_prompt("hello")
    fragments = chunk_fragments(chunk)
    assert ("bold white on #444444", "> hello") in fragments

    padded = "".join(part for _, part in pad_full_width(fragments, 40))
    assert "> hello" + " " * (40 - len("> hello")) + "\n" in padded

    # >80 columns is the regression case for the offscreen Console's width=4096.
    wide = "".join(part for _, part in pad_full_width(fragments, 132))
    assert "> hello" + " " * (132 - len("> hello")) + "\n" in wide


def test_pad_full_width_accumulates_across_fragments() -> None:
    """Multiple styled fragments on one visual line produce one padded line."""
    padded = pad_full_width(
        [("bold", "> "), ("white on #444444", "hello"), ("", "\n")],
        columns=20,
    )
    text = "".join(part for _, part in padded)
    assert text == "> hello" + " " * (20 - len("> hello")) + "\n"
    assert text.count("\n") == 1


def test_pad_full_width_expands_tabs_and_skips_overwide_lines() -> None:
    """Tabs count as real columns; over-wide lines are left alone."""
    tabbed = pad_full_width([("style", "a\tb\n")], columns=20)
    tab_text = "".join(part for _, part in tabbed)
    assert "\t" not in tab_text
    assert tab_text == "a       b" + " " * (20 - len("a       b")) + "\n"

    wide = pad_full_width([("style", "x" * 30 + "\n")], columns=20)
    assert "".join(part for _, part in wide) == "x" * 30 + "\n"


def _max_line_width(fragments: list[tuple[str, str]]) -> int:
    """Widest visual line in a fragment list, in terminal cells."""
    return max(cell_len(line) for line in "".join(t for _, t in fragments).split("\n"))


def test_markdown_wraps_at_the_requested_width() -> None:
    """Markdown is hard-wrapped by Rich, so the wrap width must be the terminal's.

    Regression: markdown_fragments used a fixed 100 columns, so a wider terminal got
    prose that stopped short of the right edge and a narrower one wrapped early.
    """
    prose = " ".join(["word"] * 200) + "\n"

    narrow = chunk_fragments(OutputChunk(prose, format="markdown"), 60)
    wide = chunk_fragments(OutputChunk(prose, format="markdown"), 120)

    assert _max_line_width(narrow) <= 60
    assert _max_line_width(wide) <= 120
    assert _max_line_width(wide) > 100  # it really used the wider target
    narrow_text = "".join(t for _, t in narrow)
    wide_text = "".join(t for _, t in wide)
    assert narrow_text.count("\n") > wide_text.count("\n")

    # Callers without a terminal width keep the deterministic fallback.
    default = chunk_fragments(OutputChunk(prose, format="markdown"))
    assert _max_line_width(default) <= 100


def test_commit_renders_at_the_passed_width() -> None:
    """The commit renderer wraps markdown and pads prompts at the width it is given."""
    from agent_code.tui.render import chunk_to_text

    prose = " ".join(["word"] * 200) + "\n"
    rendered = chunk_to_text(OutputChunk(prose, format="markdown"), width=72)
    widths = [cell_len(line) for line in rendered.plain.split("\n")]
    assert max(widths) <= 72
    assert max(widths) > 40  # wrapped at 72, not at the 100-column fallback

    prompt = chunk_to_text(render_user_prompt("hi"), width=33)
    bar = [line for line in prompt.plain.split("\n") if "hi" in line][0]
    assert bar == "> hi" + " " * (33 - len("> hi"))


def test_live_tail_truncates_by_display_width() -> None:
    """Wide text is cut by cells, not characters, so the Live window keeps one row."""
    from agent_code.ui.live import _fit_width

    assert _fit_width("abc", 10) == "abc"
    assert _fit_width("中文中文", 5) == "中文"  # 4 cells fit, a third char would make 6
    assert _fit_width("中文" * 20, 7) == "中文中"
    assert cell_len(_fit_width("中文" * 20, 7)) <= 7
    assert _fit_width("anything", 0) == ""


def test_console_wire_sink_writes_a_styled_transcript() -> None:
    """One-shot output also feeds a TranscriptSink, so --continue can replay it."""
    from agent_code.cli import _ConsoleWireSink

    chunks: list[OutputChunk] = []
    sink = _ConsoleWireSink(chunks.append)
    sink.send([TurnStarted(), TextDelta("**hi**"), TextDelta("\n\n"), TurnEnded()])

    assert [chunk.text for chunk in chunks] == ["**hi**\n\n"]
    assert chunks[0].format == "markdown"


def test_transcript_prompt_style_survives_resume(tmp_path: Path) -> None:
    """Transcript fragments round-trip through JSONL and still render on replay."""
    from agent_code.session import Session

    session = Session.create(tmp_path)
    session.append_transcript(render_user_prompt("hello"))
    resumed = Session.load_by_id(tmp_path, session.session_id)

    assert resumed is not None
    chunk = resumed.transcript_chunks[0]
    assert chunk.full_width is True
    padded = "".join(part for _, part in pad_full_width(chunk_fragments(chunk), 40))
    assert "> hello" + " " * (40 - len("> hello")) + "\n" in padded


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
    # render_prompt_request: content BELOW the question line for the Live area.
    # A bare confirm has no content -- the question line alone is the whole prompt.
    assert render_prompt_request(confirm_request) == ""
    # prompt_for_request: the one-line input prompt (goes into the prompt message).
    assert prompt_for_request(confirm_request) == "? Run this command? [y/N]: "

    choice_request = {
        "type": "choice",
        "question": "Pick one",
        "labels": ["A", "B"],
        "default": 0,
    }
    assert render_prompt_request(choice_request) == "  1. A\n  2. B\n  0. Skip / Other\n"
    assert prompt_for_request(choice_request) == "? Pick one (1-2, default 1): "


def test_prompt_request_helpers_render_detail_field() -> None:
    """The detail field (diff / command) is the Live-area content, below the question."""
    request = {
        "type": "confirm",
        "message": "Apply this edit to a.py?",
        "default": False,
        "detail": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-hello\n+hi",
    }
    assert render_prompt_request(request) == ("--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-hello\n+hi\n")
    # The message (question line) must NOT leak into the content: it renders once,
    # in the prompt message.
    assert "Apply this edit" not in render_prompt_request(request)


def test_prompt_request_helpers_render_body_field() -> None:
    """body (e.g. the rendered plan panel) is the Live-area content, below the question."""
    request = {
        "type": "confirm",
        "message": "Approve this plan and exit plan mode?",
        "default": False,
        "body": "╭─ Plan ─╮\n│ do it  │\n╰────────╯",
    }
    assert render_prompt_request(request) == "╭─ Plan ─╮\n│ do it  │\n╰────────╯\n"


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


def test_sessions_slash_with_records_returns_text(tmp_path: Path) -> None:
    """A non-empty /sessions result must be text, not a Rich Table.

    The REPL sink stores ``str(result.message)``, so returning the Table object made
    the scrollback print ``<rich.table.Table object at 0x...>`` -- both frontends now
    get the rendered text.
    """
    from agent_code.session import Session

    session = Session.create(tmp_path)
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

    assert result.handled is True
    assert isinstance(result.message, str)
    assert "<rich.table" not in result.message
    assert session.session_id in result.message
    assert "last updated" in result.message
    # The rendered table survives the REPL's str() unchanged (no repr leak).
    assert str(result.message) == result.message
    assert len(result.message.splitlines()) >= 4


def test_slash_completer_suggests_command_prefixes() -> None:
    suggestions = SlashCompleter().suggestions("/he")

    assert [s.insert for s in suggestions] == ["/help"]
    assert suggestions[0].meta == "show all available slash commands"


def test_slash_completer_prefix_first_contains_fallback() -> None:
    """Two-tier matching: prefix hits rank before contains hits. "/ex" lists /exit
    (prefix) before /context (contains); a mid-word typo "/ELP" still finds /help
    via the contains fallback."""
    completer = SlashCompleter()

    ex_names = [s.insert for s in completer.suggestions("/ex")]
    assert ex_names.index("/exit") < ex_names.index("/context")

    assert [s.insert for s in completer.suggestions("/ELP")] == ["/help"]


def test_slash_completer_ignores_non_command_input() -> None:
    completer = SlashCompleter()

    assert completer.suggestions("hello") == []
    assert completer.suggestions("/loop ") == []


# ---- bottom status bar ------------------------------------------------------


def test_bottom_toolbar_only_shows_mode_and_model() -> None:
    from agent_code.runtime import TodoItem
    from agent_code.ui.status import bottom_toolbar

    state = RuntimeState(permission_mode="plan", model="mock")
    state.todo_store = [TodoItem(content="task", status="in_progress", active_form="working")]
    fragments = bottom_toolbar(state)
    assert "".join(text for _style, text in fragments) == " plan · mock "


def test_bottom_toolbar_shows_context_usage() -> None:
    """With a live budget the toolbar gains a token bar; hot styling at threshold."""
    from agent_code.context import ContextBudget
    from agent_code.ui.status import bottom_toolbar

    state = RuntimeState(permission_mode="default", model="mock")
    budget = ContextBudget(window_tokens=100_000)
    budget.reset(42_000)
    state.context_budget = budget
    fragments = bottom_toolbar(state)
    text = "".join(t for _s, t in fragments)
    assert "[#####-------] 42% 42k/100k tokens" in text
    assert all("ctx.hot" not in s for s, _t in fragments)
    # Each of the three segments carries its own color class.
    assert any("mode.default" in s for s, _t in fragments)
    assert any("seg.model" in s for s, _t in fragments)
    assert any("seg.ctx" in s for s, _t in fragments)

    # At/above the compact threshold the segment switches to the warning style.
    budget.reset(95_000)
    hot = bottom_toolbar(state)
    assert "[###########-] 95% 95k/100k tokens" in "".join(t for _s, t in hot)
    assert any("ctx.hot" in s for s, _t in hot)


def test_bottom_toolbar_mode_segment_tint_tracks_permission_mode() -> None:
    """The mode segment's color class follows the permission mode."""
    from agent_code.ui.status import bottom_toolbar

    for mode, cls in [
        ("default", "mode.default"),
        ("acceptEdits", "mode.edits"),
        ("plan", "mode.plan"),
        ("fullAccess", "mode.full"),
    ]:
        state = RuntimeState(permission_mode=mode, model="mock")
        assert any(cls in s for s, _t in bottom_toolbar(state)), mode


def test_bottom_toolbar_omits_context_without_budget() -> None:
    """No budget (one-shot state) -> no context segment, no misleading zero."""
    from agent_code.ui.status import bottom_toolbar

    state = RuntimeState(permission_mode="default", model="mock")
    text = "".join(t for _s, t in bottom_toolbar(state))
    assert "tokens" not in text
