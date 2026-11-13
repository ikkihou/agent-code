"""UI building blocks for the interactive REPL.

A scrollback + Live REPL (modelled on kimi-cli's terminal-ownership model):

- The pt PromptSession (prompt_async) solely owns the terminal: the input line, a Live
  area just above it (spinner / streaming tail / pending preview), and the
  bottom_toolbar status bar.
- The input area has horizontal rules above and below (top/bottom only -- the Frame's
  side columns are collapsed by ``layout._strip_frame_side_borders``) and the input line
  is prefixed with ``>``. The layout follows the content: ``layout._collapse_input_height``
  pins the input window to its content height, so the input box sits right under the
  scrollback tail and scrolls with it. It is deliberately NOT pinned to the bottom
  edge -- a full-height layout leaves no scroll space above, so content committed with
  ``run_in_terminal`` would be drawn on top of the prompt instead of scrolling up. The
  only exception is the completion menu, which needs rows inside the input window: the
  height becomes dynamic and takes ``layout.MENU_ROWS`` rows only while the menu is open.
- In-progress output does not live in the prompt frame: ``layout._install_live_area_window``
  puts it in its own window between the scrollback tail and the frame, so a submitted
  prompt's spinner and streamed text appear above the input box and move up into the
  scrollback as they finish.
- Finished content reaches the native terminal scrollback through
  ``sys.stdout.write`` (inside patch_stdout) -- native scrolling/selection/copy, pt no
  longer manages a history region, and there is no cost of resetting a whole Document
  (this is the "right direction" from the output-panel performance notes, implemented).
- wire events: the worker thread sends -> call_soon_threadsafe bridge -> UI coroutine
  consumes. Finished events are committed to scrollback immediately; streaming
  TextDelta keeps only the tail lines visible in the Live area and commits the whole
  paragraph when the turn ends.

Sub-modules by responsibility:
- completer:  slash command completer
- scrollback: commit path OutputChunk -> ANSI -> native terminal scrollback
- live:       Live area state (spinner / streaming tail / pending preview)
- events:     translation of finished wire events -> OutputChunk
- prompts:    rendering and answer parsing for prompt requests (confirm/choice)
- layout:     status bar, horizontal rules around the input, Live area window
- shell:      the InteractiveShell REPL itself (threading model + input loop)
"""
