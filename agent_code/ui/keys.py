"""REPL key bindings: Shift+Enter remapping and the input/abort/mode-cycle bindings.

Extracted from ``InteractiveShell`` so the shell class stays about orchestration
(threads, the event loop, prompts) rather than key plumbing. ``build_key_bindings`` is a
pure-ish factory: it takes the shared ``RuntimeState`` (abort flag + permission-mode
cycler) and the worker's ``busy`` Event, and returns a configured ``KeyBindings``.
"""

from __future__ import annotations

import threading
from typing import Any

from prompt_toolkit.filters import emacs_insert_mode, has_selection, vi_insert_mode
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.named_commands import get_by_name
from prompt_toolkit.keys import Keys

from ..runtime import RuntimeState

# Shift+Enter encodings used by the common extended protocols. prompt_toolkit has no
# kitty protocol support and maps the xterm modifyOtherKeys sequence to ControlM, which
# is indistinguishable from Enter. The ANSI parser reads this dict when it feeds input,
# so overriding these entries makes the sequences surface as ControlJ (the existing
# c-j binding inserts a newline). This is intentionally a module-level, idempotent
# install: every REPL in the process must see the same mapping.
_SHIFT_ENTER_SEQUENCES = (
    "\x1b[13;2u",  # kitty CSI-u
    "\x1b[27;2;13u",  # kitty legacy
    "\x1b[27;2;13~",  # xterm modifyOtherKeys
)


def install_shift_enter_mapping() -> None:
    """Map standard Shift+Enter sequences to ControlJ (newline in this app)."""
    for sequence in _SHIFT_ENTER_SEQUENCES:
        ANSI_SEQUENCES[sequence] = Keys.ControlJ


install_shift_enter_mapping()

_INSERT_MODE = vi_insert_mode | emacs_insert_mode


def _restart_completion(buffer: Any) -> None:
    """Re-run completion after a deletion.

    ``complete_while_typing`` only fires on ``insert_text``; deletions change the
    document without firing that event. ``start_completion`` schedules the same async
    completion coroutine the typing path uses, so the UI is not blocked.
    """
    if buffer.complete_while_typing():
        buffer.start_completion()


def build_key_bindings(state: RuntimeState, busy: threading.Event) -> KeyBindings:
    """Build the REPL's key bindings.

    ``state`` supplies the abort flag (ESC / Ctrl+C during a turn) and the permission-
    mode cycler (Shift+Tab); ``busy`` is the worker's flag deciding whether Ctrl+C aborts
    a running turn or exits the REPL.
    """
    kb = KeyBindings()

    @kb.add("escape")
    def _(event: Any) -> None:
        # The provider watches this signal and actively closes the HTTP stream it is
        # reading. Esc+Enter is a longer match handled below; session.app.timeoutlen
        # keeps a plain Escape from feeling delayed.
        state.abort_event.set()

    @kb.add("enter")
    def _(event: Any) -> None:
        # Ordinary Enter (ControlM) submits.
        event.current_buffer.validate_and_handle()

    # Deletions do not fire Buffer.on_text_insert, so complete_while_typing never
    # reopens the slash menu after a backspace. Bind the common deletion keys to
    # delete first, then schedule the same async completion path typing uses.
    @kb.add("backspace", filter=_INSERT_MODE & ~has_selection)
    def _(event: Any) -> None:
        get_by_name("backward-delete-char").handler(event)
        _restart_completion(event.current_buffer)

    @kb.add("delete", filter=_INSERT_MODE & ~has_selection)
    def _(event: Any) -> None:
        get_by_name("delete-char").handler(event)
        _restart_completion(event.current_buffer)

    @kb.add("c-w", filter=_INSERT_MODE & ~has_selection)
    def _(event: Any) -> None:
        get_by_name("unix-word-rubout").handler(event)
        _restart_completion(event.current_buffer)

    @kb.add("escape", "enter")
    @kb.add("c-j")
    def _(event: Any) -> None:
        # Shift+Enter sequences are remapped to ControlJ by
        # install_shift_enter_mapping; Alt+Enter (often sent as Esc, Enter) and
        # Ctrl+J are explicit fallbacks. copy_margin=False keeps newlines predictable.
        event.current_buffer.newline(copy_margin=False)

    @kb.add("s-tab")
    def _(event: Any) -> None:
        state.cycle_permission_mode()
        event.app.invalidate()

    @kb.add("c-c")
    def _(event: Any) -> None:
        if busy.is_set():
            # Turn in flight: Ctrl+C behaves like ESC (abort the turn) and does not exit.
            state.abort_event.set()
        else:
            event.app.exit(exception=EOFError)

    return kb
