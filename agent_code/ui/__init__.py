"""UI layer for the interactive REPL: display/input logic and the Textual frontend.

The package holds everything that turns wire events into what the user sees. It is
split into toolkit-agnostic building blocks (pure functions and small state objects,
no terminal or Textual dependency) and the Textual frontend that renders them.

The public entry point is ``run_interactive_shell`` in ``app.py``, which ``cli.py``
calls.

Wire events flow from the worker thread into the frontend, which decides how to render
them. ``TranscriptSink`` turns each batch into ordered render items: static
``OutputChunk``\\ s (colored in ``events``), ``ThinkingItem`` blocks, and ``ToolCallItem``
blocks. Streaming ``TextDelta``\\ s keep only a tail in the ``LiveArea`` and commit whole
paragraphs; thinking and tool calls mount as collapsed collapsibles in ``BlockLog``.
In-progress output never occupies the prompt; it shows as a spinner / streaming tail /
pending preview above the input line. The fragment pipeline itself
(``OutputChunk`` -> style fragments -> padded ``rich.text.Text``) lives in
``agent_code.output``.

Toolkit-agnostic sub-modules:
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

Textual frontend:
- app:        ``AgentApp`` + ``run_interactive_shell`` (worker thread, event drain)
- widgets:    ``LivePanel`` / ``PromptInput`` / ``QuestionLine`` / ``StatusBar``
- blocks:     ``BlockLog`` + static ``TextBlock`` and foldable ``ThinkingBlock`` /
              ``ToolCallBlock`` (committed conversation)
- transcript: ``TranscriptSink`` state machine (wire events -> render items)
- render:     Rich ``Text`` rendering for the log / live panel / status bar
- completion: adapter that drives the completers for the Textual prompt
- styles/:    ``default.tcss`` loaded via ``AgentApp.CSS_PATH``
"""
