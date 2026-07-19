#!/usr/bin/env python3
"""
##
##       filename: cli.py
##        created: 2026/06/14
##         author: Paul_Bao
##            IDE: Neovim
##       Version : 1.0
##       Contact : paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .agent import run_agent, build_system_prompt
from .model import AnthropicProvider, create_provider
from .tools import default_tools
from .session import Session

console = Console()
app = typer.Typer(add_completion=False)


def render_header(cwd: Path, provider: str, model: str, base_url: str | None) -> None:
    console.print("[bold]Agent Code[/bold]")
    console.print(f"[dim]cwd: {cwd}[/dim]")
    console.print(f"[dim]provider: {provider}  model: {model}[/dim]")
    if base_url:
        console.print(f"[dim]base_url: {base_url}[/dim]")
    console.print()


def _session_summaries(cwd: Path) -> list[tuple[str, int, float]]:
    """返回 session_id、user/assistant 消息数和文件修改时间。"""
    from .session import _session_dir

    sessions_dir = _session_dir(cwd)
    summaries: list[tuple[str, int, float]] = []

    if not sessions_dir.is_dir():
        return summaries

    for file_path in sessions_dir.rglob("*.jsonl"):
        message_count = 0
        try:
            for line in file_path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("role") in {
                    "user",
                    "assistant",
                }:
                    message_count += 1
            modified_at = file_path.stat().st_mtime
        except (OSError, UnicodeError):
            continue

        summaries.append((file_path.stem, message_count, modified_at))

    return sorted(summaries, key=lambda item: item[2], reverse=True)


def _render_sessions(cwd: Path) -> None:
    summaries = _session_summaries(cwd)
    if not summaries:
        console.print("[dim]没有找到会话。[/dim]")
        return

    table = Table(title="Sessions")
    table.add_column("session_id")
    table.add_column("消息数", justify="right")
    table.add_column("最后更新时间")
    for session_id, message_count, modified_at in summaries:
        updated = (
            datetime.fromtimestamp(modified_at)
            .astimezone()
            .isoformat(timespec="seconds")
        )
        table.add_row(session_id, str(message_count), updated)
    console.print(table)


def handle_slash(line: str, cwd: Path | None = None) -> bool:
    # slash command 是 CLI 控制命令，不交给模型。
    if line == "/help":
        console.print("可用命令：/help, /sessions, /exit")
        return True
    if line == "/sessions":
        _render_sessions(cwd or Path.cwd())
        return True
    return False


def run_once(
    prompt: str,
    cwd: Path,
    provider_name: str,
    model: str,
    base_url: str | None,
    max_steps: int,
    permission_mode: str,
    session: Session | None = None,
    system_prompt: str | None = None,
) -> None:
    render_header(cwd, provider_name, model, base_url)
    if session:
        suffix = " (resumed)" if session.resumed else ""
        console.print(f"[dim]session: {session.session_id}{suffix}[/dim]")
    provider = create_provider(provider_name, model, base_url)
    run_agent(
        prompt,
        provider,
        default_tools(),
        max_steps=max_steps,
        cwd=cwd,
        permission_mode=permission_mode,
        session=session,
        system_prompt=system_prompt,
    )


def _prompt() -> str | None:
    """读取一行用户输入，返回 stripped 内容，EOF/中断时返回 None。"""
    try:
        line = input("> ")
        return line.strip()
    except (EOFError, KeyboardInterrupt):
        return None


@app.callback(invoke_without_command=True)
def main_command(
    prompt: str = typer.Argument("", help="Prompt to send to the agent."),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", "-C"),
    provider: str = typer.Option("anthropic", "--provider"),
    model: str = typer.Option("deepseek-v4-flash", "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
    max_steps: int = typer.Option(8, "--max-steps"),
    permission_mode: str = typer.Option(
        "default",
        "--permission-mode",
        help="Permission mode: default, acceptEdits, plan",
    ),
    resume: str | None = typer.Option(
        None, "--resume", help="按 session id 恢复指定会话"
    ),
    continue_: bool = typer.Option(
        False, "--continue", "-c", help="恢复 cwd 最近一次会话"
    ),
) -> None:

    # 启动时只解析一次 cwd，让整次运行共享同一个工作目录。
    resolved_cwd = cwd.resolve()

    #
    session: Session | None = None
    if continue_:
        session = Session.load_latest(resolved_cwd)
        if session is None:
            console.print("[red]没有找到历史会话，无法 --continue。[/red]")
            raise typer.Exit(code=1)
    elif resume:
        session = Session.load_by_id(resolved_cwd, resume)
        if session is None:
            console.print(f"[red]找不到 session: {resume}[/red]")
            raise typer.Exit(code=1)

    system_prompt = build_system_prompt(cwd)
    text = prompt.strip()
    if text:
        if session is None:
            session = Session.create(resolved_cwd)
        run_once(
            text,
            resolved_cwd,
            provider,
            model,
            base_url,
            max_steps,
            permission_mode,
            session,
            system_prompt,
        )
        return

    # REPL 分支——命令后面没跟 prompt，走下面交互循环
    render_header(resolved_cwd, provider, model, base_url)
    console.print("输入 /help 查看命令，输入 /exit 退出。")
    if not session:
        session = Session.create(resolved_cwd)
    while True:
        line = _prompt()
        if line is None:  # EOF / Ctrl+D / Ctrl+C
            console.print("\nBye.")
            return
        if not line:
            continue
        if line == "/exit":
            console.print("Bye.")
            return
        if line.startswith("/") and handle_slash(line, resolved_cwd):
            continue
        run_once(
            text,
            resolved_cwd,
            provider,
            model,
            base_url,
            max_steps,
            permission_mode,
            session,
            system_prompt,
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
