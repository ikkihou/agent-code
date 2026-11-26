"""Textual frontend for the agent REPL.

The public entry point is ``run_interactive_shell`` in ``app.py``, which ``cli.py``
calls.
"""

from __future__ import annotations

from .app import run_interactive_shell

__all__ = ["run_interactive_shell"]
