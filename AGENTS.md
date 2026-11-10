# Repository Guidelines

## Project Structure & Module Organization

`agent_code/` contains the Python package and CLI implementation. `cli.py` defines the Typer interface, `agent.py` coordinates model/tool turns, `model.py` provides model backends, and the `tools/` package, `fs_safety.py`, and `permissions.py` enforce tool and filesystem behavior. Session, memory, and background-process support live in focused modules such as `session.py`, `memdir/`, and `bg_manager.py`. Add automated tests under `tests/`, mirroring package names (for example, `tests/test_fs_safety.py`). Keep generated caches, virtual environments, and session history out of version control.

## Build, Test, and Development Commands

- `uv sync --dev` installs Python 3.12 dependencies and the pytest development group from `uv.lock`.
- `uv run agent-code --help` verifies the installed CLI entry point.
- `uv run agent-code "your prompt"` runs a one-shot request; configure model credentials first.
- `uv run pytest` runs the complete test suite.
- `uv build` creates source and wheel distributions through Hatchling.
- `uvx ruff check agent_code tests` applies the repository's Ruff lint configuration.

## Coding Style & Naming Conventions

Use four-space indentation, complete type hints, and `from __future__ import annotations` where forward references help. Follow PEP 8 naming: `snake_case` for modules, functions, and variables; `PascalCase` for classes; and `UPPER_SNAKE_CASE` for constants. Keep modules narrowly scoped and prefer `pathlib.Path` for filesystem work. Use docstrings for public or non-obvious behavior. Do not use `print()` for diagnostics; use `logging` or the existing Rich console for intentional CLI output.

## Testing Guidelines

Tests use pytest. Name files `test_*.py` and test functions `test_<behavior>`. Cover success paths and safety boundaries, especially path traversal, permission decisions, output truncation, session persistence, and tool failures. Use temporary directories and mocks rather than real user files or network APIs. There is currently no enforced coverage percentage; new behavior should still include focused regression tests.

## Commit & Pull Request Guidelines

Recent commits use short, imperative summaries such as `Add background command execution support`; scoped Conventional Commit messages such as `fix(file_write): ...` are also acceptable. Keep each commit focused. Pull requests should explain the behavior change, list validation commands, link related issues, and include terminal output or screenshots when CLI presentation changes. Never commit API keys, `.env` files, session data, or local editor state.

## Security & Configuration

Set `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in the environment; optionally set `ANTHROPIC_BASE_URL`. Preserve the path-resolution, read-before-edit, size, and permission checks when changing tool execution code.

<!-- graft:start -->
## Graft — repo context graph

This repo is indexed in `graft/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `graft map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `graft ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `graft grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `graft skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `graft callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `graft/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `graft ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `graft build` (deterministic,
no API key, $0).
<!-- graft:end -->
