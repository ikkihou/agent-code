"""Frontend-agnostic UI building blocks for the interactive REPL.

This package holds the display/input logic shared by whatever frontend renders it --
pure functions and small state objects, with no terminal or toolkit dependency. The
two frontends are:

- ``agent_code.tui`` -- the current Textual ``App`` (the prompt_toolkit shell it
  replaced lived in ``ui/shell.py``).
- the native-scrollback commit path, still used to replay transcripts as ANSI.

Wire events flow from the worker thread into the frontend, which decides how to render
them. Finished events become ``OutputChunk``\\ s (colored in ``events``), streaming
``TextDelta``\\ s keep only a tail in the ``LiveArea`` and commit whole paragraphs when
the turn ends. In-progress output never occupies the prompt; it shows as a spinner /
streaming tail / pending preview above the input line.

Sub-modules by responsibility:
- completer:  slash command completer + @file mention completer (TTL-cached cwd scan,
              honoring SkipPolicy so git-ignored/vendored paths never surface). The
              public API is ``suggestions(text) -> list[Suggestion]`` -- no toolkit.
- paste:      oversized bracketed-paste placeholdering ([Pasted text #N +K lines],
              expanded back to full text at submit time; the echo shows the token)
- scrollback: fragment/padding pipeline (OutputChunk -> style fragments) shared by the
              Rich renderers, plus the ANSI commit path and the legacy prompt_toolkit
              vocabulary shim for old transcripts
- banner:     startup logo + rounded info box (width-aware logo size, per-column gradient)
- live:       Live area state (spinner / streaming tail / pending preview)
- events:     translation of finished wire events -> OutputChunk
- prompts:    rendering and answer parsing for prompt requests (confirm/choice)
- status:     bottom status bar text (mode · model · context usage)
"""
