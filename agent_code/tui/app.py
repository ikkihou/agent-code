"""The Textual frontend for the agent REPL.

Replaces ``ui/shell.py`` (prompt_toolkit) with a Textual ``App``. The threading model is
unchanged in spirit: a worker thread runs ``run_agent`` / cron handling and posts wire
events to the UI through a thread-safe queue + ``call_from_thread``; the UI thread owns
the ``LiveArea`` state machine, the output log and the input widget.

The former native scrollback becomes the ``OutputLog`` widget; the former Live window
becomes the ``LivePanel``; the pending question becomes the ``QuestionLine``; completion
is an ``OptionList`` kept directly above the input.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from ..output import OutputChunk
from ..runtime import PromptFallback, PromptRequest, RuntimeState
from ..slash import SlashContext, SlashRegistry, dispatch_slash
from ..ui.events import render_user_prompt
from ..ui.paste import PasteCache
from ..ui.prompts import (
    parse_choice_index,
    parse_confirm_answer,
    prompt_for_request,
    render_prompt_request,
)
from ..wire import Line, PromptEcho, TurnEnded, TurnStarted, Wire, WireEvent
from .completion import CompletionSource, Suggestion
from .render import render_live
from .transcript import TranscriptSink
from .widgets import LivePanel, OutputLog, PromptInput, QuestionLine, StatusBar


class AgentApp(App[None]):
    """The agent REPL: scrollback log + Live panel + multiline prompt + status bar."""

    CSS = """
    #log { border: none; padding: 0 1; }
    #live { height: auto; }
    #question { height: auto; color: #4fd6ff; }
    #completion { height: auto; max-height: 16; display: none; border: solid #556677; }
    #prompt { height: auto; min-height: 1; border: solid #556677; }
    #status { height: 1; }
    /* The dark theme's default selection color has identical fg/bg, which makes
       selected text unreadable (in the log and in the prompt alike). */
    Screen > .screen--selection { background: #264f78; color: #ffffff; }
    """

    BINDINGS = [
        Binding("escape", "escape_key", "abort / close completion", show=False),
        Binding("ctrl+c", "abort_or_quit", "abort turn / quit", show=False),
        Binding("shift+tab", "cycle_mode", "cycle permission mode", show=False),
    ]

    def __init__(
        self,
        state: RuntimeState,
        run_turn: Callable[[str, Wire], None],
        make_slash_context: Callable[[], SlashContext],
        initial_output: OutputChunk | str = "",
        initial_transcript: list[OutputChunk] | None = None,
        on_transcript: Callable[[OutputChunk], None] | None = None,
        drain_pending: Callable[[], list[str]] | None = None,
        slash_registry: SlashRegistry | None = None,
        cwd: Path | None = None,
    ) -> None:
        super().__init__()
        self._state = state
        self._run_turn = run_turn
        self._make_slash_context = make_slash_context
        self._on_transcript = on_transcript
        self._drain_pending = drain_pending
        self._slash_registry = slash_registry
        self._cwd = cwd

        self._paste_cache = PasteCache()
        self._job_queue: queue.Queue[str] = queue.Queue()
        self._busy = threading.Event()
        self._event_queue: queue.Queue[list[WireEvent]] = queue.Queue()
        self._transcript = TranscriptSink()
        self._live = self._transcript.live
        # Pending confirm/choice prompt: the worker waits on ``queue`` for the answer.
        self._pending_prompt: dict[str, Any] | None = None
        self._pending_answer_chunk: OutputChunk | None = None
        self._prompt_was_active = False

        # Completion state (UI thread only).
        self._completion: CompletionSource | None = None
        self._suggestions: list[Suggestion] = []
        self._replace_len = 0

        # Initial transcript (resume replay) + header, committed right after mount.
        self._initial_chunks: list[OutputChunk] = list(initial_transcript or [])
        if initial_output:
            self._initial_chunks.insert(
                0,
                OutputChunk(initial_output) if isinstance(initial_output, str) else initial_output,
            )

    # ---- composition --------------------------------------------------------

    def compose(self) -> Any:
        with Vertical():
            yield OutputLog(id="log")
            yield LivePanel(id="live")
            yield QuestionLine(id="question")
            yield OptionList(id="completion")
            yield PromptInput(id="prompt")
            yield StatusBar(self._state, id="status")

    # ---- mount --------------------------------------------------------------

    def on_mount(self) -> None:
        for chunk in self._initial_chunks:
            self._commit_chunk(chunk)
        self._completion = CompletionSource(self._slash_registry, self._cwd)
        self._prompt = self.query_one(PromptInput)
        self._completion_list = self.query_one(OptionList)
        self._prompt.completion_handler = self._handle_completion_key
        self._prompt.focus()
        self.set_terminal_cursor(True)

        self._state.asker = self._terminal_asker
        self._worker = threading.Thread(target=self._work_loop, daemon=True)
        self._worker.start()
        self.set_interval(0.12, self._tick)

    # ---- worker thread ------------------------------------------------------

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
                    wire.sink = self._worker_sink
                    self._worker_sink([TurnStarted()])
                    self._run_turn(text, wire)
                except Exception as e:  # never let a provider/tool error kill the worker
                    detail = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                    self._worker_sink([Line(f"[error] {e}\n{detail}", markup=True)])
                finally:
                    self._worker_sink([TurnEnded()])
                    self._busy.clear()

            while not self._state.input_queue.empty():
                self._job_queue.put(self._state.input_queue.get())

            if self._drain_pending is not None:
                for prompt in self._drain_pending():
                    self._handle_cron_prompt(prompt)

    def _worker_sink(self, batch: list[WireEvent]) -> None:
        """The worker's wire sink: post a batch to the UI thread and wake it up."""
        self._event_queue.put(batch)
        self.call_from_thread(self._drain_events)

    def _handle_cron_prompt(self, text: str) -> None:
        text = text.strip()
        if not text or text == "/exit":
            return
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

    # ---- UI thread: event drain ---------------------------------------------

    def _drain_events(self) -> None:
        batches: list[list[WireEvent]] = []
        while True:
            try:
                batches.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        if not batches:
            return
        for batch in batches:
            for chunk in self._transcript.handle_batch(batch):
                self._commit_chunk(chunk)
        self._refresh_live()
        self.query_one(StatusBar).refresh_status()

    def _tick(self) -> None:
        if self._live.turn_active:
            self._live.spinner_frame += 1
            self._refresh_live()

    # ---- commit / refresh ---------------------------------------------------

    def _commit_chunk(self, chunk: OutputChunk) -> None:
        if self._on_transcript is not None:
            self._on_transcript(chunk)
        self.query_one(OutputLog).write_chunk(chunk)

    def _commit_now(self, text: str) -> None:
        self._commit_chunk(OutputChunk(text))

    def _refresh_live(self) -> None:
        self.query_one(LivePanel).update(
            render_live(self._live, max(self.size.width, 1), time.monotonic())
        )

    def _refresh_question(self) -> None:
        question = self.query_one(QuestionLine)
        if self._pending_prompt is not None:
            request: PromptRequest = self._pending_prompt["request"]
            question.update(Text(prompt_for_request(request), style="bold #4fd6ff"))
        else:
            question.update(Text())

    # ---- asker (worker -> UI prompt) ----------------------------------------

    def _terminal_asker(self, request: PromptRequest | PromptFallback) -> Any:
        # The REPL only ever receives structured dict requests (one-shot mode injects no
        # asker and calls the fallback directly), so a non-dict fallback is unreachable.
        result_queue: queue.Queue[Any] = queue.Queue()
        self.call_from_thread(self._begin_prompt, request, result_queue)
        return result_queue.get()

    def _begin_prompt(self, request: PromptRequest, result_queue: queue.Queue[Any]) -> None:
        self._pending_prompt = {"request": request, "queue": result_queue}
        self._pending_answer_chunk = None
        self._live.pending_preview = render_prompt_request(request).rstrip().splitlines()
        self._prompt_was_active = self._live.turn_active
        self._live.turn_active = False
        self._refresh_question()
        self._refresh_live()

    def _finish_prompt(self) -> None:
        self._pending_prompt = None
        self._live.pending_preview = []
        self._live.turn_active = self._prompt_was_active
        self._refresh_question()
        self._refresh_live()
        answer, self._pending_answer_chunk = self._pending_answer_chunk, None
        if answer is not None:
            self._commit_chunk(answer)

    def _answer_chunk(self, request: PromptRequest, answer: str) -> OutputChunk:
        if request.get("type") == "choice":
            question = str(request.get("question") or "Choose")
        else:
            question = str(request.get("message") or "Confirm")
        return OutputChunk(f"? {question} → {answer}\n")

    # ---- input submission ---------------------------------------------------

    def _submit_text(self, text: str) -> None:
        text = text.strip()
        if self._pending_prompt is not None:
            entry = self._pending_prompt
            request: PromptRequest = entry["request"]
            result_queue: queue.Queue[Any] = entry["queue"]
            request_type = request.get("type")

            if request_type == "choice":
                labels = request.get("labels") or []
                idx = parse_choice_index(text, len(labels), int(request.get("default", 0)))
                if idx is None:
                    self._commit_now("Invalid choice. Enter a listed number.\n")
                    return
                result_queue.put(str(idx))
                answer = "skip" if idx == 0 else labels[idx - 1]
                self._pending_answer_chunk = self._answer_chunk(request, str(answer))
                self._finish_prompt()
                return

            answer = parse_confirm_answer(text, bool(request.get("default", False)))
            if answer is None:
                self._commit_now("Invalid answer. Enter y or n.\n")
                return
            result_queue.put(answer)
            self._pending_answer_chunk = self._answer_chunk(request, "yes" if answer else "no")
            self._finish_prompt()
            return

        if not text:
            return

        if text.startswith("/"):
            self._route_slash_command(text)
            return

        expanded = self._paste_cache.expand(text)
        self._echo_user_prompt(text)
        if self._busy.is_set():
            self._state.input_queue.put(expanded)
            self._commit_now("[queued] will run after the current turn\n")
        else:
            self._job_queue.put(expanded)

    def _route_slash_command(self, text: str) -> bool:
        result = dispatch_slash(text, self._make_slash_context())
        if not result.handled:
            return False
        if result.message:
            chunk = OutputChunk(
                str(result.message) + "\n", format="rich" if result.markup else "plain"
            )
            self._commit_chunk(chunk)
        if result.should_query:
            self._echo_user_prompt(result.prompt)
            self._job_queue.put(result.prompt)
        if result.should_exit:
            self.exit()
        return True

    def _echo_user_prompt(self, text: str) -> None:
        self._commit_chunk(render_user_prompt(text))

    # ---- completion ---------------------------------------------------------

    def on_text_area_changed(self, message: PromptInput.Changed) -> None:
        # NOTE: the handler name is dictated by the message class, not our widget:
        # PromptInput.Changed subclasses TextArea.Changed, so Textual dispatches it to
        # ``on_text_area_changed`` (verified via PromptInput.Changed.handler_name). This
        # was previously misnamed on_prompt_input_changed and the completion menu never
        # opened at all.
        self._refresh_completion()

    def _last_input_line(self) -> str:
        lines = self._prompt.text.split("\n")
        return lines[-1] if lines else ""

    def _handle_completion_key(self, key: str) -> bool:
        if not self._completion_list.display:
            return False  # no menu open: let the key fall through to normal handling
        if key == "escape":
            self._hide_completion()
            return True
        if key == "up":
            self._completion_list.action_cursor_up()
            return True
        if key == "down":
            self._completion_list.action_cursor_down()
            return True
        if key == "tab":
            self._accept_completion()
            return True
        if key == "enter":
            # Enter on an open menu accepts the suggestion instead of submitting the
            # half-typed command -- unless the input already IS the highlighted
            # suggestion ("/help" typed in full), in which case the user plainly wants
            # to submit it.
            index = self._completion_list.highlighted
            if index is not None and 0 <= index < len(self._suggestions):
                suggestion = self._suggestions[index]
                if suggestion.insert != self._last_input_line():
                    self._accept_completion()
                    return True
            return False
        return False

    def _accept_completion(self) -> None:
        index = self._completion_list.highlighted
        if index is not None and 0 <= index < len(self._suggestions):
            self._apply_completion(self._suggestions[index])
        self._hide_completion()

    def _apply_completion(self, suggestion: Suggestion) -> None:
        text = self._prompt.text
        lines = text.split("\n")
        line = lines[-1]
        lines[-1] = line[: len(line) - self._replace_len] + suggestion.insert
        self._prompt.load_text("\n".join(lines))
        self._prompt.move_cursor((len(lines) - 1, len(lines[-1])))
        # load_text posts Changed, which recomputes suggestions: the completed word
        # usually matches again, so the menu stays open for further refinement (e.g.
        # "@src/u" -> "@src/util/" keeps listing deeper paths).
        self._refresh_completion()

    def _refresh_completion(self) -> None:
        """Recompute suggestions for the current input line and redraw the menu."""
        if self._completion is None:
            self._hide_completion()
            return
        line = self._last_input_line()
        suggestions, replace_len = self._completion.suggestions(line)
        self._suggestions = suggestions
        self._replace_len = replace_len
        if not suggestions:
            self._hide_completion()
            return
        self._completion_list.clear_options()
        self._completion_list.add_options(
            [Option(s.insert, id=str(i)) for i, s in enumerate(suggestions)]
        )
        self._completion_list.highlighted = 0
        self._completion_list.display = True

    def _hide_completion(self) -> None:
        self._completion_list.display = False
        self._suggestions = []
        self._replace_len = 0

    # ---- terminal cursor / clipboard ----------------------------------------

    def set_terminal_cursor(self, visible: bool) -> None:
        """Show or hide the terminal's own (hardware) cursor.

        Textual hides it and paints a virtual block cursor instead, but macOS input
        methods anchor their composition window to the *terminal* cursor: while it is
        hidden the pinyin keystrokes are delivered as plain letters and no candidate
        window appears. The prompt therefore asks for the hardware cursor while it has
        focus; Textual already moves it to the caret on every frame.
        """
        driver = self._driver
        if driver is None or self.is_headless:
            return
        driver.write("\x1b[?25h" if visible else "\x1b[?25l")
        driver.flush()

    def copy_to_clipboard(self, text: str) -> None:
        """Copy text to the system clipboard.

        Textual only emits OSC 52, which a number of terminals ignore (macOS Terminal
        among them). On macOS we additionally pipe the text through ``pbcopy``, so
        copying selected output works regardless of OSC 52 support.
        """
        super().copy_to_clipboard(text)
        if sys.platform == "darwin" and not self.is_headless:
            threading.Thread(target=_pbcopy, args=(text,), daemon=True).start()

    def on_text_selected(self) -> None:
        """A mouse drag finished selecting text: hint that ctrl+c copies it."""
        if self.is_headless or not self.screen.get_selected_text():
            return
        self.notify("Text selected \u2014 ctrl+c copies it", timeout=2)

    # ---- key actions --------------------------------------------------------

    def action_escape_key(self) -> None:
        if self._completion_list.display:
            self._hide_completion()
            return
        self._state.abort_event.set()

    def action_abort_or_quit(self) -> None:
        if self._copy_selection():
            return
        if self._busy.is_set():
            self._state.abort_event.set()
        else:
            self.exit()

    def _copy_selection(self) -> bool:
        """Copy the current text selection, if any (ctrl+c on selected output)."""
        selection = self.screen.get_selected_text()
        if not selection:
            return False
        self.copy_to_clipboard(selection)
        self.screen.clear_selection()
        return True

    def action_cycle_mode(self) -> None:
        self._state.cycle_permission_mode()
        self.query_one(StatusBar).refresh_status()

    # ---- message handlers ---------------------------------------------------

    def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        self._submit_text(message.text)
        self._prompt.load_text("")

    def on_prompt_input_abort(self, message: PromptInput.Abort) -> None:
        self.action_abort_or_quit()


def _pbcopy(text: str) -> None:
    """Put ``text`` on the macOS clipboard (no-op if ``pbcopy`` is unavailable)."""
    try:
        subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            timeout=2,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def run_interactive_shell(
    state: RuntimeState,
    run_turn: Callable[[str, Wire], None],
    make_slash_context: Callable[[], SlashContext],
    initial_output: OutputChunk | str = "",
    initial_transcript: list[OutputChunk] | None = None,
    on_transcript: Callable[[OutputChunk], None] | None = None,
    drain_pending: Callable[[], list[str]] | None = None,
    slash_registry: SlashRegistry | None = None,
    cwd: Path | None = None,
) -> None:
    # The kitty keyboard protocol garbles IME commits: with REPORT_ALL_KEYS, an input
    # method's committed text arrives as one oversized key report that Textual's parser
    # drops past its 32-char sequence threshold (upstream issue #6721). Plain xterm
    # encoding delivers IME commits as normal characters, so Chinese/Japanese input
    # works. Set before the driver starts; an explicit user setting wins.
    os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")
    app = AgentApp(
        state,
        run_turn,
        make_slash_context,
        initial_output,
        initial_transcript,
        on_transcript,
        drain_pending,
        slash_registry,
        cwd,
    )
    try:
        # Inline (not alternate-screen) so the terminal's native scrollback above the
        # app survives while running and the last frame stays on screen after exit.
        app.run(inline=True, inline_no_clear=True)
    finally:
        state.asker = None
        app._job_queue.put("__EXIT__")
