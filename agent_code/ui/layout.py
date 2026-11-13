"""Bottom status bar and the input-area Frame layout reshaping."""

from __future__ import annotations

from typing import Any, Callable, Iterator

from prompt_toolkit.filters import to_filter
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets.base import Border

from ..runtime import RuntimeState

# Rows the layout grows by while the completion menu is open: pt draws the menu as a
# Float inside the input window, so the window must be tall enough to hold it.
MENU_ROWS = 8


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


def _collapse_input_height(session: PromptSession[str]) -> None:
    """Make the input area stop at its content height instead of absorbing the rest of
    the screen -- the layout then follows the content, right under the scrollback tail.

    By default pt gives the input buffer window a bare ``Dimension()`` (min 0, no
    maximum), so inside the root HSplit it stretches to fill everything from the
    scrollback tail down to the bottom_toolbar -- the frame then encloses a tall empty
    box, and content committed to scrollback with ``run_in_terminal`` lands *inside* it
    (``Renderer.erase`` puts the cursor at the layout's top row, which is also the input
    row, so the write overwrites the input line instead of scrolling up).

    Pinning the window to its content height (``dont_extend_height``) shrinks the whole
    layout to "Live window + frame + toolbar".

    One exception: the completion menu is a Float inside the frame body, so it is
    clipped to that body's height -- a one-row body would hide it entirely. The height
    is therefore dynamic: while the buffer has completions pending the layout grows to
    ``MENU_ROWS`` and shrinks back as soon as the menu closes (same effect as pt's
    ``reserve_space_for_menu``, except it only takes the rows while the menu is up).

    The Window is modified in place: Layout's focus stack and visible_windows hold the
    original references, so wrapping it would break those links.
    """
    for window in _iter_windows(session.layout.container):
        control = window.content
        if isinstance(control, BufferControl) and control.buffer is session.default_buffer:
            window.dont_extend_height = to_filter(True)
            window.height = lambda: Dimension(
                min=MENU_ROWS if control.buffer.complete_state is not None else 0
            )
            return


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

    The Frame split is located by shape, not position, so this works regardless of
    where in the layout root the frame branch sits -- and whether ``show_frame`` is
    currently True or False: pt builds the Frame branch into the layout as the
    ConditionalContainer's content regardless of the filter value, so the collapse
    sticks even though ``show_frame`` is only turned on per
    ``prompt_async(show_frame=True)`` call.
    """
    split = _find_frame_split(session)
    if split is None:
        return  # unexpected shape (pt upgrade?): fail soft, keep side borders
    left, _body, right = split.children[1].children
    left.width = Dimension.exact(0)
    right.width = Dimension.exact(0)


def _install_live_area_window(
    session: PromptSession[str], get_text: Callable[[], AnyFormattedText]
) -> Window:
    """Insert a Live-area window directly above the input frame.

    The Live area (spinner / streaming tail / pending prompt question) must not sit
    inside the input frame: the frame is there to delimit the input line, and
    in-progress output belongs visually with the scrollback above it. pt's prompt
    ``message`` is rendered *inside* the frame, so the Live area cannot ride on it --
    it gets its own window in the layout root, placed right before the frame branch.

    ``dont_extend_height`` + a content-derived preferred height make the window
    collapse to zero rows when there is nothing in progress, and grow line by line
    while a turn streams.
    """
    live_window = Window(
        FormattedTextControl(text=get_text),
        height=Dimension(min=0),
        dont_extend_height=True,
        wrap_lines=False,
    )
    root = session.layout.container
    frame_split = _find_frame_split(session)
    insert_at = len(root.children)
    if frame_split is not None:
        # Place the Live area immediately above the root child that wraps the frame.
        for index, child in enumerate(root.children):
            if any(node is frame_split for node in _iter_containers(child)):
                insert_at = index
                break
    root.children.insert(insert_at, live_window)
    return live_window


def _find_frame_split(session: PromptSession[str]) -> HSplit | None:
    """Locate the Frame's outer HSplit [top rule, middle VSplit, bottom rule] by shape."""
    for split in _iter_containers(session.layout.container):
        if not isinstance(split, HSplit) or len(split.children) != 3:
            continue
        middle = split.children[1]
        if not (isinstance(middle, VSplit) and len(middle.children) == 3):
            continue
        left, _body, right = middle.children
        if _is_vertical_bar(left) and _is_vertical_bar(right):
            return split
    return None


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


def _iter_windows(root: Any) -> Iterator[Window]:
    """Depth-first walk of the layout tree, yielding every Window (floats and
    conditional branches included)."""
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, Window):
            yield node
        stack.extend(node.get_children())
