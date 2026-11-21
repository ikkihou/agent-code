# Developer entry points: `just` (list), `just check` (the one gate).
#
# Everything runs through uv so the pinned .venv and the dev group (pytest, ruff,
# pyright) are always the ones used -- no accidental system interpreter.

# List the recipes.
default:
    @just --list

# Full gate: lint + types + tests. Run this before claiming a change is done.
check: lint types test

# ruff lint (rules from [tool.ruff.lint] in pyproject.toml).
lint:
    uv run ruff check agent_code tests

# Both type checkers: pyright (stricter, pyrightconfig.json) + ty (Astral, pyproject).
types: types-pyright types-ty

# pyright (standard mode, over agent_code + tests). Extra args go to pyright:
#   just types-pyright --outputjson
#   just types-pyright agent_code/ui
types-pyright *args:
    uv run pyright {{ args }}

# ty (Astral). Extra args go to `ty check`:
#   just types-ty agent_code/ui
#   just types-ty --output-format concise
types-ty *args:
    uv run ty check {{ args }}

# Test suite. Extra args go to pytest:
#   just test -q
#   just test tests/test_hooks.py -k name
test *args:
    uv run pytest {{ args }}

# Apply ruff's autofixes and formatting (writes files).
fix:
    uv run ruff check --fix agent_code tests
    uv run ruff format agent_code tests

# Report formatting drift without writing. Not part of `check` yet: some files are
# still unformatted, so this is reported separately instead of failing the gate.
fmt-check:
    uv run ruff format --check agent_code tests

# Build sdist/wheel.
build:
    uv build
