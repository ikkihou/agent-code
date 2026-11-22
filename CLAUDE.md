# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent-code` is a from-scratch, Claude-Code-style AI coding agent CLI written in Python 3.14. It runs an agent loop against an Anthropic-compatible chat API (DeepSeek by default), with tools for file/bash/search, a three-tier permission system, JSONL session persistence, cross-session memory, shell hooks, cron jobs, and a scrollback+Live `prompt_toolkit` REPL. This repo is itself a toy clone of the harness you're running in — "the agent" here refers to the LLM agent this CLI drives, not Claude Code.

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

`run_agent` and the tool layer never print output themselves: they `wire.send()` structured events (`TextDelta` raw stream deltas, `Line` notices, `ToolCallStarted`/`ToolCallCompleted`, `DiffPreview`, `CommandPreview`), and every frontend is a pure event consumer that owns all formatting/coloring/line-buffering. `PromptEcho` and `TurnStarted`/`TurnEnded` are emitted by the REPL itself, not the loop. Frontends: the synchronous `_ConsoleWireSink` in `cli.py` (one-shot) and the scrollback+Live REPL in `ui/shell.py`. Adjacent unconsumed `TextDelta`s merge in the queue so a slow UI never accumulates a delta backlog; a `Wire` with neither `sink` nor `notify` drops events, so the loop never knows whether anyone listens. `AgentResult.trace` is maintained separately by `run_agent` and is not part of the event stream.

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

### Interactive shell (`ui/` package, `prompt_ui.py`)

The REPL is a **scrollback + Live** design implemented in `agent_code/ui/`: `shell.py` (`InteractiveShell` — threads, input loop, keys), `live.py` (`LiveArea`), `layout.py` (toolbar, frame reshaping, Live window), `events.py` (wire event → `OutputChunk`), `scrollback.py` (commit path), `banner.py` (startup logo + info box), `prompts.py` (confirm/choice rendering + parsing), `completer.py` (slash + @file mention completion), `paste.py` (oversized-paste placeholdering). The `ui/__init__.py` docstring is the design overview.

`PromptSession.prompt_async()` owns the terminal and redraws only the multiline input (a `> ` prompt inside a frame with top/bottom rules) whose height follows its line count, the Live area and the `bottom_toolbar` (permission mode + model only). The Live area is a **separate window above the input frame** (`layout._install_live_area_window`) — *not* the prompt `message`; `layout._collapse_input_height` / `_strip_frame_side_borders` reshape the frame to content height with no side columns, so it follows the content instead of filling the screen. The slash-completion menu is a Float just below the frame (`layout._relocate_completion_menus` swaps the stock `CompletionsMenu`/`MultiColumnCompletionsMenu` for a `_CompletionMenuFloat`), and `layout._install_menu_spacer` inserts a dynamic spacer between the frame and the status bar that reserves the menu's rows only while completions are open (0 otherwise). `LiveArea.formatted()` puts the spinner at the top of that window after one leading blank line — the prompt echo ends with a single `\n`, so the gap between a submitted prompt and the spinner is exactly one line. Everything finished is written to native terminal scrollback via `sys.stdout.write` inside `patch_stdout(raw=True)` — never `print_formatted_text`, which schedules an un-awaited `run_in_terminal` and corrupts prompt input, and never `raw=False`, which replaces every ESC with `?` and turns ANSI into literal `?[...m` text.

- **Input**: `PromptSession(multiline=True)`. Enter submits; Ctrl+J, Alt+Enter (usually encoded as `Esc, Enter`) and Esc+Enter insert a newline. Shift+Enter rides on `shell.py::_install_shift_enter_mapping()`, which overrides three `ANSI_SEQUENCES` entries (kitty CSI-u `\x1b[13;2u`, kitty legacy `\x1b[27;2;13u`, xterm modifyOtherKeys `\x1b[27;2;13~`) to `Keys.ControlJ`; it only fires if the terminal already sends those sequences — pt 3.0.52 has no kitty keyboard protocol support and cannot enable it (doing so would change how Esc/Ctrl+C arrive and break them). `session.app.timeoutlen = 0.2` keeps a plain Esc aborting promptly while Esc+Enter still matches.
- **@file mentions & pastes**: `completer.py::FileMentionCompleter` completes paths after an `@` word, kimi-cli-style: subsequence fuzzy matching with three rank tiers (basename prefix > basename contains > path subsequence, then earliest match, then shallowest path), a top-level-only listing for short fragments (`<3` chars, no `/` — instant first keystroke) before the full pruned walk, `@` trigger guards (an `@` after an identifier char, e.g. emails, never triggers), a short-circuit once the fragment names an existing file, and an early-pruned `os.scandir` walk (~5ms; `rglob` + post-filter is ~1200ms) honoring `SkipPolicy` so git-ignored/skip-dir paths never surface — `should_skip` also tries the path with a trailing `/` so directory rules like `build/` hide the directory entry itself. `paste.py::PasteCache` + a custom `Keys.BracketedPaste` binding replace pastes over 30 lines with `[Pasted text #N +K lines]`; `_submit_text()` expands the token for the job queue (what the model sees) while the prompt echo shows the token. The custom binding wins over pt's default verbatim insert because app-level bindings land last in pt's merged registry and `matches[-1]` wins.
- **Prompt echo**: `ui/events.py::render_user_prompt` returns `full_width=True` with `bold white on #444444` fragments. Fragment styles use Rich's inline vocabulary throughout (markdown/rich chunks get `str(Style)` verbatim from `output.py::_rich_fragment_style`), so the commit path hands them to `rich.text.Text` unchanged; `_pad_full_width()` pads each visual line to the terminal width (`get_cwidth` for CJK, tabs expanded, over-wide lines left alone); the offscreen Console uses `width=4096` so Rich does not fall back to 80 columns. Padding happens only at commit time — transcripts persist the `full_width` intent (older records are inferred from `bg:#444444`/`on #444444`). Transcripts written before the vocabulary unification carry prompt_toolkit styles; `scrollback.py::_looks_legacy_pt_style` detects them (`ansi*`/`bg:`/`fg:`/`no*` tokens) and routes them through `_legacy_pt_style`.
- **Streaming**: `LiveArea.feed_stream()` commits a paragraph as soon as a blank line closes it, keeping only the last `STREAM_TAIL_LINES` lines plus the partial line in Live; `flush_paragraph()` commits the remainder on `TurnEnded`.
- **Threads**: `run_agent` runs on a worker thread (`_work_loop`); its wire sink only does `loop.call_soon_threadsafe`, and the async `_consume_events()` coroutine renders on the UI thread. The worker also moves `state.input_queue` into the job queue after each turn and polls `drain_pending()` every second, so cron fires while idle.
- **Approvals**: `_run_async()` injects `state.asker = _terminal_asker`; `prompt_ui._ask_request()` routes structured confirm/choice requests through `run_coroutine_threadsafe(_ask_in_app)`, which parks the question in the Live area and awaits a future that `_submit_text()` resolves. Writing questions to stdout instead would detach prompt input and deadlock. One-shot mode injects no asker and falls back to `typer` prompts.
- **Preview limits**: `_consume_events()` turns `DiffPreview`/`CommandPreview` into one "waiting for approval" Live line; the full diff/command text reaches scrollback only in one-shot (`ui/events.py` can render them, but the REPL path short-circuits first).
- **Keys & slash**: Enter submits; Ctrl+J / Alt+Enter / Esc+Enter insert a newline (Shift+Enter only with an extended-sequence terminal); ESC sets `state.abort_event`, Ctrl+C aborts a running turn (exits when idle), shift+tab cycles permission mode, `/exit` leaves. Slash commands (`slash.py`: `/help`, `/model`, `/context`, `/permissions`, `/sessions`, `/loop`, `/todo`) dispatch on the UI thread in the REPL and via `cli.run_user_input()` in one-shot; a `should_query` result re-enters the loop as a new turn.

### Persistence & memory

- **Sessions** (`session.py`): append-only JSONL per turn under `.agent/sessions/<sanitized-abs-cwd>/<id>.jsonl`, plus a rendered-output sidecar `<id>.transcript.jsonl` the REPL replays into scrollback (styling + `full_width`; old records infer it from `bg:#444444` or `on #444444`). `--resume <id>` and `--continue`/`-c` both work; sidecars are excluded by name (`_is_session_file`), and `history` skips lines missing `role`/`content` instead of raising.
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

Types are part of the gate: `just types` runs **both** checkers over `agent_code` + `tests` and both must stay at zero errors — pyright (standard mode) and ty (Astral's preview checker). pyright is the stricter of the two, so a ty-clean tree is not enough; if they ever disagree, satisfy pyright's reading (and file ty's gap upstream rather than silencing it). Fix a complaint by narrowing the real type — `assert isinstance(x, Dimension)`, a local variable, `getattr` for attributes prompt_toolkit only declares on concrete containers — rather than reaching for `# type: ignore`. Two traps worth knowing:

- fragment lists are `output.StyleAndText` (`tuple[str, str]`), not prompt_toolkit's `StyleAndTextTuples`, because `list` is invariant; wrap in `FormattedText` only at the pt control boundary.
- after `assert obj.prop is None`, pyright keeps `prop` narrowed for the rest of the scope; read a property through a local variable or a helper when a later access needs its real type.
- a method named after a builtin (`ToolRegistry.list`) shadows it inside the class body, so later annotations of `list[...]` in that class resolve to the method; use a module-level alias (`ToolList`) there.
