"""Decoupling channel between the agent loop and the frontend (modelled on
kimi-cli's wire architecture).

run_agent and the tool layer only publish structured events through
``Wire.send()`` -- they never touch the terminal and do not know a frontend exists.
Frontends (REPL output panel, one-shot direct printing, future record/replay) are
pure event consumers: they decide how to render each event.

Events fall into two families:

- **Content events**: ``TextDelta`` (raw model streaming text; adjacent deltas merge
  in place inside the queue, so a slow consumer never builds a backlog), ``Line``
  (one complete notification line, optionally Rich markup), ``PromptEcho`` (the echo
  block for user/cron input).
- **Display events**: ``ToolCallStarted`` / ``DiffPreview`` / ``CommandPreview`` --
  the tool layer only describes "what happened"; coloring, diff styling and line
  formatting are entirely up to the frontend.

Wire consumption model: the producer (worker thread) sends; the consumer sets a
``sink`` that receives event batches. Two pacing modes:

- Synchronous consumption (one-shot): ``sink`` only, every send flushes to it
  immediately;
- Asynchronous consumption (REPL): ``sink`` plus ``notify``, send only triggers
  notify (the frontend schedules it onto the UI thread), and the UI thread hands the
  drained batch to the sink -- which is what makes the merge buffer effective.
"""

# here put the import lib
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextDelta:
    """A model streaming text increment (raw text, not line-buffered; the consumer
    assembles lines itself)."""

    text: str


@dataclass(frozen=True)
class Line:
    """One complete agent notification line (continue/final/hook/error, ...).

    markup=True means text is Rich markup for each frontend to render; trace
    (AgentResult.trace) is recorded separately on the agent side and is unrelated to
    events.
    """

    text: str
    markup: bool = False


@dataclass(frozen=True)
class PromptEcho:
    """Echo block for user input (or a cron-due prompt); the frontend renders it as a
    highlighted prompt line."""

    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """A tool call starts (the Live area shows a spinner). args_preview is the truncated
    argument preview."""

    call_id: str
    name: str
    args_preview: str


@dataclass(frozen=True)
class ToolCallCompleted:
    """A tool call finishes (the spinner settles and the frontend commits the line to
    scrollback)."""

    call_id: str
    name: str
    is_error: bool = False


@dataclass(frozen=True)
class DiffPreview:
    """Diff preview for one file write/edit (raw text; the frontend handles coloring)."""

    path: str
    old: str
    new: str


@dataclass(frozen=True)
class CommandPreview:
    """A bash command awaiting confirmation (raw text; the frontend handles rendering)."""

    command: str


@dataclass(frozen=True)
class TurnStarted:
    """A turn starts (the frontend starts the Live spinner/timer)."""


@dataclass(frozen=True)
class TurnEnded:
    """A turn ends (the frontend wraps up: commits the streamed text, settles the spinner)."""


WireEvent = (
    TextDelta
    | Line
    | PromptEcho
    | ToolCallStarted
    | ToolCallCompleted
    | DiffPreview
    | CommandPreview
    | TurnStarted
    | TurnEnded
)

# Consumer callback: receives a batch of events (guaranteed in send order).
EventSink = Callable[[list[WireEvent]], None]


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class Wire:
    """One-way event channel: the producer sends (thread-safe), the consumer receives
    batches through sink.

    Merge semantics: when send(TextDelta) finds an unconsumed TextDelta at the tail, it
    concatenates in place instead of appending a new event -- so a slow UI during
    streaming never accumulates hundreds of deltas.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[WireEvent] = []
        # sink: consumes event batches; notify: wake-up hook fired whenever an event
        # arrives (the frontend schedules its own drain on its thread). With both unset,
        # events are dropped (fire-and-forget).
        self.sink: EventSink | None = None
        self.notify: Callable[[], None] | None = None

    def send(self, event: WireEvent) -> None:
        with self._lock:
            if (
                isinstance(event, TextDelta)
                and self._pending
                and isinstance(self._pending[-1], TextDelta)
            ):
                self._pending[-1] = TextDelta(self._pending[-1].text + event.text)
            else:
                self._pending.append(event)
        notify = self.notify
        if notify is not None:
            notify()
            return
        sink = self.sink
        if sink is not None:
            # Synchronous consumption: flush right away (one-shot / test scenarios).
            batch = self.drain()
            if batch:
                sink(batch)

    def drain(self) -> list[WireEvent]:
        """Atomically take every pending event (preserving send order)."""
        with self._lock:
            batch, self._pending = self._pending, []
            return batch


# ---------------------------------------------------------------------------
# Streaming line buffer (consumer side)
# ---------------------------------------------------------------------------


class StreamLineBuffer:
    """Buffer that assembles raw TextDelta text into whole lines -- held by the
    frontend, not by the agent.

    The agent only emits raw deltas now; line boundaries are a rendering concern
    (prompt_toolkit must not overwrite half a line, one-shot wants whole lines), so
    that logic moved to the consumer.
    """

    def __init__(self, on_line: Callable[[str], None]) -> None:
        self._on_line = on_line
        self.pending: str = ""
        self.started: bool = False

    def feed(self, text: str) -> None:
        if not text:
            return

        self.started = True
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self._on_line(line.removesuffix("\r"))

    def finish(self) -> None:
        if self.pending:
            self._on_line(self.pending)
            self.pending = ""
