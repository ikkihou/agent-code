"""Frontend-agnostic UI building blocks for the interactive REPL.

This package holds the display/input logic shared by the Textual frontend in
``agent_code.tui``: pure functions and small state objects, with no terminal or toolkit
dependency.

Wire events flow from the worker thread into the frontend, which decides how to render
them. Finished events become ``OutputChunk``\\ s (colored in ``events``), streaming
``TextDelta``\\ s keep only a tail in the ``LiveArea`` and commit whole paragraphs when
the turn ends. In-progress output never occupies the prompt; it shows as a spinner /
streaming tail / pending preview above the input line. The fragment pipeline itself
(``OutputChunk`` -> style fragments -> padded ``rich.text.Text``) lives in
``agent_code.output``.

Sub-modules by responsibility:
- completer:  slash command completer + @file mention completer (TTL-cached cwd scan,
              honoring SkipPolicy so git-ignored/vendored paths never surface). The
              public API is ``suggestions(text) -> list[Suggestion]`` -- no toolkit.
- paste:      oversized bracketed-paste placeholdering ([Pasted text #N +K lines],
              expanded back to full text at submit time; the echo shows the token)
- banner:     startup bao logo + startup info in one rounded, width-clamped box
- live:       Live area state (spinner / streaming tail / pending preview)
- events:     translation of finished wire events -> OutputChunk
- prompts:    rendering and answer parsing for prompt requests (confirm/choice)
- status:     bottom status bar text (mode · model · context usage)
"""
