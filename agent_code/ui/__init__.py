"""UI building blocks for the interactive REPL.

A scrollback + Live REPL (modelled on kimi-cli's terminal-ownership model):

- The pt PromptSession (prompt_async) solely owns the terminal: the input line, a Live
  area just above it (spinner / streaming tail / pending preview), and the
  bottom_toolbar status bar.
- The input area has horizontal rules above and below (top/bottom only -- the Frame's
  side columns are collapsed by ``layout.strip_frame_side_borders``) and the input line
  is prefixed with ``>``. The layout follows the content: ``layout.collapse_input_height``
  pins the input window to its content height, so the input box sits right under the
  scrollback tail and scrolls with it. It is deliberately NOT pinned to the bottom
  edge -- a full-height layout leaves no scroll space above, so content committed with
  ``run_in_terminal`` would be drawn on top of the prompt instead of scrolling up.
  Completion menus float just below the frame, and a dynamic spacer window between the
  frame and the status bar (``layout._install_menu_spacer``) reserves their rows only
  while the menu is open, so the input window never grows.
- In-progress output does not live in the prompt frame: ``layout.install_live_area_window``
  puts it in its own window between the scrollback tail and the frame, so a submitted
  prompt's spinner and streamed text appear above the input box and move up into the
  scrollback as they finish. The Live window top starts with one blank separator line,
  then the spinner, so there is exactly one gap below the submitted prompt.
- Finished content reaches the native terminal scrollback through
  ``sys.stdout.write`` (inside patch_stdout) -- native scrolling/selection/copy, pt no
  longer manages a history region, and there is no cost of resetting a whole Document
  (this is the "right direction" from the output-panel performance notes, implemented).
- wire events: the worker thread sends -> call_soon_threadsafe bridge -> UI coroutine
  consumes. Finished events are committed to scrollback immediately; streaming
  TextDelta keeps only the tail lines visible in the Live area and commits the whole
  paragraph when the turn ends.

Sub-modules by responsibility:
- completer:  slash command completer + @file mention completer (TTL-cached cwd scan,
              honoring SkipPolicy so git-ignored/vendored paths never surface)
- paste:      oversized bracketed-paste placeholdering ([Pasted text #N +K lines],
              expanded back to full text at submit time; the echo shows the token)
- scrollback: commit path OutputChunk -> ANSI -> native terminal scrollback
- live:       Live area state (spinner / streaming tail / pending preview)
- events:     translation of finished wire events -> OutputChunk
- prompts:    rendering and answer parsing for prompt requests (confirm/choice)
- theme:      bottom status bar text + prompt_toolkit stylesheet
- keys:       REPL key bindings (Shift+Enter remap, abort/delete/newline, mode cycle,
              oversized-paste interception)
- layout:     input-area Frame reshaping (height collapse, side-border strip, menus, Live window)
- shell:      the InteractiveShell REPL itself (threading model + input loop)
"""
