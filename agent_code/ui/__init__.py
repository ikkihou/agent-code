"""UI building blocks for the interactive REPL.

A scrollback + Live REPL (modelled on kimi-cli's terminal-ownership model):

- The pt PromptSession (prompt_async) solely owns the terminal: input line + message
  area (Live: spinner / streaming tail / pending preview) + bottom_toolbar status bar.
- The input area has a Frame (show_frame) and is pinned to the bottom:
  ``layout._install_bottom_anchored_layout`` reshapes the layout root in place -- it
  inserts a spacer at the top that absorbs the remaining height, sets
  dont_extend_height on the buffer window, and the input box therefore stays pinned to
  the bottom edge of the terminal (matching Claude Code) instead of floating after the
  scrollback tail.
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
- layout:     bottom status bar and the "pin the input area to the bottom" reshaping
- shell:      the InteractiveShell REPL itself (threading model + input loop)
"""
