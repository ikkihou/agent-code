"""Commit path from finished content to the native terminal scrollback.

OutputChunk is rendered to ANSI by Rich and then written with ``sys.stdout.write``
(inside patch_stdout) into the scrollback above the prompt -- native
scrolling/selection/copy, with pt no longer managing a history region.
"""

from __future__ import annotations

import sys
from io import StringIO

from prompt_toolkit.formatted_text import StyleAndTextTuples
from rich.console import Console
from rich.text import Text

from ..output import OutputChunk, chunk_fragments

_ansi_console = Console(
    file=StringIO(),
    force_terminal=True,
    color_system="truecolor",
    highlight=False,
)


def _chunk_fragments(chunk: OutputChunk) -> StyleAndTextTuples:
    """OutputChunk -> (style string, text) fragments (style strings use Rich's vocabulary)."""
    if chunk.fragments:
        return list(chunk.fragments)
    if chunk.format == "rich":
        return chunk_fragments(chunk)
    return [("", chunk.text)]


def _chunk_to_ansi(chunk: OutputChunk) -> str:
    """OutputChunk -> ANSI string.

    The TranscriptLexer fragment translation was retired along with the output panel;
    here the fragments are rebuilt into a Rich Text and rendered to ANSI -- the color
    table matches pt inline style strings (named colors and hex work on both sides).
    """
    text = Text()
    for style, fragment in _chunk_fragments(chunk):
        text.append(fragment, style=style)
    with _ansi_console.capture() as capture:
        _ansi_console.print(text, end="")
    return capture.get()


def _commit_chunk(chunk: OutputChunk) -> None:
    """Write an OutputChunk into the terminal scrollback above the prompt.

    Uses ``sys.stdout.write``: ``patch_stdout`` replaces stdout with a StdoutProxy that
    draws above the prompt through run_in_terminal on its own flush thread -- the only
    thread-safe commit path that does not fight prompt_async over input.

    Do not use pt's ``print_formatted_text``: while an app is running it goes through
    ``loop.call_soon_threadsafe(run_in_terminal(...))`` and the caller cannot wait, so in
    a loop of "commit, then immediately await the next prompt_async" it races the
    prompt's input detach/attach and all subsequent input stops working.
    """
    sys.stdout.write(_chunk_to_ansi(chunk))
    sys.stdout.flush()
