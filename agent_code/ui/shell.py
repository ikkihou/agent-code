"""InteractiveShell: the scrollback+Live REPL itself.

See the package docstring (ui/__init__.py) for the threading and terminal-ownership
model. This module keeps only stateful orchestration: the worker thread (turn
execution / cron dispatch), the UI coroutine (event consumption / spinner / prompts),
input submission and key bindings. Pure rendering and parsing live in sibling modules.
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
from prompt_toolkit.key_binding import KeyBindings
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
from .events import event_to_chunks, render_user_prompt
from .layout import (
    _collapse_input_height,
    _install_live_area_window,
    _strip_frame_side_borders,
    bottom_toolbar,
    build_style,
)
from .live import LiveArea, _preview_lines
from .prompts import (
    parse_choice_index,
    parse_confirm_answer,
    prompt_for_request,
    render_prompt_request,
)
from .scrollback import _commit_chunk

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

        # Event queue consumed by the UI coroutine (the worker posts via call_soon_threadsafe).
        self._event_queue: asyncio.Queue[WireEvent] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: PromptSession[str] | None = None
        self._live = LiveArea()
        self._live_dirty = asyncio.Event()
        # Pending prompt (confirm/choice) state machine: the UI thread writes, the worker waits.
        self._pending_prompt: PendingPrompt | None = None
        self._exit_requested = False
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
        Live area for streaming events."""
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

            commit: list[OutputChunk] = []
            for event in batch:
                if isinstance(event, TextDelta):
                    # Incremental commit: a closed paragraph goes to scrollback at once and
                    # only the tail paragraph stays in the Live area.
                    for paragraph in self._live.feed_stream(event.text):
                        commit.append(OutputChunk(paragraph))
                    continue
                if isinstance(event, TurnStarted):
                    self._live.turn_active = True
                    self._live.turn_started_at = time.monotonic()
                    self._live.reset_stream()
                    self._live.active_tool = None
                    continue
                if isinstance(event, TurnEnded):
                    # Wrap-up: commit the whole open paragraph (usually just the last few lines).
                    tail = self._live.flush_paragraph()
                    self._live.turn_active = False
                    self._live.active_tool = None
                    self._live.pending_preview = []
                    if tail.strip():
                        commit.append(OutputChunk(tail))
                    continue
                if isinstance(event, ToolCallStarted):
                    self._live.active_tool = event.name
                    continue
                if isinstance(event, ToolCallCompleted):
                    self._live.active_tool = None
                preview = _preview_lines(event)
                if preview is not None:
                    self._live.pending_preview = preview
                    continue
                # Everything else is a "finished" fact -> scrollback.
                commit.extend(event_to_chunks(event))

            if commit:
                self._commit_chunks(commit)
            self._invalidate()

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
        """
        future: asyncio.Future[Any] = self._loop.create_future()  # type: ignore[union-attr]
        self._pending_prompt = {"request": request, "future": future}
        self._live.pending_preview = render_prompt_request(request).rstrip().splitlines()
        self._invalidate()
        try:
            return await future
        finally:
            self._live.pending_preview = []
            self._invalidate()

    def _terminal_asker(self, func: PromptFallback | PromptRequest) -> Any:
        """Asking entry point on the worker thread: structured requests go through the in-app
        asker, everything else falls back to run_in_terminal."""
        if isinstance(func, dict):
            return asyncio.run_coroutine_threadsafe(self._ask_in_app(func), self._loop).result()
        # run_in_terminal already returns a coroutine, no need to wrap it in another async fn.
        return asyncio.run_coroutine_threadsafe(run_in_terminal(func), self._loop).result()

    # ---- input submission (UI thread) ---------------------------------------

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
                self._commit_now("Choice: skip\n" if idx == 0 else f"Choice: {labels[idx - 1]}\n")
                return

            answer = parse_confirm_answer(text, bool(request.get("default", False)))
            if answer is None:
                self._commit_now("Invalid answer. Enter y or n.\n")
                return
            self._pending_prompt = None
            if not future.done():
                future.set_result(answer)
            self._commit_now(f"Answer: {'yes' if answer else 'no'}\n")
            return

        if not text:
            return

        if text == "/exit":
            self._exit_requested = True
            raise EOFError  # break out of _submit_text; the prompt loop catches it and exits

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
            _commit_chunk(chunk)

    # ---- main entry ----------------------------------------------------------

    async def _run_async(self) -> None:
        app_session = get_app_session()
        output = app_session.output
        style = build_style()
        self._loop = asyncio.get_running_loop()

        # Resume replay / header: written straight to the terminal before the prompt starts
        # (no app yet, direct write).
        for chunk in self._initial_chunks:
            _commit_chunk(chunk)

        session = PromptSession[str](
            # Only the input prompt lives in `message`: pt renders it INSIDE the frame,
            # and the frame is there to delimit the input line alone. The Live area
            # (spinner / streaming tail / pending question) gets its own window above
            # the frame -- see _install_live_area_window.
            message=lambda: FormattedText([("class:prompt", "> ")]),
            bottom_toolbar=lambda: FormattedText(
                [("class:bottom-toolbar", bottom_toolbar(self._state))]
            ),
            completer=SlashCompleter(self._slash_registry),
            complete_while_typing=True,
            style=style,
            key_bindings=self._build_key_bindings(),
            # Soft-wrapped input lines align under the text, past the '> ' prefix
            # (the default is no indentation in single-line mode).
            prompt_continuation=lambda width, line_number, wrap_count: " " * width,
            # Default is 8: reserving 8 rows for the completion menu leaves a large blank
            # area under a single-line input, especially ugly once the frame is drawn. The
            # layout grows temporarily when the menu appears, so no reservation is needed.
            reserve_space_for_menu=0,
            # Erase the prompt UI when a submission completes. With the default (False)
            # the renderer keeps the finished layout on screen -- the typed line -- and the
            # styled echo committed right after would show it a second time. Erasing first
            # leaves the echo as the only copy.
            erase_when_done=True,
            # Explicitly bind the current AppSession's input/output: without this,
            # PromptSession builds its own Win32 console input from default widgets and
            # pipes/test doubles stop working.
            input=app_session.input,
            output=output,
        )
        self._session = session
        _collapse_input_height(session)
        _strip_frame_side_borders(session)
        self._install_live_window(session)

        worker = threading.Thread(target=self._work_loop, daemon=True)
        worker.start()
        self._state.asker = self._terminal_asker

        consume_task = asyncio.create_task(self._consume_events())
        spinner_task = asyncio.create_task(self._animate_spinner())

        try:
            # patch_stdout: a backstop for any print that bypasses wire (a leaking tool/hook).
            with patch_stdout(raw=False):
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

    def _install_live_window(self, session: PromptSession[str]) -> None:
        """Put the Live area in its own window right above the input frame."""
        _install_live_area_window(session, self._render_live)

    def _render_live(self) -> FormattedText:
        """Render the Live area: spinner / pending question / streaming tail.

        Rendered as its own layout window above the input frame, NOT as the prompt
        ``message`` -- with message-based rendering all of this would appear inside
        the frame, next to the input line.
        """
        columns = 80
        app = get_app_or_none()
        if app is not None:
            columns = app.output.get_size().columns
        fragments: list[tuple[str, str]] = []
        # Pending prompt: the input prompt hangs at the top of the Live area (the question
        # body is there too, see _ask_in_app -- nothing is written to stdout, so nothing
        # races the prompt over input).
        if self._pending_prompt is not None:
            request = self._pending_prompt["request"]
            fragments.append(("class:preview", prompt_for_request(request) + "\n"))
        fragments.extend(self._live.formatted(columns, time.monotonic()))
        # Drop the trailing newline: the window's preferred height is derived from the
        # content, and a final empty line would reserve a blank row while nothing is
        # in progress.
        while fragments and not fragments[-1][1].strip():
            fragments.pop()
        if fragments:
            fragments[-1] = (fragments[-1][0], fragments[-1][1].rstrip("\n"))
        return FormattedText(fragments)

    # ---- keys ----------------------------------------------------------------

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("escape")
        def _(event: Any) -> None:
            # The provider watches this signal and actively closes the HTTP stream it is reading.
            self._state.abort_event.set()

        @kb.add("s-tab")
        def _(event: Any) -> None:
            self._state.cycle_permission_mode()
            event.app.invalidate()

        @kb.add("c-c")
        def _(event: Any) -> None:
            if self._busy.is_set():
                # Turn in flight: Ctrl+C behaves like ESC (abort the turn) and does not exit.
                self._state.abort_event.set()
            else:
                event.app.exit(exception=EOFError)

        return kb

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
