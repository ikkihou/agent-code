"""
File      :   runtime.py
Date      :   2026-07-22 15:22:36
Author    :   baoyihui
Contact   :   yihui.bao@apopgeei.com
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from queue import Queue


@dataclass
class TodoItem:
    content: str
    status: str
    active_form: str


@dataclass
class RuntimeState:
    permission_mode: str = "default"
    model: str = "deepseek-v4-flash"
    provider: str = "anthropic"
    abort_event: threading.Event = field(default_factory=threading.Event)
    input_queue: Queue[str] = field(default_factory=Queue)
    todo_store: list[TodoItem] = field(default_factory=list)

    def cycle_permission_mode(self) -> str:
        order = ["default", "acceptEdits", "plan"]
        idx = order.index(self.permission_mode) if self.permission_mode in order else 0
        self.permission_mode = order[(idx + 1) % len(order)]
        return self.permission_mode
