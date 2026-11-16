"""InteractiveShell: the scrollback+Live REPL itself.

See the package docstring (ui/__init__.py) for the threading and terminal-ownership
model. This module keeps only stateful orchestration: the worker thread (turn
execution / cron dispatch), the UI coroutine (event consumption / spinner / prompts),
and input submission. Key bindings live in ``keys.py``; rendering and parsing live in
the other sibling modules.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import traceback
from typing import Any, Callable

from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.application.current import get_app_or_none, get_app_session
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession

from ..output import OutputChunk
from ..runtime import PromptFallback, PromptRequest, RuntimeState
from ..slash import SlashContext, SlashRegistry, dispatch_slash
from ..wire import (
    Line,
    PromptEcho,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
    Wire,
    WireEvent,
)
from .completer import SlashCompleter
from .events import event_to_chunks, render_user_prompt, tool_fold_chunk, tool_outcome
from .keys import build_key_bindings
from .layout import (
    collapse_input_height,
    install_live_area_window,
    relocate_completion_menus,
    strip_frame_side_borders,
)
from .live import LiveArea, preview_lines
from .prompts import (
    parse_choice_index,
    parse_confirm_answer,
    prompt_for_request,
    render_prompt_request,
)
from .scrollback import commit_chunk
from .theme import bottom_toolbar, build_style

PendingPrompt = dict[str, Any]


class InteractiveShell:
    """The scrollback+Live REPL.

    Threading model: the main thread runs the asyncio event loop (prompt_async + the
    event-consuming coroutine); the worker thread runs run_agent / cron handling and
    communicates through wire (sink -> call_soon_threadsafe bridge) and ``_job_queue``.
    """

    def __init__(
        self,
        state: RuntimeState,
        run_turn: Callable[[str, Wire], None],
        make_slash_context: Callable[[], SlashContext],
        initial_output: str = "",
        initial_transcript: list[OutputChunk] | None = None,
        on_transcript: Callable[[OutputChunk], None] | None = None,
        drain_pending: Callable[[], list[str]] | None = None,
        slash_registry: SlashRegistry | None = None,
    ) -> None:
        self._state = state
        self._run_turn = run_turn
        self._make_slash_context = make_slash_context
        self._on_transcript = on_transcript
        self._drain_pending = drain_pending
        self._slash_registry = slash_registry

        self._job_queue: queue.Queue[str] = queue.Queue()
        self._busy = threading.Event()
        self._turn_wire: Wire | None = None
        # Finished tool calls not yet committed. They fold into ONE summary line so a
        # long tool chain cannot push the conversation out of the scrollback; the burst
        # ends when prose resumes, another finished event arrives, or the turn ends.
        self._tool_fold: list[tuple[str, str]] = []
        # Scrollback text for the answer to the prompt currently pending (a question
        # line renders where it was asked; its answer lands right under it).
        self._pending_answer_chunk: OutputChunk | None = None

        # Event queue consumed by the UI coroutine (the worker posts via call_soon_threadsafe).
        self._event_queue: asyncio.Queue[WireEvent] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: PromptSession[str] | None = None
        self._live = LiveArea()
        self._live_dirty = asyncio.Event()
        # Pending prompt (confirm/choice) state machine: the UI thread writes, the worker waits.
        self._pending_prompt: PendingPrompt | None = None
        # Initial transcript (resume replay): committed to scrollback right after startup.
        self._initial_chunks: list[OutputChunk] = list(initial_transcript or [])
        if initial_output:
            self._initial_chunks.insert(0, OutputChunk(initial_output))

    # ---- worker thread side: turn execution and cron --------------------------

    def _work_loop(self) -> None:
        while True:
            if self._drain_pending is None:
                text = self._job_queue.get()
            else:
                try:
                    text = self._job_queue.get(timeout=1.0)
                except queue.Empty:
                    text = None

            if text == "__EXIT__":
                break

            if text is not None:
                self._state.abort_event.clear()
                self._busy.set()
                try:
                    wire = Wire()
                    self._turn_wire = wire
                    wire.sink = self._worker_sink
                    self._worker_sink([TurnStarted()])
                    self._run_turn(text, wire)
                except Exception as e:  # never let a provider/tool error kill the worker silently
                    # Include the traceback: str(e) alone cannot tell which path raised
                    # (encoding errors especially -- the message shows no stack).
                    detail = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                    self._worker_sink([Line(f"[error] {e}\n{detail}", markup=True)])
                finally:
                    self._worker_sink([TurnEnded()])
                    self._turn_wire = None
                    self._busy.clear()

            while not self._state.input_queue.empty():
                self._job_queue.put(self._state.input_queue.get())

            # Cron-due prompt: goes through slash dispatch like user input, or runs as a new turn.
            # The worker idles on get(timeout=1.0), so cron does not depend on user input to fire.
            if self._drain_pending is not None:
                for prompt in self._drain_pending():
                    self._handle_cron_prompt(prompt)

    def _worker_sink(self, batch: list[WireEvent]) -> None:
        """The worker thread's wire sink: posts event batches to the UI event loop.

        Synchronous sink mode: run_agent flushes one batch here on every send.
        """
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._enqueue_events, batch)

    def _enqueue_events(self, batch: list[WireEvent]) -> None:
        for event in batch:
            self._event_queue.put_nowait(event)
        self._live_dirty.set()

    def _handle_cron_prompt(self, text: str) -> None:
        """Handle a cron-due prompt on the worker thread: slash commands dispatch in place,
        anything else becomes a new turn."""
        text = text.strip()
        if not text or text == "/exit":
            return  # a cron-due prompt must not trigger application exit
        if text.startswith("/"):
            result = dispatch_slash(text, self._make_slash_context())
            if result.handled and result.message:
                self._worker_sink([Line(str(result.message), markup=result.markup)])
            if result.should_query:
                self._worker_sink([PromptEcho(result.prompt)])
                self._job_queue.put(result.prompt)
            return
        self._worker_sink([PromptEcho(text)])
        self._job_queue.put(text)

    # ---- UI coroutine: event consumption + Live rendering + scrollback commits -----

    async def _consume_events(self) -> None:
        """UI event-consuming coroutine: commit finished events to scrollback, update the
        Live area for streaming events.

        Ordering rule: scrollback must read in the order things actually happened. Open
        streamed prose is therefore flushed to scrollback *before* any other event is
        handled -- otherwise the prose (which the Live area holds until a blank line
        closes it) would land after the tool lines that followed it. Tool results are
        folded into one line per burst rather than one line per call.
        """
        while True:
            await self._live_dirty.wait()
            self._live_dirty.clear()
            # Drain in one go: finished events merge into a single commit, reducing
            # scrollback redraws.
            batch: list[WireEvent] = []
            while not self._event_queue.empty():
                batch.append(self._event_queue.get_nowait())
            if not batch:
                continue

            commit = self._handle_batch(batch)
            if commit:
                self._commit_chunks(commit)
            self._invalidate()

    def _handle_batch(self, batch: list[WireEvent]) -> list[OutputChunk]:
        """Fold one drained batch into the chunks to commit (in scrollback order).

        Pure state transition over the Live area and the tool-burst buffer: no terminal
        IO, so the ordering rules are testable without an event loop.
        """
        commit: list[OutputChunk] = []
        for event in batch:
            if isinstance(event, TextDelta):
                # The model is speaking again: the tool burst it preceded is finished
                # and must land before this new prose.
                self._flush_tool_fold(commit)
                # Incremental commit: a closed block goes to scrollback at once (as
                # markdown: headings/tables/code blocks get real styling) and only
                # the tail block stays in the Live area.
                for paragraph in self._live.feed_stream(event.text):
                    commit.append(OutputChunk(paragraph, format="markdown"))
                continue
            if isinstance(event, ToolCallStarted):
                # Prose written before the call belongs above the tool line.
                self._flush_prose(commit)
                self._live.active_tool = event.name
                continue
            if isinstance(event, ToolCallCompleted):
                self._live.active_tool = None
                self._tool_fold.append((event.name, tool_outcome(event)))
                continue
            if isinstance(event, TurnStarted):
                # A new turn starts clean: any leftover paragraph from the previous one
                # is closed out (fence state included) before the Live area resets.
                self._flush_prose(commit, close_fence=True)
                self._live.turn_active = True
                self._live.turn_started_at = time.monotonic()
                self._live.reset_stream()
                self._live.active_tool = None
                continue
            if isinstance(event, TurnEnded):
                # End of turn: no trailing blank line (the next prompt echo supplies
                # its own gap), and the fence state is discarded with the turn.
                self._flush_prose(commit, trailing_blank=False, close_fence=True)
                self._flush_tool_fold(commit)
                self._live.turn_active = False
                self._live.active_tool = None
                self._live.pending_preview = []
                continue

            preview = preview_lines(event)
            if preview is not None:
                # An approval is pending: the preview replaces the Live content but
                # nothing is committed yet.
                self._flush_prose(commit)
                self._live.pending_preview = preview
                continue
            # Any other finished fact (notification / hook line): prose first, then
            # the tool summary, then the line itself.
            self._flush_prose(commit)
            self._flush_tool_fold(commit)
            commit.extend(event_to_chunks(event))
        return commit

    def _flush_prose(
        self,
        commit: list[OutputChunk],
        *,
        trailing_blank: bool = True,
        close_fence: bool = False,
    ) -> None:
        """Move the open streamed paragraph into ``commit`` so it keeps its place in the
        scrollback order.

        Mid-turn flushes keep the fence state (a blank line after the flush must not
        split a code block that is still open) and end with one blank line, separating
        the prose from whatever follows.
        """
        tail = self._live.flush_paragraph(close_fence=close_fence)
        if not tail.strip():
            return
        if trailing_blank:
            commit.append(OutputChunk(tail.rstrip("\n") + "\n\n", format="markdown"))
        else:
            commit.append(OutputChunk(tail, format="markdown"))

    def _flush_tool_fold(self, commit: list[OutputChunk]) -> None:
        """Close the current tool burst: emit its single summary line."""
        if not self._tool_fold:
            return
        chunk = tool_fold_chunk(self._tool_fold)
        self._tool_fold = []
        if chunk is not None:
            commit.append(chunk)

    def _invalidate(self) -> None:
        app = get_app_or_none()
        if app is not None:
            app.invalidate()

    async def _animate_spinner(self) -> None:
        """Periodically refresh the spinner and its timer while a turn is active."""
        while True:
            await asyncio.sleep(0.12)
            if self._live.turn_active:
                self._live.spinner_frame += 1
                self._invalidate()

    # ---- prompts (asker path) -------------------------------------------------

    async def _ask_in_app(self, request: PromptRequest) -> Any:
        """Ask a question from inside the UI coroutine: both the question and the input
        prompt hang in the Live area while the worker waits for the answer.

        Nothing is written to stdout: the StdoutProxy's run_in_terminal detaches input and
        races the running prompt_async over the input stream (observed to swallow
        subsequent input so the prompt never returns). Keeping the question body in the
        Live area (prompt message) is equally visible and races with nothing.

        ``_pending_answer_chunk`` collects what the user's answer should leave in the
        scrollback (if anything): the caller commits it once the prompt is settled, so
        nothing can land between the question and its answer.
        """
        future: asyncio.Future[Any] = self._loop.create_future()  # type: ignore[union-attr]
        self._pending_answer_chunk = None
        self._pending_prompt = {"request": request, "future": future}
        self._live.pending_preview = render_prompt_request(request).rstrip().splitlines()
        # Hide the spinner while waiting for the answer: nothing is running -- the turn
        # is parked on the user. turn_started_at is kept, so the elapsed timer resumes
        # correctly after the answer. Note _render_live suppresses the body again while
        # _pending_prompt is set, so the content below shows exactly once.
        was_active = self._live.turn_active
        self._live.turn_active = False
        self._invalidate()
        try:
            return await future
        finally:
            self._live.pending_preview = []
            self._live.turn_active = was_active
            self._invalidate()
            # The answer line belongs to the question just answered: commit it now, on
            # the UI thread, so no worker event can slip in between the two.
            answer, self._pending_answer_chunk = self._pending_answer_chunk, None
            if answer is not None:
                self._commit_chunks([answer])

    def _terminal_asker(self, func: PromptFallback | PromptRequest) -> Any:
        """Asking entry point on the worker thread: structured requests go through the in-app
        asker, everything else falls back to run_in_terminal."""
        if isinstance(func, dict):
            return asyncio.run_coroutine_threadsafe(self._ask_in_app(func), self._loop).result()
        # run_in_terminal already returns a coroutine, no need to wrap it in another async fn.
        return asyncio.run_coroutine_threadsafe(run_in_terminal(func), self._loop).result()

    # ---- input submission (UI thread) ---------------------------------------

    def _answer_chunk(self, request: PromptRequest, answer: str) -> OutputChunk:
        """One scrollback line recording a settled question and its answer.

        The question itself is never a committed line of its own: it lives in the input
        box (``_prompt_message``) and disappears with the prompt (``erase_when_done``).
        Folding question and answer into one line keeps the durable record readable
        without spending two rows per confirmation.
        """
        if request.get("type") == "choice":
            question = str(request.get("question") or "Choose")
        else:
            question = str(request.get("message") or "Confirm")
        return OutputChunk(f"? {question} → {answer}\n")

    def _submit_text(self, text: str) -> None:
        """Single input entry point: feed a pending prompt state machine first, otherwise
        submit as new input."""
        text = text.strip()
        if self._pending_prompt is not None:
            entry = self._pending_prompt
            request = entry["request"]
            future: asyncio.Future[Any] = entry["future"]
            request_type = request.get("type")

            if request_type == "choice":
                labels = request.get("labels") or []
                idx = parse_choice_index(text, len(labels), int(request.get("default", 0)))
                if idx is None:
                    self._commit_now("Invalid choice. Enter a listed number.\n")
                    return
                self._pending_prompt = None
                result = str(idx)
                if not future.done():
                    future.set_result(result)
                answer = "skip" if idx == 0 else labels[idx - 1]
                self._pending_answer_chunk = self._answer_chunk(request, str(answer))
                return

            answer = parse_confirm_answer(text, bool(request.get("default", False)))
            if answer is None:
                self._commit_now("Invalid answer. Enter y or n.\n")
                return
            self._pending_prompt = None
            if not future.done():
                future.set_result(answer)
            self._pending_answer_chunk = self._answer_chunk(
                request, "yes" if answer else "no"
            )
            return

        if not text:
            return

        if text.startswith("/"):
            self._route_slash_command(text)
            return

        self._echo_user_prompt(text)
        if self._busy.is_set():
            self._state.input_queue.put(text)
            self._commit_now("[queued] will run after the current turn\n")
        else:
            self._job_queue.put(text)

    def _route_slash_command(self, text: str) -> bool:
        """Slash dispatch: commit the result in place when handled, queue the expanded prompt
        when should_query is set. Returns whether it was handled."""
        result = dispatch_slash(text, self._make_slash_context())
        if not result.handled:
            return False
        if result.message:
            chunk = OutputChunk(
                str(result.message) + "\n", format="rich" if result.markup else "plain"
            )
            self._commit_chunks([chunk])
        if result.should_query:
            self._echo_user_prompt(result.prompt)
            self._job_queue.put(result.prompt)
        if result.should_exit:
            raise EOFError
        return True

    def _echo_user_prompt(self, text: str) -> None:
        self._commit_chunks([render_user_prompt(text)])

    def _commit_now(self, text: str) -> None:
        """(UI thread) Commit one plain-text line to scrollback."""
        self._commit_chunks([OutputChunk(text)])

    def _commit_chunks(self, chunks: list[OutputChunk]) -> None:
        """(UI thread) Commit chunks: scrollback + the session transcript sidecar."""
        for chunk in chunks:
            if self._on_transcript is not None:
                self._on_transcript(chunk)
            commit_chunk(chunk)

    # ---- main entry ----------------------------------------------------------

    async def _run_async(self) -> None:
        app_session = get_app_session()
        output = app_session.output
        style = build_style()
        self._loop = asyncio.get_running_loop()

        # Resume replay / header: written straight to the terminal before the prompt starts
        # (no app yet, direct write).
        for chunk in self._initial_chunks:
            commit_chunk(chunk)

        session = PromptSession[str](
            # Only the input prompt lives in `message`: pt renders it INSIDE the frame,
            # and the frame is there to delimit the input line alone. While a question
            # is pending the message becomes the question line itself (``? ... [y/N]``)
            # so the cursor sits right after the question; otherwise it is plain ``> ``.
            # The Live area (spinner / streaming tail / question *body*) gets its own
            # window above the frame -- see install_live_area_window.
            message=self._prompt_message,
            bottom_toolbar=lambda: FormattedText(
                [("class:bottom-toolbar", bottom_toolbar(self._state))]
            ),
            completer=SlashCompleter(self._slash_registry),
            complete_while_typing=True,
            # Multiline layout: the buffer can contain newlines, and the input frame
            # grows with them. Enter submits; Shift+Enter, Alt+Enter/Esc+Enter and
            # Ctrl+J insert newlines. Shift+Enter is covered by the ANSI_SEQUENCES
            # patch above.
            multiline=True,
            style=style,
            key_bindings=build_key_bindings(self._state, self._busy),
            # Soft-wrapped input lines align under the text, past the '> ' prefix
            # (the default is no indentation in single-line mode).
            prompt_continuation=lambda width, line_number, wrap_count: " " * width,
            # Default is 8: reserving 8 rows for the completion menu leaves a large
            # blank area under a single-line input, especially ugly once the frame is
            # drawn. The layout grows temporarily when the menu appears, so no
            # reservation is needed.
            reserve_space_for_menu=0,
            # Erase the prompt UI when a submission completes. With the default
            # (False) the renderer keeps the finished layout on screen -- the typed
            # line -- and the styled echo committed right after would show it a second
            # time. Erasing first leaves the echo as the only copy.
            erase_when_done=True,
            # Explicitly bind the current AppSession's input/output: without this,
            # PromptSession builds its own Win32 console input from default widgets
            # and pipes/test doubles stop working.
            input=app_session.input,
            output=output,
        )
        self._session = session
        # Escape is both "abort" and the prefix of Esc+Enter. Keep the prefix timeout
        # short so a plain Escape still aborts promptly.
        session.app.timeoutlen = 0.2
        collapse_input_height(session)
        strip_frame_side_borders(session)
        relocate_completion_menus(session)
        self._install_live_window(session)

        worker = threading.Thread(target=self._work_loop, daemon=True)
        worker.start()
        self._state.asker = self._terminal_asker

        consume_task = asyncio.create_task(self._consume_events())
        spinner_task = asyncio.create_task(self._animate_spinner())

        try:
            # patch_stdout: a backstop for any print that bypasses wire (a leaking tool/hook).
            # raw=True is required: the proxy's default write() replaces every ESC with
            # "?", which would turn our ANSI-styled scrollback chunks into "?[...m" text.
            with patch_stdout(raw=True):
                while True:
                    try:
                        # show_frame must be passed HERE, not to the constructor:
                        # prompt_async(show_frame=False) is the default parameter value
                        # (not None), so every call overwrites the session attribute with
                        # False. Passing True each round keeps the frame on.
                        text = await session.prompt_async(show_frame=True)
                        self._submit_text(text)
                    except (EOFError, KeyboardInterrupt):
                        break
        finally:
            consume_task.cancel()
            spinner_task.cancel()
            self._state.asker = None

    # ---- Live area rendering (own window, above the input frame) --------------

    def _prompt_message(self) -> FormattedText:
        """Input-box prompt: the pending question when asking, ``> `` otherwise.

        Putting the question IN the message puts the cursor right after it -- the
        question and the answer line are one visual unit, like Claude Code.
        """
        if self._pending_prompt is not None:
            request: PromptRequest = self._pending_prompt["request"]
            return FormattedText([("class:prompt", prompt_for_request(request))])
        return FormattedText([("class:prompt", "> ")])

    def _install_live_window(self, session: PromptSession[str]) -> None:
        """Put the Live area in its own window right above the input frame."""
        install_live_area_window(session, self._render_live)

    def _render_live(self) -> FormattedText:
        """Render the Live area: spinner / pending question body / streaming tail.

        Rendered as its own layout window above the input frame, NOT as the prompt
        ``message`` -- with message-based rendering all of this would appear inside
        the frame, next to the input line. The Live window is directly below the
        scrollback tail, so the spinner sits immediately under the submitted prompt.

        While a question is pending, the one-line prompt (``? ... [y/N]``) moves into
        the input box (see ``_prompt_message``) and only the question *body* --
        option list, diff, command -- renders here, so the question appears exactly
        once and the cursor sits right after it.
        """
        columns = 80
        app = get_app_or_none()
        if app is not None:
            columns = app.output.get_size().columns
        fragments: list[tuple[str, str]] = []
        # While a question is pending, its content (options / diff / command / plan
        # panel) was parked in live.pending_preview by _ask_in_app and renders below
        # via live.formatted(); the question line itself is the prompt message. No
        # duplicate rendering here.
        fragments.extend(self._live.formatted(columns, time.monotonic()))
        # Drop the trailing newline: the window's preferred height is derived from the
        # content, and a final empty line would reserve a blank row while nothing is
        # in progress.
        while fragments and not fragments[-1][1].strip():
            fragments.pop()
        if fragments:
            fragments[-1] = (fragments[-1][0], fragments[-1][1].rstrip("\n"))
        return FormattedText(fragments)

    def run(self) -> None:
        asyncio.run(self._run_async())
        self._job_queue.put("__EXIT__")


def run_interactive_shell(
    state: RuntimeState,
    run_turn: Callable[[str, Wire], None],
    make_slash_context: Callable[[], SlashContext],
    initial_output: str = "",
    initial_transcript: list[OutputChunk] | None = None,
    on_transcript: Callable[[OutputChunk], None] | None = None,
    drain_pending: Callable[[], list[str]] | None = None,
    slash_registry: SlashRegistry | None = None,
) -> None:
    InteractiveShell(
        state,
        run_turn,
        make_slash_context,
        initial_output,
        initial_transcript,
        on_transcript,
        drain_pending,
        slash_registry,
    ).run()
