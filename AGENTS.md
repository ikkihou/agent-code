# Repository Guidelines

## Project Structure & Module Organization

`agent_code/` contains the Python package and CLI implementation. `cli.py` defines the Typer interface, `agent.py` coordinates model/tool turns, `model.py` provides model backends, and `tools.py`, `fs_safety.py`, and `permissions.py` enforce tool and filesystem behavior. Session, memory, and background-process support live in focused modules such as `session.py`, `memdir/`, and `bg_manager.py`. Add automated tests under `tests/`, mirroring package names (for example, `tests/test_fs_safety.py`). Keep generated caches, virtual environments, and session history out of version control.

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
