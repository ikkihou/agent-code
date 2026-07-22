from __future__ import annotations

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File      :   runtime.py
Date      :   2026-07-22 15:22:36
Author    :   baoyihui
Contact   :   yihui.bao@apopgeei.com
"""

# here put the import lib
from queue import Queue
import threading
from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    permission_mode: str = "default"
    model: str = "deepseek-v4-flash"
    provider: str = "anthropic"
    abort_event: threading.Event = field(default_factory=threading.Event)
    input_queue: Queue[str] = field(default_factory=Queue)
