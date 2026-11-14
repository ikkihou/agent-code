# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent-code` is a from-scratch, Claude-Code-style AI coding agent CLI written in Python 3.12. It runs an agent loop against an Anthropic-compatible chat API (DeepSeek by default), with tools for file/bash/search, a three-tier permission system, JSONL session persistence, cross-session memory, shell hooks, cron jobs, and a scrollback+Live `prompt_toolkit` REPL. This repo is itself a toy clone of the harness you're running in — "the agent" here refers to the LLM agent this CLI drives, not Claude Code.

## Commands

```sh
uv sync --dev           # install deps + pytest dev group (uv, Python 3.12)
uv run agent-code "hi"  # one-shot run (module entry: python -m agent_code)
uv run agent-code       # scrollback+Live interactive REPL
uv run agent-code -c    # resume most recent session in cwd
uv run agent-code --provider mock "hi"   # deterministic fake model, no API key needed
uv run pytest           # full test suite
uv run pytest tests/test_hooks.py -k test_name   # single test
uvx ruff check agent_code tests    # lint (line-length 100, double quotes)
uv build                # build sdist/wheel via hatchling
```

Auth is read from `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` (env, or the `env` block of `~/.claude/settings.json`). The default `--provider anthropic` points at DeepSeek's Anthropic-compatible endpoint (`https://api.deepseek.com/anthropic`), overridable with `--base-url`. `--provider mock` needs no key and is used by tests.

## Architecture

The data flow is `cli.py` (entry) → `run_agent()` in `agent.py` (the loop) → `tool_execution.py` (one tool call at a time) → `ModelProvider` in `model.py` → tools/hooks/session. Supporting systems hang off the loop.

### The agent loop (`agent.py`)

`run_agent()` is the core. Each iteration: stream a completion from the provider → append the assistant message → if the response has no tool calls, finish (after Stop hooks / plan-mode approval; a `max_tokens` cut-off or an empty completion instead gets up to two "continue" retries); if it has tool calls, execute them and append a `user` message containing the `tool_result` blocks. The loop only schedules; per-call logic lives in `tool_execution.py`.

Protocol constraints that are easy to break:

- **`tool_result` blocks must come back in `tool_use` order.** `partition_tool_calls()` (in `tool_execution.py`) groups consecutive read-only tools into parallel batches run through `ThreadPoolExecutor.map`, which preserves input order — keep it that way. Write/unknown tools get their own serial batch.
- Tool-call args are truncated in the display preview only (`_format_call_args` in `tool_execution.py`, 80-char preview); the **full** args still go to the tool.
- When `len(messages) > 40`, `compact()` (`compact_basic.py`) deterministically replaces the middle with a `<compacted-history>` summary block — no LLM involved.

### The wire (loop ↔ frontend decoupling, `wire.py`)

`run_agent` and the tool layer never print output themselves: they `wire.send()` structured events (`TextDelta` raw stream deltas, `Line` notices, `ToolCallStarted`/`ToolCallCompleted`, `DiffPreview`, `CommandPreview`), and every frontend is a pure event consumer that owns all formatting/coloring/line-buffering. `PromptEcho` and `TurnStarted`/`TurnEnded` are emitted by the REPL itself, not the loop. Frontends: the synchronous `_ConsoleWireSink` in `cli.py` (one-shot) and the scrollback+Live REPL in `ui/shell.py`. Adjacent unconsumed `TextDelta`s merge in the queue so a slow UI never accumulates a delta backlog; a `Wire` with neither `sink` nor `notify` drops events, so the loop never knows whether anyone listens. `AgentResult.trace` is maintained separately by `run_agent` and is not part of the event stream.

### Streaming, cancellation, and rendering (`model.py`, `output.py`)

`ModelProvider` is a protocol with `complete_stream()`, which yields `ModelStreamEvent`s (`text_delta` / `completed`) and takes a `CancellationSignal`. `AnthropicProvider` runs a watcher thread that closes the HTTP stream when the signal fires; callers must re-check the signal after stream completion and map read failures to `ModelRequestAborted` when the signal is set. `MockProvider` is the test double.

`TextDelta`s arrive as raw deltas; each consumer buffers them itself. The `OutputChunk` type in `output.py` (plain text + prompt_toolkit style fragments) is the display unit for scrollback commits, transcript persistence, and resume replay. The REPL does not buffer into whole lines via `StreamLineBuffer` — it uses `LiveArea.feed_stream()`, which commits *closed paragraphs* (blank-line delimited) immediately and keeps only the open paragraph tail (last `STREAM_TAIL_LINES` lines) in the Live area. `StreamLineBuffer` still exists in `wire.py` but only tests use it; one-shot prints deltas straight to the console with no buffering.

### Tools (`tools/` package)

Tools live in a package: `tools/core.py` defines the `Tool` dataclass `(name, description, run fn, JSON-schema parameters, is_read_only)`, plus `ToolContext` and `ToolRegistry` (maps name → Tool). Implementation and registration metadata are co-located in per-domain sub-modules, each exposing `tools() -> list[Tool]`:
`read.py` (read_file, list_files, glob, grep, project_tree), `edit.py` (file_write, file_edit), `shell.py` (bash, git_status, git_diff), `memory.py` (memory_write, memory_recall), `todo.py` (todo_write, todo_read), `plan.py` (enter_plan_mode, exit_plan_mode), `misc.py` (echo, system_date, ask_user_question), `web.py` (web_fetch, web_search — implemented and referenced by `permissions.py`/`tool_execution.py`, but **not** registered by `default_tools()` yet). `tools/registry.py`'s `default_tools()` aggregates the modules above plus the cron tools (from `cron_tools.py`, imported lazily inside the function to break the `cron_tools → tools` cycle). New tools go into the matching sub-module (or a new one) and usually to the permission tables in `permissions.py`.

Per-call execution lives in `tool_execution.py`, not `agent.py`: `execute_one_tool_call()` = permission decision → PreToolUse hooks → edit preflight → ask-mode confirmation → `tools.run()` → PostToolUse hooks. Two special cases:
- `ask_user_question` never reaches its registered `run`: `_handle_ask()` answers it with `prompt_single_choice` and returns that answer as the tool_result.
- `exit_plan_mode` is approved (via `approve_plan()`) before it runs, and a rejection returns an error block. `file_write`/`file_edit`/`bash` are only *previewed* and confirmed there — they then execute normally through `tools.run()`.

### Permissions & plan mode (`permissions.py`, `tool_execution.py`)

`decide_permission()` returns allow / ask / deny from `(tool, args, mode)`. Mode lives in `RuntimeState.permission_mode`, cycled by `RuntimeState.cycle_permission_mode()`: default → acceptEdits → plan (shift+tab in the REPL). Read-only tools (`_READONLY_TOOLS`) are whitelisted; `_ASK_TOOLS` (ask_user_question, web_*) always ask; dangerous bash patterns deny outright; `_LOW_RISK_WRITES` (memory/cron/todo/plan) allow outside plan mode; `acceptEdits` skips edit confirmations; **plan mode is a hard read-only constraint** — writes are denied, and `exit_plan_mode` is a *turn boundary*: when in plan mode, `execute_plan_boundary_calls()` skips every other tool in the same batch and only executes `exit_plan_mode`. Both plan-approval paths (final plan text in `agent.py`, the `exit_plan_mode` tool) go through `approve_plan()`, which flips the mode to `acceptEdits` on approval.

File-edit safety lives in `tool_execution._edit_preflight()` (helpers in `fs_safety.py`), not in the tool functions: resolve-in-cwd, missing-file check, read-before-edit (`ensure_read_before_edit`), mtime conflict (`check_mtime_conflict`), then `apply_single_replace` to build the diff shown by `confirm_edit`. `acceptEdits` still runs this preflight — it only skips the confirmation UI. The tool re-applies `apply_single_replace` at write time as a race guard.

### Interactive shell (`ui/` package, `prompt_ui.py`)

The REPL is a **scrollback + Live** design implemented in `agent_code/ui/`: `shell.py` (`InteractiveShell` — threads, input loop, keys), `live.py` (`LiveArea`), `layout.py` (toolbar, frame reshaping, Live window), `events.py` (wire event → `OutputChunk`), `scrollback.py` (commit path), `prompts.py` (confirm/choice rendering + parsing), `completer.py` (slash completion). The `ui/__init__.py` docstring is the design overview.

`PromptSession.prompt_async()` owns the terminal and redraws only the input line (a `> ` prompt inside a frame with top/bottom rules), the Live area and the `bottom_toolbar`. The Live area is a **separate window above the input frame** (`layout._install_live_area_window`) — *not* the prompt `message`; `layout._collapse_input_height` / `_strip_frame_side_borders` reshape the frame to content height with no side columns, so it follows the content instead of filling the screen. Everything finished is written to native terminal scrollback via `sys.stdout.write` inside `patch_stdout` — never `print_formatted_text`, which schedules an un-awaited `run_in_terminal` and corrupts prompt input.

- **Streaming**: `LiveArea.feed_stream()` commits a paragraph as soon as a blank line closes it, keeping only the last `STREAM_TAIL_LINES` lines plus the partial line in Live; `flush_paragraph()` commits the remainder on `TurnEnded`.
- **Threads**: `run_agent` runs on a worker thread (`_work_loop`); its wire sink only does `loop.call_soon_threadsafe`, and the async `_consume_events()` coroutine renders on the UI thread. The worker also moves `state.input_queue` into the job queue after each turn and polls `drain_pending()` every second, so cron fires while idle.
- **Approvals**: `_run_async()` injects `state.asker = _terminal_asker`; `prompt_ui._ask_request()` routes structured confirm/choice requests through `run_coroutine_threadsafe(_ask_in_app)`, which parks the question in the Live area and awaits a future that `_submit_text()` resolves. Writing questions to stdout instead would detach prompt input and deadlock. One-shot mode injects no asker and falls back to `typer` prompts.
- **Preview limits**: `_consume_events()` turns `DiffPreview`/`CommandPreview` into one "waiting for approval" Live line; the full diff/command text reaches scrollback only in one-shot (`ui/events.py` can render them, but the REPL path short-circuits first).
- **Keys & slash**: ESC sets `state.abort_event`, Ctrl+C aborts a running turn (exits when idle), shift+tab cycles permission mode, `/exit` leaves. Slash commands (`slash.py`: `/help`, `/model`, `/context`, `/permissions`, `/sessions`, `/loop`, `/todo`) dispatch on the UI thread in the REPL and via `cli.run_user_input()` in one-shot; a `should_query` result re-enters the loop as a new turn.

### Persistence & memory

- **Sessions** (`session.py`): append-only JSONL per turn under `.agent/sessions/<sanitized-abs-cwd>/<id>.jsonl`, plus a rendered-output sidecar `<id>.transcript.jsonl` that the REPL replays into scrollback on resume (styling included). Resume with `--resume <id>` or `--continue`.
- **Long-term memory** (`memdir/`): `.agent/memory/<type>/<slug>.md` (frontmatter + body) plus a `MEMORY.md` index. Types: user/feedback/project/reference. The index is loaded into the system prompt each run.
- **System prompt** (`build_system_prompt` in `agent.py`): core instruction + `AGENT.md` (singular, via `project_memory.py`) + `MEMORY.md` index. Note the repo's own **`AGENTS.md`** (plural) is the developer guideline doc and is a different file from `AGENT.md`.
- **File history** (`file_history.py`): pre-write backups under `.agent/history/<rel>/<ts>`.

### Hooks & cron

- **Hooks** (`hooks.py`): driven by `hooks.json` in the cwd. Events: `PreToolUse` (can block a tool), `PostToolUse` (successful results only), `Stop`. Matchers: `*`, exact, or `a|b`. Each hook command gets a JSON payload on stdin. `run_hooks_raw` runs `Stop` hooks; a failing Stop hook's non-empty output forces an extra loop iteration.
- **Cron** (`scheduler.py`, `cron_tools.py`): `CronScheduler` is a background thread that pushes due prompts into a pending queue; the REPL's `ui/shell.py::_work_loop` drains it after each turn and polls every second while idle, so cron fires without user input. Jobs persist to `.agent/cron.json`. Only active in REPL mode.

## Safety invariants

- All tool paths go through `resolve_in_cwd()` (`fs_safety.py`), which confines them to the cwd subtree (traversal → `ValueError`).
- File reads: text-only (suffix whitelist or NUL-byte sniff), ≤ 256 KiB. Tool observations truncated to 8000 chars; bash output to 12000.
- `should_skip()` filters skip-dirs + `.gitignore`; git-ignored files are invisible to the agent.
- `bash_runner.py` / `bg_manager.py` run commands with a shared minimal env (`MINIMAL_ENV`, defined in `bash_runner.py`) and a fresh shell per call; `background=True` streams to `.bg/<id>.out/.err`.

## Testing & style

Tests are pytest, named `test_*.py` mirroring the package (see `tests/`). Prefer pure-function tests with `tmp_path` and mock providers over network or real user files — `MockProvider` plus an explicit `Wire` with a recording sink make `run_agent` testable without a network. Keep the `.agent/` (sessions, memory, cron, history, bg output) and `.bg/` directories out of version control. Style follows `AGENTS.md`: type hints + `from __future__ import annotations`, `pathlib.Path`, Rich console for CLI output (not `print()`), Conventional Commit messages. Comments, docstrings and user-facing strings are all in English (CJK is kept only in tests that exercise non-ASCII or GBK handling) — keep new code consistent.
