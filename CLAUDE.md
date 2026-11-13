# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent-code` is a from-scratch, Claude-Code-style AI coding agent CLI written in Python 3.12. It runs an agent loop against an Anthropic-compatible chat API (DeepSeek by default), with tools for file/bash/search, a three-tier permission system, JSONL session persistence, cross-session memory, shell hooks, cron jobs, and a full-screen `prompt_toolkit` REPL. This repo is itself a toy clone of the harness you're running in — "the agent" here refers to the LLM agent this CLI drives, not Claude Code.

## Commands

```sh
uv sync --dev           # install deps + pytest dev group (uv, Python 3.12)
uv run agent-code "hi"  # one-shot run (module entry: python -m agent_code)
uv run agent-code       # full-screen interactive REPL
uv run agent-code -c    # resume most recent session in cwd
uv run agent-code --provider mock "hi"   # deterministic fake model, no API key needed
uv run pytest           # full test suite
uv run pytest tests/test_hooks.py -k test_name   # single test
uvx ruff check agent_code tests    # lint (line-length 100, double quotes)
uv build                # build sdist/wheel via hatchling
```

Auth is read from `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` (env, or the `env` block of `~/.claude/settings.json`). The default `--provider anthropic` points at DeepSeek's Anthropic-compatible endpoint (`https://api.deepseek.com/anthropic`), overridable with `--base-url`. `--provider mock` needs no key and is used by tests.

## Architecture

The data flow is `cli.py` (entry) → `run_agent()` in `agent.py` (the loop) → `ModelProvider` in `model.py` → tools/hooks/session. Supporting systems hang off the loop.

### The agent loop (`agent.py`)

`run_agent()` is the core. Each iteration: stream a completion from the provider → append the assistant message → if the response has no tool calls, finish (or run Stop hooks / plan-mode approval); if it has tool calls, execute them and append a `user` message containing the `tool_result` blocks.

Protocol constraints that are easy to break:

- **`tool_result` blocks must come back in `tool_use` order.** `partition_tool_calls()` groups consecutive read-only tools into parallel batches run through `ThreadPoolExecutor.map`, which preserves input order — keep it that way. Write/unknown tools get their own serial batch.
- Tool-call args are truncated in the display preview only (`_format_call_args`, 80-char preview); the **full** args still go to the tool.
- When `len(messages) > 40`, `compact()` (`compact_basic.py`) deterministically replaces the middle with a `<compacted-history>` summary block — no LLM involved.

### The wire (loop ↔ frontend decoupling, `wire.py`)

`run_agent` and the tool layer never touch the terminal: they `wire.send()` structured events (`TextDelta` raw stream deltas, `Line` notices, `PromptEcho`, `ToolCallStarted`/`ToolCallCompleted`, `DiffPreview`, `CommandPreview`, `TurnStarted`/`TurnEnded`), and every frontend (one-shot console sink in `cli.py`, scrollback+Live REPL in `interactive.py`) is a pure event consumer that owns all formatting/coloring/line-buffering. Adjacent unconsumed `TextDelta`s merge in the queue so a slow UI never accumulates a delta backlog. `AgentResult.trace` is maintained separately by `run_agent` and is not part of the event stream.

### Streaming, cancellation, and rendering (`model.py`, `output.py`)

`ModelProvider` is a protocol with `complete_stream()`, which yields `ModelStreamEvent`s (`text_delta` / `completed`) and takes a `CancellationSignal`. `AnthropicProvider` runs a watcher thread that closes the HTTP stream when the signal fires; callers must re-check the signal after stream completion and map read failures to `ModelRequestAborted` when the signal is set. `MockProvider` is the test double.

`TextDelta`s arrive as raw deltas; each consumer buffers them itself. The `OutputChunk` type in `output.py` (plain text + prompt_toolkit style fragments) is the display unit for scrollback commits, transcript persistence, and resume replay. The REPL does not buffer into whole lines via `StreamLineBuffer` — it uses `LiveArea.feed_stream()`, which commits *closed paragraphs* (blank-line delimited) immediately and keeps only the open paragraph tail in the Live area. `StreamLineBuffer` remains in `wire.py` for consumers that need line-oriented output.

### Tools (`tools/` package)

Tools live in a package: `tools/core.py` defines the `Tool` dataclass `(name, description, run fn, JSON-schema parameters, is_read_only)`, plus `ToolContext` and `ToolRegistry` (maps name → Tool). Implementation and registration metadata are co-located in per-domain sub-modules, each exposing `tools() -> list[Tool]`:
`read.py` (read_file, list_files, glob, grep), `edit.py` (file_write, file_edit), `shell.py` (bash, git_status, git_diff), `memory.py` (memory_write, memory_recall), `todo.py` (todo_write, todo_read), `plan.py` (enter_plan_mode, exit_plan_mode), `misc.py` (echo, system_date, ask_user_question). `tools/registry.py`'s `default_tools()` aggregates them plus the cron tools (from `cron_tools.py`, imported lazily inside the function to break the `cron_tools → tools` cycle). New tools go into the matching sub-module (or a new one) and usually to the permission tables in `permissions.py`.

Two tools are special-cased and never actually executed by their registered `run`:
- `ask_user_question` is intercepted in `agent.py`'s permission block, which calls `prompt_single_choice` and feeds the result back as the observation.
- `bash`/`file_write`/`file_edit`/`exit_plan_mode` do their real work only after the `agent.py` interception block passes them.

### Permissions & plan mode (`permissions.py`, `agent.py`)

`decide_permission()` returns allow / ask / deny from `(tool, args, mode)`. Mode lives in `RuntimeState.permission_mode` and cycles default → acceptEdits → plan (shift+tab in the REPL). Read-only tools are whitelisted; `_ASK_TOOLS` always ask; dangerous bash patterns deny outright; `acceptEdits` skips edit confirmations; **plan mode is a hard read-only constraint** — writes are denied, and `exit_plan_mode` is a *turn boundary*: when in plan mode, `execute_plan_boundary_calls()` skips every other tool in the same batch and only executes `exit_plan_mode`.

File-edit safety lives in `agent.py`, not in the tool functions: before an ask/allow, it runs read-before-edit (`ensure_read_before_edit`), mtime-conflict, then diff preview + `confirm_edit`. The tool re-applies `apply_single_replace` afterwards as a race guard.

### Interactive shell (`interactive.py`, `prompt_ui.py`)

The REPL is a **scrollback + Live** design: `PromptSession.prompt_async()` owns the terminal and redraws only the input line, the `message` area (Live: spinner + elapsed, active tool, pending approval preview, tail of the open paragraph) and `bottom_toolbar`. Everything already finished is written to native terminal scrollback via `sys.stdout.write` inside `patch_stdout`'s `StdoutProxy` — never `print_formatted_text`, which schedules an un-awaited `run_in_terminal` and corrupts prompt input. `run_agent` runs on a worker thread; its wire sink only does `loop.call_soon_threadsafe`, and an async `_consume_events()` coroutine does the actual rendering on the UI thread. Questions the worker needs answered (confirmations, choices) are rendered *inside the Live area* through `prompt_ui.set_terminal_asker()` → `_ask_in_app` via `run_coroutine_threadsafe`, with `_submit_text` feeding the answer into the pending future; writing them to stdout instead would detach prompt input and deadlock. One-shot mode (no asker injected) falls back to `typer` prompts. ESC sets `state.abort_event`, shift+tab cycles permission mode.

### Persistence & memory

- **Sessions** (`session.py`): append-only JSONL per turn under `.agent/sessions/<sanitized-abs-cwd>/<id>.jsonl`. Resume with `--resume <id>` or `--continue`.
- **Long-term memory** (`memdir/`): `.agent/memory/<type>/<slug>.md` (frontmatter + body) plus a `MEMORY.md` index. Types: user/feedback/project/reference. The index is loaded into the system prompt each run.
- **System prompt** (`build_system_prompt` in `agent.py`): core instruction + `AGENT.md` (singular, via `project_memory.py`) + `MEMORY.md` index. Note the repo's own **`AGENTS.md`** (plural) is the developer guideline doc and is a different file from `AGENT.md`.
- **File history** (`file_history.py`): pre-write backups under `.agent/history/<rel>/<ts>`.

### Hooks & cron

- **Hooks** (`hooks.py`): driven by `hooks.json` in the cwd. Events: `PreToolUse` (can block a tool), `PostToolUse`, `Stop`. Matchers: `*`, exact, or `a|b`. Each hook command gets a JSON payload on stdin. `run_hooks_raw` runs `Stop` hooks; a failing Stop hook's non-empty output forces an extra loop iteration.
- **Cron** (`scheduler.py`, `cron_tools.py`): `CronScheduler` is a background thread that pushes due prompts into a pending queue; the REPL's `work_loop` drains it after each turn and polls every second while idle, so cron fires without user input. Jobs persist to `.agent/cron.json`. Only active in REPL mode.

## Safety invariants

- All tool paths go through `resolve_in_cwd()`, which confines them to the cwd subtree (traversal → `ValueError`).
- File reads: text-only (suffix whitelist or NUL-byte sniff), ≤ 256 KiB. Tool observations truncated to 8000 chars; bash output to 12000.
- `should_skip()` filters skip-dirs + `.gitignore`; git-ignored files are invisible to the agent.
- `bash_runner.py` / `bg_manager.py` run commands with a shared minimal env (`MINIMAL_ENV`, defined in `bash_runner.py`) and a fresh shell per call; `background=True` streams to `.bg/<id>.out/.err`.

## Testing & style

Tests are pytest, named `test_*.py` mirroring the package (see `tests/`). Prefer pure-function tests with `tmp_path` and mock providers over network or real user files — `MockProvider` and the `output=` callback make `run_agent` testable without a network. Keep the `.agent/` (sessions, memory, cron, history, bg output) and `.bg/` directories out of version control. Style follows `AGENTS.md`: type hints + `from __future__ import annotations`, `pathlib.Path`, Rich console for CLI output (not `print()`), Conventional Commit messages. Comments, docstrings and user-facing strings are all in English (CJK is kept only in tests that exercise non-ASCII or GBK handling) — keep new code consistent.
