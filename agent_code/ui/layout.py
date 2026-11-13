"""Bottom status bar and the "input area Frame + bottom anchoring" layout reshaping."""

from __future__ import annotations

from typing import Any, Iterator

from prompt_toolkit.filters import to_filter
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets.base import Border

from ..runtime import RuntimeState


def bottom_toolbar(state: RuntimeState) -> str:
    """Bottom status bar: current mode + model."""
    mode = {"default": "default", "acceptEdits": "accept edits", "plan": "plan"}.get(
        state.permission_mode, state.permission_mode
    )
    active = next((t.active_form for t in state.todo_store if t.status == "in_progress"), "")
    todo = f" · {active}" if active else ""
    return f" {mode} · {state.model}{todo} "


def build_style() -> Style:
    """REPL stylesheet: spinner / pending preview / status bar / Frame border."""
    return Style.from_dict(
        {
            "spinner": "bold ansibrightcyan",
            "preview": "ansiyellow",
            "bottom-toolbar": "bg:#222222 ansiwhite",
            # Frame border: muted slate blue, understated but distinct from scrollback content.
            "frame.border": "#556677",
        }
    )


def _install_bottom_anchored_layout(session: PromptSession[str]) -> None:
    """Reshape the PromptSession layout so the "input area is pinned to the terminal bottom".

    By default pt hands the whole render region (everything from the cursor down to the
    screen bottom) to the prompt layout, and the input buffer window absorbs all
    remaining height -- which puts the input box right after the scrollback tail with a
    large blank area below. The fix: insert a spacer at the head of the layout root
    HSplit (a flexible window with a huge weight and preferred=0 that absorbs all the
    remaining height), then set dont_extend_height on the buffer window -- it stops at
    its content height (message area + input line) and gets pushed to the bottom by the
    spacer. The other children (validation toolbar / bottom_toolbar, ...) keep their
    order.

    The original Window objects are modified in place (no new instances): Layout's focus
    stack and visible_windows hold the original references, and wrapping them would
    break those links.
    """
    root = session.layout.container  # layout root HSplit (_Split)
    buffer_window = next(
        w
        for w in _iter_windows(root)
        if isinstance(w.content, BufferControl) and w.content.buffer is session.default_buffer
    )
    buffer_window.dont_extend_height = to_filter(True)
    # spacer: preferred=0 keeps it out of preferred-height allocation; unbounded max
    # plus an overwhelming weight -> every remaining pixel in the "grow to max" phase
    # goes to it, squeezing its siblings to the bottom.
    spacer = Window(width=Dimension(preferred=0), height=Dimension(weight=1000))
    root.children.insert(0, spacer)


def _strip_frame_side_borders(session: PromptSession[str]) -> None:
    """Remove the Frame's left/right vertical borders, keeping only the horizontal
    rules (top ┌──┐ / bottom └──┘, no │ columns).

    pt's ``show_frame=True`` wraps the input in ``Frame``: an HSplit of
    [top rule, VSplit(│ body │), bottom rule]. We want the rules (they separate the
    input area from scrollback) but not the side columns -- the Live area is
    full-width text, so bars only indent it. The middle VSplit is
    [Window(│), DynamicContainer(body), Window(│)]; collapsing the two border
    windows to zero width removes the columns while leaving the body (and thus the
    input buffer window) untouched, so Layout's focus references stay valid.

    The Frame split is located by shape, not position, so this works whether or
    not any spacer has been inserted into the layout root already -- and whether
    ``show_frame`` is currently True or False: pt builds the Frame branch into the
    layout as the ConditionalContainer's content regardless of the filter value, so
    the collapse sticks even though ``show_frame`` is only turned on per
    ``prompt_async(show_frame=True)`` call.
    """
    for split in _iter_containers(session.layout.container):
        if not isinstance(split, HSplit) or len(split.children) != 3:
            continue
        middle = split.children[1]
        if not (isinstance(middle, VSplit) and len(middle.children) == 3):
            continue
        left, _body, right = middle.children
        if _is_vertical_bar(left) and _is_vertical_bar(right):
            left.width = Dimension.exact(0)
            right.width = Dimension.exact(0)
            return


def _is_vertical_bar(container: Any) -> bool:
    """True for Frame's 1-column vertical-border fill windows."""
    return isinstance(container, Window) and container.char == Border.VERTICAL


def _iter_containers(root: Any) -> Iterator[Any]:
    """Depth-first walk yielding every container node (floats included)."""
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.get_children())


def _iter_windows(root: Any) -> Iterator[Any]:
    """Depth-first walk of the layout tree, yielding every Window (floats and
    conditional branches included)."""
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, Window):
            yield node
        stack.extend(node.get_children())
