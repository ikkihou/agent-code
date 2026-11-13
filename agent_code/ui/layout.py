"""底部状态栏与"输入区 Frame + 底部对齐"布局改造。"""

from __future__ import annotations

from typing import Any, Iterator

from prompt_toolkit.filters import to_filter
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.styles import Style

from ..runtime import RuntimeState


def bottom_toolbar(state: RuntimeState) -> str:
    """底部状态栏——当前模式 + 模型。"""
    mode = {"default": "default", "acceptEdits": "accept edits", "plan": "plan"}.get(
        state.permission_mode, state.permission_mode
    )
    active = next((t.active_form for t in state.todo_store if t.status == "in_progress"), "")
    todo = f" · {active}" if active else ""
    return f" {mode} · {state.model}{todo} "


def build_style() -> Style:
    """REPL 样式表：spinner / 待确认预览 / 状态栏 / Frame 边框。"""
    return Style.from_dict(
        {
            "spinner": "bold ansibrightcyan",
            "preview": "ansiyellow",
            "bottom-toolbar": "bg:#222222 ansiwhite",
            # Frame 边框：暗灰蓝，低调但能与 scrollback 内容区分开。
            "frame.border": "#556677",
        }
    )


def _install_bottom_anchored_layout(session: PromptSession[str]) -> None:
    """把 PromptSession 的布局改成"输入区钉在终端底部"。

    pt 默认把渲染区（光标到屏底的全部行）整体交给 prompt 布局，且输入
    buffer 窗口会吸收所有剩余高度——表现为输入框贴着 scrollback 尾部、
    下方大片留白。改法：在布局根 HSplit 头部插一个 spacer（weight 极大、
    preferred=0 的弹性窗口，吸收全部剩余高度），再给 buffer 窗口打上
    dont_extend_height——它就停在内容高度（message 区 + 输入行），被
    spacer 压到最底。其余子项（校验栏 / bottom_toolbar 等）顺序不变。

    就地改动原 Window 对象（不换新实例）：Layout 的 focus 栈、visible_
    windows 都持有原引用，换包一层会让这些引用脱链。
    """
    root = session.layout.container  # 布局根 HSplit（_Split）
    buffer_window = next(
        w
        for w in _iter_windows(root)
        if isinstance(w.content, BufferControl) and w.content.buffer is session.default_buffer
    )
    buffer_window.dont_extend_height = to_filter(True)
    # spacer：preferred=0 不参与首选高度分配，max 无上限 + weight 压倒性
    # 大 → "增长到 max" 阶段的剩余高度全归它，把兄弟子项挤到底部。
    spacer = Window(width=Dimension(preferred=0), height=Dimension(weight=1000))
    root.children.insert(0, spacer)


def _iter_windows(root: Any) -> Iterator[Any]:
    """深度优先遍历布局树，产出所有 Window（含 floats 与条件分支）。"""
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, Window):
            yield node
        stack.extend(node.get_children())
