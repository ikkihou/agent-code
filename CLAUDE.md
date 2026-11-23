# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent-code` is a from-scratch, Claude-Code-style AI coding agent CLI written in Python 3.14. It runs an agent loop against an Anthropic-compatible chat API (DeepSeek by default), with tools for file/bash/search, a three-tier permission system, JSONL session persistence, cross-session memory, shell hooks, cron jobs, and a scrollback+Live Textual REPL. This repo is itself a toy clone of the harness you're running in — "the agent" here refers to the LLM agent this CLI drives, not Claude Code.

## Commands

The dev loop is wrapped in a `justfile`. `just check` is the gate: it must be green
before a change counts as done.

```sh
just                    # list the recipes
just check              # lint + types + tests (the gate)
just lint               # ruff check agent_code tests (line-length 100, double quotes)
just types              # pyright + ty (both must pass)
just types-pyright      # pyright only (config: pyrightconfig.json, standard mode)
just types-ty           # ty only, Astral's checker (config: pyproject.toml [tool.ty])
just types-pyright agent_code/ui   # one checker over a subtree
just test               # full test suite
just test tests/test_hooks.py -k test_name   # single test
just fix                # ruff --fix + ruff format (writes files)
just fmt-check          # formatting drift only (not part of check yet)
just build              # build sdist/wheel via hatchling
```

Underlying commands, when `just` is not available:

```sh
uv sync --dev           # install deps + pytest/ruff/pyright/ty dev group (uv, Python 3.14)
uv run agent-code "hi"  # one-shot run (module entry: python -m agent_code)
uv run agent-code       # scrollback+Live interactive REPL
uv run agent-code -c    # resume latest session
uv run agent-code --provider mock "hi"   # deterministic fake model, no API key needed
uv run pytest           # full test suite
uv run ruff check agent_code tests    # lint
uv run pyright          # type check, strict-ish (agent_code + tests)
uv run ty check         # type check, Astral preview (agent_code + tests)
uv build                # build sdist/wheel via hatchling
```

Auth is read from `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` (env, or the `env` block of `~/.claude/settings.json`). The default `--provider anthropic` points at DeepSeek's Anthropic-compatible endpoint (`https://api.deepseek.com/anthropic`), overridable with `--base-url`. `--provider mock` needs no key and is used by tests.

Python is pinned to 3.14 in four places that must move together when bumping: `.python-version` (drives `uv sync` / `uv run`), `requires-python` in `pyproject.toml`, `pythonVersion` in `pyrightconfig.json`, and `[tool.ty.environment].python-version` in `pyproject.toml`.

## Architecture

The data flow is `cli.py` (entry) → `run_agent()` in `agent.py` (the loop) → `tool_execution.py` (one tool call at a time) → `ModelProvider` in `model.py` → tools/hooks/session. Supporting systems hang off the loop.

### The agent loop (`agent.py`)

`run_agent()` is the core. Each iteration: stream a completion from the provider → append the assistant message → if the response has no tool calls, finish (after Stop hooks / plan-mode approval; a `max_tokens` cut-off or an empty completion instead gets up to two "continue" retries); if it has tool calls, execute them and append a `user` message containing the `tool_result` blocks. The loop only schedules; per-call logic lives in `tool_execution.py`.

Protocol constraints that are easy to break:

- **`tool_result` blocks must come back in `tool_use` order.** `partition_tool_calls()` (in `tool_execution.py`) groups consecutive read-only tools into parallel batches run through `ThreadPoolExecutor.map`, which preserves input order — keep it that way. Write/unknown tools get their own serial batch.
- Tool-call args are truncated in the display preview only (`_format_call_args` in `tool_execution.py`, 80-char preview); the **full** args still go to the tool.
- Compaction is token-budget driven, not count driven: `ContextBudget` (`context.py`) tracks the API `usage` report plus a chars/4 estimate of appended messages, re-seeded from the assembled history at the start of every run (compaction rewrites only the loop's list while the session file stays append-only). When `should_compact()` trips (default 80% of the context window) the loop calls `compact_to_budget()` (`compact_basic.py`), which deterministically replaces the middle with a `<compacted-history>` summary block — no LLM involved — and only at pairing-safe `tool_use`/`tool_result` boundaries. `resolve_window()` resolves the window (CLI override > `CONTEXT_WINDOW_TOKENS` > provider default).

### The wire (loop ↔ frontend decoupling, `wire.py`)

`run_agent` and the tool layer never print output themselves: they `wire.send()` structured events (`TextDelta` raw stream deltas, `Line` notices, `ToolCallStarted`/`ToolCallCompleted`, `DiffPreview`, `CommandPreview`), and every frontend is a pure event consumer that owns all formatting/coloring/line-buffering. `PromptEcho` and `TurnStarted`/`TurnEnded` are emitted by the REPL itself, not the loop. Frontends: the synchronous `_ConsoleWireSink` in `cli.py` (one-shot) and the scrollback+Live REPL in `tui/` (the Textual frontend). Adjacent unconsumed `TextDelta`s merge in the queue so a slow UI never accumulates a delta backlog; a `Wire` with neither `sink` nor `notify` drops events, so the loop never knows whether anyone listens. `AgentResult.trace` is maintained separately by `run_agent` and is not part of the event stream.

### Streaming, cancellation, and rendering (`model.py`, `output.py`)

`ModelProvider` is a protocol with `complete_stream()`, which yields `ModelStreamEvent`s (`text_delta` / `completed`) and takes a `CancellationSignal`. `AnthropicProvider` runs a watcher thread that closes the HTTP stream when the signal fires; callers must re-check the signal after stream completion and map read failures to `ModelRequestAborted` when the signal is set. `MockProvider` is the test double.

`TextDelta`s arrive as raw deltas; each consumer buffers them itself. The `OutputChunk` type in `output.py` (plain text + Rich-vocabulary style fragments + a `full_width` flag) is the display unit for scrollback commits, transcript persistence, and resume replay; `full_width` asks the commit path to pad to the terminal width and is stored as intent, not baked-in padding. The REPL uses `LiveArea.feed_stream()`, which commits *closed paragraphs* (blank-line delimited) immediately and keeps only the open paragraph tail (last `STREAM_TAIL_LINES` lines) in the Live area; one-shot prints deltas straight to the console with no buffering.

### Tools (`tools/` package)

Tools live in a package: `tools/core.py` defines the `Tool` dataclass `(name, description, run fn, JSON-schema parameters, is_read_only)`, plus `ToolContext` and `ToolRegistry` (maps name → Tool). Implementation and registration metadata are co-located in per-domain sub-modules, each exposing `tools() -> list[Tool]`:
`read.py` (read_file, list_files, glob, grep, project_tree), `edit.py` (file_write, file_edit), `shell.py` (bash, git_status, git_diff), `memory.py` (memory_write, memory_recall), `todo.py` (todo_write, todo_read), `plan.py` (enter_plan_mode, exit_plan_mode), `misc.py` (echo, system_date, ask_user_question), `web.py` (web_fetch, web_search — implemented and registered by `default_tools()`; permission-wise they live in `_ASK_TOOLS`, so every web_* call asks the user first). `tools/registry.py`'s `default_tools()` aggregates the modules above plus the cron tools (from `cron_tools.py`, imported lazily inside the function to break the `cron_tools → tools` cycle). New tools go into the matching sub-module (or a new one) and usually to the permission tables in `permissions.py`.

Per-call execution lives in `tool_execution.py`, not `agent.py`: `execute_one_tool_call()` = permission decision → PreToolUse hooks → edit preflight → ask-mode confirmation → `tools.run()` → PostToolUse hooks. Two special cases:
- `ask_user_question` never reaches its registered `run`: `_handle_ask()` answers it with `prompt_single_choice` and returns that answer as the tool_result.
- `exit_plan_mode` is approved (via `approve_plan()`) before it runs, and a rejection returns an error block. `file_write`/`file_edit`/`bash` are only *previewed* and confirmed there — they then execute normally through `tools.run()`.

### Permissions & plan mode (`permissions.py`, `tool_execution.py`)

`decide_permission()` returns allow / ask / deny from `(tool, args, mode)`. Mode lives in `RuntimeState.permission_mode`, cycled by `RuntimeState.cycle_permission_mode()`: default → acceptEdits → plan (shift+tab in the REPL). Read-only tools (`_READONLY_TOOLS`) are whitelisted; `_ASK_TOOLS` (ask_user_question, web_*) always ask; dangerous bash patterns deny outright; `_LOW_RISK_WRITES` (memory/cron/todo/plan) allow outside plan mode; `acceptEdits` skips edit confirmations; **plan mode is a hard read-only constraint** — writes are denied, and `exit_plan_mode` is a *turn boundary*: when in plan mode, `execute_plan_boundary_calls()` skips every other tool in the same batch and only executes `exit_plan_mode`. Both plan-approval paths (final plan text in `agent.py`, the `exit_plan_mode` tool) go through `approve_plan()`, which flips the mode to `acceptEdits` on approval.

File-edit safety lives in `tool_execution._edit_preflight()` (helpers in `fs_safety.py`), not in the tool functions: resolve-in-cwd, missing-file check, read-before-edit (`ensure_read_before_edit`), mtime conflict (`check_mtime_conflict`), then `apply_single_replace` to build the diff shown by `confirm_edit`. `acceptEdits` still runs this preflight — it only skips the confirmation UI. The tool re-applies `apply_single_replace` at write time as a race guard.

### Interactive shell (`tui/` frontend + frontend-agnostic `ui/`)

The REPL is a **scrollback + Live** design rendered by a Textual `App` in `agent_code/tui/` (replacing the old `prompt_toolkit` shell). The frontend-agnostic display/input logic still lives in `agent_code/ui/`: `live.py` (`LiveArea`), `events.py` (wire event → `OutputChunk`), `scrollback.py` (fragment/padding pipeline + the ANSI commit path used by transcript replay), `banner.py` (startup logo + info box), `prompts.py` (confirm/choice rendering + parsing), `completer.py` (slash + @file mention completion), `paste.py` (oversized-paste placeholdering), `status.py` (bottom status bar). The `ui/__init__.py` docstring is the design overview.

`agent_code/tui/` is the Textual frontend: `app.py` (`AgentApp(App[None])` + `run_interactive_shell`), `widgets.py` (`OutputLog` / `PromptInput` / `LivePanel` / `QuestionLine` / `StatusBar`), `render.py` (OutputChunk / LiveArea / status → Rich `Text`), `transcript.py` (`TranscriptSink`), `completion.py` (`CompletionSource`). `cli.py` calls `tui.run_interactive_shell`.

`AgentApp.compose()` lays out one vertical stack, top to bottom: `OutputLog` (committed history, the former native scrollback — a `RichLog` with `markup=False`, `wrap=True`, `auto_scroll`, `max_lines=10_000`), `LivePanel` (spinner / streaming tail / pending preview), `QuestionLine` (the one-line pending question, empty when idle), an `OptionList` (completion menu, `display: none` until suggestions exist), `PromptInput` (multiline input), and `StatusBar` (mode · model · context usage). Committed chunks render through `render.chunk_to_text`, which reuses `ui/scrollback`'s fragment/padding pipeline so `full_width` prompt echoes still pad to the log's content width. `ui/scrollback.py` keeps `_chunk_to_ansi` only for transcript replay/tests, and the legacy prompt_toolkit-vocabulary shim for old transcripts.

- **Output selection & copy**: `RichLog` has no selection support upstream — it never reports content offsets to the screen and never paints the selection — so `OutputLog` overrides three methods: `render_line()` appends `Strip.apply_offsets(scroll_x, content_y)` (the metadata the compositor maps a mouse position back to a line/column with), `get_selection()` reads the already-wrapped `Strip` lines back as text (trailing padding rstripped), and `_paint_selection()` overlays the `screen--selection` component style with `Segment.apply_style(..., post_style=...)` (a base style would lose against the line's own colors) — `selection_updated()` clears RichLog's per-line cache. The theme's default selection color has identical fg/bg, so `AgentApp.CSS` sets `Screen > .screen--selection` explicitly. Ctrl+C copies a live selection before it aborts/quits (`action_abort_or_quit` → `_copy_selection`), and `copy_to_clipboard` is overridden to also pipe through `pbcopy` on macOS because Textual only emits OSC 52, which several terminals ignore.
- **IME (拼音/日文) anchoring**: Textual hides the hardware cursor (`CSI ?25l`) and paints a virtual block cursor instead, but macOS input methods anchor the candidate window to the *terminal* cursor — with it hidden, pinyin keystrokes arrive as plain letters and no candidate window appears. `PromptInput` therefore calls `app.set_terminal_cursor(True)` on focus and `False` on blur (Textual already moves the hardware cursor to the caret every frame via `App.cursor_position`, which `TextArea` maintains).
- **Input**: `PromptInput` is a `TextArea`. Enter submits (posts `Submitted`); Ctrl+J / Alt+Enter / Shift+Enter insert a newline. Ctrl+C is re-bound with `priority=True` to abort (TextArea's default is "copy"). App-level bindings: escape → abort / close completion, ctrl+c → abort a running turn (exit when idle) or copy a live selection, shift+tab → cycle permission mode.
- **Completion**: `ui/completer.py` exposes a frontend-agnostic `suggestions(text) -> list[Suggestion]` API (no toolkit). `tui/completion.py::CompletionSource` wraps `SlashCompleter` + `FileMentionCompleter` and returns `(suggestions, replace_len)`. On each input change `AgentApp.on_prompt_input_changed` fills the `OptionList`; while the menu is open, `PromptInput` delegates up/down/tab/escape to the app via its `completion_handler` (up/down move, Tab accepts, Escape closes), so the input keeps focus and typing keeps refining the menu.
- **@file mentions & pastes**: `completer.py::FileMentionCompleter` completes paths after an `@` word, kimi-cli-style: subsequence fuzzy matching with three rank tiers (basename prefix > basename contains > path subsequence, then earliest match, then shallowest path), a top-level-only listing for short fragments (`<3` chars, no `/` — instant first keystroke) before the full pruned walk, `@` trigger guards (an `@` after an identifier char, e.g. emails, never triggers), a short-circuit once the fragment names an existing file, and an early-pruned `os.scandir` walk (~5ms; `rglob` + post-filter is ~1200ms) honoring `SkipPolicy` so git-ignored/skip-dir paths never surface — `should_skip` also tries the path with a trailing `/` so directory rules like `build/` hide the directory entry itself. `paste.py::PasteCache` replaces pastes over 30 lines with `[Pasted text #N +K lines]`; `AgentApp._submit_text` expands the token for the job queue (what the model sees) while the prompt echo shows the token.
- **Prompt echo**: `ui/events.py::render_user_prompt` returns `full_width=True` with `bold white on #444444` fragments. Fragment styles use Rich's inline vocabulary throughout (markdown/rich chunks get `str(Style)` verbatim from `output.py::_rich_fragment_style`), so the commit path hands them to `rich.text.Text` unchanged; `_pad_full_width()` pads each visual line to the content width (`cell_len` for CJK, tabs expanded, over-wide lines left alone). Padding happens only at commit time — transcripts persist the `full_width` intent (older records are inferred from `bg:#444444`/`on #444444`). Transcripts written before the vocabulary unification carry prompt_toolkit styles; `scrollback.py::_looks_legacy_pt_style` detects them (`ansi*`/`bg:`/`fg:`/`no*` tokens) and routes them through `_legacy_pt_style`.
- **Streaming & ordering**: `tui/transcript.py::TranscriptSink` is the pure state machine (ported out of the old shell so the Textual app shares one tested implementation) that folds a drained wire batch into the chunks to commit, in scrollback order: streamed prose lands before the tool call that followed it, and a burst of finished tool calls folds into one summary line. It owns the `LiveArea` (`feed_stream()` commits a paragraph as soon as a blank line closes it, keeping only the last `STREAM_TAIL_LINES` lines plus the partial line in Live; `flush_paragraph()` commits the remainder on `TurnEnded`) and the `tool_fold` buffer.
- **Threads**: `run_agent` runs on a worker thread (`AgentApp._work_loop`); its wire sink (`_worker_sink`) puts batches on `_event_queue` and calls `call_from_thread(_drain_events)`, so the UI thread does all rendering. The worker also moves `state.input_queue` into the job queue after each turn and polls `drain_pending()` every second, so cron fires while idle. `set_interval(0.12, _tick)` advances the spinner.
- **Approvals**: confirm/choice requests route through `prompt_ui._ask_request` (also home to `render_diff`); when `state.asker` is set it forwards to the frontend, otherwise it calls the typer fallback. `on_mount` injects `state.asker = _terminal_asker`, which does `call_from_thread(_begin_prompt, ...)` to park the question in `QuestionLine` + Live `pending_preview`, then blocks on a `queue.Queue.get()`. `_submit_text` resolves the answer (`parse_confirm_answer` / `parse_choice_index`) and commits an `? <question> → <answer>` line. Writing questions to stdout instead would detach prompt input and deadlock. One-shot mode injects no asker and falls back to `typer` prompts.
- **Preview limits**: `TranscriptSink` turns `DiffPreview`/`CommandPreview` into a "waiting for approval" Live line (via `ui/live.py::preview_lines`); the full diff/command text reaches scrollback only in one-shot (`ui/events.py` can render them, but the REPL path short-circuits first).
- **Keys & slash**: Enter submits; Ctrl+J / Alt+Enter / Shift+Enter insert a newline; ESC sets `state.abort_event`, Ctrl+C aborts a running turn (exits when idle), shift+tab cycles permission mode, `/exit` leaves. Slash commands (`slash.py`: `/help`, `/model`, `/context`, `/permissions`, `/sessions`, `/loop`, `/todo`) dispatch on the UI thread in the REPL and via `cli.run_user_input()` in one-shot; a `should_query` result re-enters the loop as a new turn.

### Persistence & memory

- **Sessions** (`session.py`): append-only JSONL per turn under `.agent/sessions/<sanitized-abs-cwd>/<id>.jsonl`, plus a rendered-output sidecar `<id>.transcript.jsonl` the REPL replays into scrollback (styling + `full_width`; old records infer it from `bg:#444444` or `on #444444`). `--resume <id>` and `--continue`/`-c` both work; sidecars are excluded by name (`_is_session_file`), and `history` skips lines missing `role`/`content` instead of raising.
- **Long-term memory** (`memdir/`): `.agent/memory/<type>/<slug>.md` (frontmatter + body) plus a `MEMORY.md` index. Types: user/feedback/project/reference. The index is loaded into the system prompt each run.
- **System prompt** (`build_system_prompt` in `agent.py`): core instruction + `AGENT.md` (singular, via `project_memory.py`) + `MEMORY.md` index. Note the repo's own **`AGENTS.md`** (plural) is the developer guideline doc and is a different file from `AGENT.md`.
- **File history** (`file_history.py`): pre-write backups under `.agent/history/<rel>/<ts>`.

### Hooks & cron

- **Hooks** (`hooks.py`): driven by `hooks.json` in the cwd. Events: `PreToolUse` (can block a tool), `PostToolUse` (successful results only), `Stop`. Matchers: `*`, exact, or `a|b`. Each hook command gets a JSON payload on stdin. `run_hooks_raw` runs `Stop` hooks; a failing Stop hook's non-empty output forces an extra loop iteration.
- **Cron** (`scheduler.py`, `cron_tools.py`): `CronScheduler` is a background thread that pushes due prompts into a pending queue; the REPL's `tui/app.py::AgentApp._work_loop` drains it after each turn and polls every second while idle, so cron fires without user input. Jobs persist to `.agent/cron.json`. Only active in REPL mode.

## Safety invariants

- All tool paths go through `resolve_in_cwd()` (`fs_safety.py`), which confines them to the cwd subtree (traversal → `ValueError`).
- File reads: text-only (suffix whitelist or NUL-byte sniff), ≤ 256 KiB. Tool observations truncated to 8000 chars; bash output to 12000.
- `should_skip()` filters skip-dirs + `.gitignore`; git-ignored files are invisible to the agent.
- `bash_runner.py` / `bg_manager.py` run commands with a shared minimal env (`MINIMAL_ENV`, defined in `bash_runner.py`) and a fresh shell per call; `background=True` streams to `.bg/<id>.out/.err`.

## Testing & style

Tests are pytest, named `test_*.py` mirroring the package (see `tests/`). Prefer pure-function tests with `tmp_path` and mock providers over network or real user files — `MockProvider` plus an explicit `Wire` with a recording sink make `run_agent` testable without a network. Keep the `.agent/` (sessions, memory, cron, history, bg output) and `.bg/` directories out of version control. Style follows `AGENTS.md`: type hints + `from __future__ import annotations`, `pathlib.Path`, Rich console for CLI output (not `print()`), Conventional Commit messages. Comments, docstrings and user-facing strings are all in English (CJK is kept only in tests that exercise non-ASCII or GBK handling) — keep new code consistent.

Types are part of the gate: `just types` runs **both** checkers over `agent_code` + `tests` and both must stay at zero errors — pyright (standard mode) and ty (Astral's preview checker). pyright is the stricter of the two, so a ty-clean tree is not enough; if they ever disagree, satisfy pyright's reading (and file ty's gap upstream rather than silencing it). Fix a complaint by narrowing the real type — `assert isinstance(...)`, a local variable — rather than reaching for `# type: ignore`. Two traps worth knowing:

- fragment lists are `output.StyleAndText` (`tuple[str, str]`), not a bare `list[str]`, because `list` is invariant.
- after `assert obj.prop is None`, pyright keeps `prop` narrowed for the rest of the scope; read a property through a local variable or a helper when a later access needs its real type.
- a method named after a builtin (`ToolRegistry.list`) shadows it inside the class body, so later annotations of `list[...]` in that class resolve to the method; use a module-level alias (`ToolList`) there.
