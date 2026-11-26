# CLAUDE.md

Guidance for agents working in this repository.

## What this is

`agent-code` is a from-scratch, Claude-Code-style AI coding agent CLI in Python 3.14.
It drives an Anthropic-compatible chat API (DeepSeek by default) through an agent loop
with file/bash/search tools, a three-tier permission system, JSONL session persistence,
cross-session memory, hooks, cron jobs, and a Textual REPL. "The agent" here means the
LLM this CLI drives, not Claude Code.

## Commands

`just check` is the gate: ruff + pyright + ty + pytest must all pass.

```sh
just check                 # the gate
just lint                  # ruff
just types                 # pyright + ty
just test [args]           # pytest
just fix                   # ruff --fix + format
just build                 # sdist/wheel

uv run agent-code "hi"                 # one-shot
uv run agent-code                      # interactive REPL
uv run agent-code -c                   # resume latest session
uv run agent-code --provider mock "hi" # no API key needed
```

Auth comes from `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` (env, or the `env` block
of `~/.claude/settings.json`). The default `--provider anthropic` targets
`https://api.deepseek.com/anthropic`; override with `--base-url`. Python 3.14 is pinned
in `.python-version`, `pyproject.toml` (`requires-python`, `[tool.ty.environment]`), and
`pyrightconfig.json` -- bump them together.

## Architecture

Data flow: `cli.py` → `run_agent()` (`agent.py`) → `tool_execution.py` →
`ModelProvider` (`model.py`) → tools / hooks / session.

- **Agent loop** (`agent.py`): stream → append the assistant message → finish, or
  execute tool calls and append their `tool_result` blocks. A `max_tokens` cut-off or
  empty completion gets up to two "continue" retries. `tool_result` blocks must stay in
  `tool_use` order: `partition_tool_calls()` runs consecutive read-only tools in
  parallel via an order-preserving `ThreadPoolExecutor.map`; write/unknown tools run
  serially. Tool-call args are truncated only in the display preview, never for the call.
- **Compaction** (`context.py`, `compact_basic.py`): token-budget driven. `ContextBudget`
  tracks API usage plus a chars/4 estimate; at ~80% of the window `compact_to_budget()`
  deterministically replaces the middle with a `<compacted-history>` summary at safe
  `tool_use`/`tool_result` boundaries. The session file stays append-only.
- **Wire** (`wire.py`): the loop and tool layer emit structured events; frontends own all
  formatting. A `Wire` with no sink drops events. `PromptEcho` and `TurnStarted`/
  `TurnEnded` are emitted by the frontend, not the loop.
- **Tools** (`tools/`): `core.py` defines `Tool`, `ToolContext`, `ToolRegistry`; each
  domain module exposes `tools() -> list[Tool]` (`read`, `edit`, `shell`, `memory`,
  `todo`, `plan`, `misc`, `web`). `registry.default_tools()` aggregates them plus cron.
  New tools usually need an entry in the permission tables (`permissions.py`).
- **Tool execution** (`tool_execution.py`): permission decision → PreToolUse hooks → edit
  preflight → ask confirmation → `tools.run()` → PostToolUse hooks. `ask_user_question`
  is answered by the frontend; `exit_plan_mode` is approved before it runs.
- **Permissions** (`permissions.py`): `decide_permission()` returns allow / ask / deny.
  Modes cycle default → acceptEdits → plan (shift+tab in the REPL). Plan mode is a hard
  read-only constraint; `exit_plan_mode` is a turn boundary. Edit safety (cwd
  confinement, read-before-edit, mtime check, diff) lives in `_edit_preflight()` and
  `fs_safety.py`.
- **Output** (`output.py`): `OutputChunk` is plain text + Rich-vocabulary fragments +
  `full_width`. `chunk_fragments()` is the single normalization entry point;
  `pad_full_width()` pads prompt blocks at commit time. Transcripts store the intent,
  never the padding.
- **UI** (`ui/`): all interface code lives here. The Textual frontend is `AgentApp`
  (`ui/app.py`, exported as `run_interactive_shell`) with `BlockLog`, `LivePanel`,
  `QuestionLine`, `OptionList`, `PromptInput`, and `StatusBar`. `BlockLog`
  (`ui/blocks.py`) is a `VerticalScroll` of static `TextBlock`s interleaved with
  collapsed-by-default `ThinkingBlock`/`ToolCallBlock` collapsibles, so thinking and
  tool calls are individually foldable in place. The worker thread posts
  wire events through a queue and wakes the UI at most once per pending drain;
  `ui/transcript.py::TranscriptSink` folds batches into ordered render items
  (`OutputChunk` / `ThinkingItem` / `ToolCallItem`) and owns the
  `LiveArea` (commits closed paragraphs, keeps a short tail). Toolkit-agnostic helpers
  (events, live state, banner, prompts, completer, paste, status) sit alongside the
  Textual modules (`app`, `widgets`, `transcript`, `render`, `completion`) and the
  `ui/styles/default.tcss` stylesheet. Approvals route through `state.asker`; one-shot
  falls back to typer prompts.
- **Persistence** (`session.py`, `memdir/`, `file_history.py`): append-only JSONL under
  `.agent/sessions/<cwd>/<id>.jsonl`, plus a `<id>.transcript.jsonl` sidecar for replay.
  `-c` / `--resume` work; replay is draw-only and must not re-append history. Long-term
  memory is `.agent/memory/<type>/<slug>.md`, indexed by `MEMORY.md`. The system prompt
  is the core instruction + `AGENT.md` + that index. `AGENTS.md` (plural) is the
  developer guide and is a different file.
- **Hooks & cron**: `hooks.json` drives PreToolUse / PostToolUse / Stop hooks with a JSON
  payload on stdin. `CronScheduler` persists jobs to `.agent/cron.json`; the REPL drains
  due prompts while idle (REPL only).

## Safety invariants

- Tool paths are confined to the cwd by `resolve_in_cwd()`.
- Reads are text-only and ≤ 256 KiB; tool observations truncate at 8000 chars, bash
  output at 12000.
- `should_skip()` honors skip dirs and `.gitignore`.
- Bash/background commands use `MINIMAL_ENV` and a fresh shell; background output goes
  to `.bg/<id>.out|.err`.

## Testing & style

Tests are pytest, `test_*.py` mirroring the package. Prefer pure functions, `tmp_path`,
and `MockProvider` + an explicit `Wire` over network or real user files. Keep `.agent/`
and `.bg/` out of version control. Use type hints + `from __future__ import annotations`,
`pathlib.Path`, and the Rich console (not `print()`). Conventional Commit messages;
comments, docstrings and user-facing strings are English. Both pyright and ty must stay
clean -- narrow real types instead of reaching for `# type: ignore`. Fragment lists are
`output.StyleAndText` (`tuple[str, str]`), not a bare `list[str]`.
