"""Textual frontend for the agent REPL (prompt_toolkit replacement).

The public entry point is ``run_interactive_shell`` in ``app.py``, which ``cli.py``
calls in place of the old ``ui.shell.run_interactive_shell``.
"""

from __future__ import annotations

from .app import run_interactive_shell

__all__ = ["run_interactive_shell"]
