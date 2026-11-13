from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PermissionRequest:
    """A permission request for one tool call. Tools only state intent; the harness
    decides whether they run."""

    tool_name: str
    args: dict
    mode: str
    cwd: Path


@dataclass
class PermissionDecision:
    """The permission engine's decision. behavior is one of allow / ask / deny."""

    behavior: str  # "allow" | "ask" | "deny"
    message: str | None = None  # explanation shown for deny or ask


# Teaching-grade list of dangerous commands. These regexes are a backstop, not a
# complete shell sandbox.
_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"rm\s+-rf", "rm -rf is destructive"),
    (r"rm\s+-fr", "rm -fr is destructive"),
    (r"\bsudo\b", "sudo grants root access"),
    (r"chmod\s+-R", "chmod -R is recursive permission change"),
    (
        r"(curl|wget).*\|.*(sh|bash|zsh)\b",
        "downloaded script execution may execute untrusted code",
    ),
    (r"git\s+push\s+.*--force", "git push --force overwrites remote history"),
    (r"git\s+push\s+-f", "git push -f overwrites remote history"),
    (r"\bgit\s+push\b", "git push publishes local changes to a remote"),
    (r"git\s+reset\s+--hard", "git reset --hard discards uncommitted changes"),
]


def _is_dangerous(command: str) -> str | None:
    """Check a command against the dangerous patterns. None means safe; a string is
    the reason it is dangerous."""
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return reason
    return None


# Read-only tool allowlist -- allowed by default, never show an ask prompt.
_READONLY_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "glob",
        "grep",
        "project_tree",
        "git_status",
        "git_diff",
        "system_date",
        "echo",
        "memory_recall",
        "cron_list",
        "todo_read",
    }
)

# default / acceptEdits let these through; plan mode still denies them -- plan mode's
# hard constraint is read-only.
_LOW_RISK_WRITES = frozenset(
    {
        "memory_write",
        "cron_create",
        "cron_cancel",
        "todo_write",
        "enter_plan_mode",
        "exit_plan_mode",
    }
)

# Interaction and network access are not writes, but the user should still know the
# agent is pausing to ask a question or reaching out to external resources.
_ASK_TOOLS = frozenset(
    {
        "ask_user_question",
        "web_fetch",
        "web_search",
    }
)


def decide_permission(request: PermissionRequest) -> PermissionDecision:
    """Permission engine entry point: decide allow / ask / deny from the tool name,
    arguments and current mode."""
    tool_name = request.tool_name
    args = request.args
    mode = request.mode

    if tool_name in _ASK_TOOLS:
        return PermissionDecision("ask")

    # plan mode: only read-only tools are allowed; ask_user_question / web_* still go
    # through ask. Every write tool is denied.
    if mode == "plan":
        if tool_name in _READONLY_TOOLS:
            return PermissionDecision("allow")
        if tool_name in (
            "enter_plan_mode",
            "exit_plan_mode",
            "todo_write",
            "todo_read",
        ):
            return PermissionDecision("allow")
        return PermissionDecision(
            "deny",
            f"plan mode: {tool_name} is not allowed. Only read-only tools can run in plan mode.",
        )

    # Read-only tools are allowed by default in every mode.
    if tool_name in _READONLY_TOOLS:
        return PermissionDecision("allow")

    # Dangerous-command detection for the bash tool -- intercept first in every mode.
    if tool_name == "bash":
        command = args.get("command", "")
        danger_reason = _is_dangerous(command)
        if danger_reason:
            return PermissionDecision("deny", f"Dangerous command blocked: {danger_reason}")

    # acceptEdits mode: file edits skip the confirmation UI, but the safety checks in
    # agent.py still run.
    if mode == "acceptEdits":
        if tool_name in ("file_write", "file_edit"):
            return PermissionDecision("allow")

    if tool_name in _LOW_RISK_WRITES:
        return PermissionDecision("allow")

    # Default mode: file writes and bash require confirmation.
    return PermissionDecision("ask")
