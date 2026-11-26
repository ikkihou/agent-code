"""agent-code: a from-scratch, Claude-Code-style AI coding agent CLI in Python."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-code")
except PackageNotFoundError:
    __version__ = "0.1.0"
