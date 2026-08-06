"""工具包公开 API。

保持与重构前 `tools.py` 相同的导出面，`from .tools import ...` 调用点零改动：
- `ToolContext` / `ToolRegistry`（agent.py、cron_tools.py、slash.py、测试）
- `default_tools()`（cli.py）
"""

from .core import Tool, ToolContext, ToolFunc, ToolRegistry
from .registry import default_tools

__all__ = ["Tool", "ToolContext", "ToolFunc", "ToolRegistry", "default_tools"]
