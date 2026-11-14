"""Bottom status bar and the input-area Frame layout reshaping."""

from __future__ import annotations

from typing import Any, Callable, Iterator

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import to_filter
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.layout.containers import Float, FloatContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu, MultiColumnCompletionsMenu
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets.base import Border

from ..runtime import RuntimeState

# Completion menus are Floats, so they do not need rows inside the input frame. This
# is only the upper bound used when measuring the menu's own preferred height.
MENU_MAX_HEIGHT = 16


def bottom_toolbar(state: RuntimeState) -> str:
    """Bottom status bar: current mode + model only."""
    mode = {"default": "default", "acceptEdits": "accept edits", "plan": "plan"}.get(
        state.permission_mode, state.permission_mode
    )
    return f" {mode} · {state.model} "


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

    The completion menu floats just below the frame, and a dynamic spacer window between
    the frame and the status bar (see ``_install_menu_spacer``) reserves the rows the menu
    needs, so the input window itself never grows: its height stays content-derived
    (``min=0``).

    The Window is modified in place: Layout's focus stack and visible_windows hold the
    original references, so wrapping it would break those links.
    """
    for window in _iter_windows(session.layout.container):
        control = window.content
        if isinstance(control, BufferControl) and control.buffer is session.default_buffer:
            window.dont_extend_height = to_filter(True)
            window.height = Dimension(min=0)
            return


def _completion_menu_height(menu_content: Any) -> int:
    """Height the completion menu needs right now (0 when closed), capped.

    Shared by the menu Float (so it reports a real height) and the spacer window (so the
    reserved gap between frame and status bar tracks the pending completions).
    """
    state = get_app().current_buffer.complete_state
    if state is None or not state.completions:
        return 0
    width = get_app().output.get_size().columns
    height = menu_content.preferred_height(width, MENU_MAX_HEIGHT).preferred
    return max(0, min(height or 0, MENU_MAX_HEIGHT))


class _CompletionMenuFloat(Float):
    """Completion Float positioned just below the input frame.

    prompt_toolkit's stock completion menus use ``ycursor=True``, which anchors them
    below the cursor *inside* the frame body -- a one-row body clips them away, which is
    why the previous layout reserved rows and stretched the frame. This Float keeps
    ``xcursor`` for the horizontal position but anchors vertically just below the frame's
    bottom rule (``top = body_height + 1``). The rows it covers are reserved by
    ``_install_menu_spacer`` (a gap between the frame and the status bar), so the menu
    never overlaps the status bar or grows the frame.

    ``get_height`` mirrors the pending completion count (capped at the menu's normal
    16-row maximum) so the Float has a real height for the top-anchored formula.
    """

    def __init__(
        self,
        content: Any,
        input_window: Window,
        *,
        transparent: Any = True,
        z_index: int = 1,
    ) -> None:
        self._input_window = input_window
        super().__init__(
            content=content,
            xcursor=True,
            transparent=transparent,
            z_index=z_index,
        )

    @property
    def top(self) -> int:
        info = self._input_window.render_info
        body_height = info.window_height if info is not None else 1
        return body_height + 1

    @top.setter
    def top(self, value: int | None) -> None:
        # Float.__init__ assigns top=None; the dynamic property above is authoritative.
        self._configured_top = value

    def get_height(self) -> int | None:
        return _completion_menu_height(self.content)


def _default_buffer_window(session: PromptSession[str]) -> Window | None:
    """Find the Window that owns the prompt's default input buffer."""
    for window in _iter_windows(session.layout.container):
        control = window.content
        if isinstance(control, BufferControl) and control.buffer is session.default_buffer:
            return window
    return None


def _relocate_completion_menus(session: PromptSession[str]) -> None:
    """Move the built-in completion Floats below the frame and reserve their space.

    prompt_toolkit builds CompletionsMenu / MultiColumnCompletionsMenu as cursor-
    anchored Floats inside the frame body. Replacing each Float with a
    ``_CompletionMenuFloat`` preserves the menu content/filters and navigation keys, but
    changes the vertical anchor so the menu floats just below the frame. A spacer window
    (``_install_menu_spacer``) then reserves the rows the menu needs between the frame and
    the status bar, so the menu never grows the frame, clips off the terminal, or covers
    the status bar.
    """
    input_window = _default_buffer_window(session)
    if input_window is None:
        return

    menu_content: Any | None = None
    seen: set[int] = set()
    for container in _iter_containers(session.layout.container):
        if id(container) in seen:
            continue
        seen.add(id(container))
        if not isinstance(container, FloatContainer):
            continue
        for index, float_ in enumerate(container.floats):
            if isinstance(float_, _CompletionMenuFloat):
                continue
            if isinstance(float_.content, (CompletionsMenu, MultiColumnCompletionsMenu)):
                if menu_content is None:
                    menu_content = float_.content
                container.floats[index] = _CompletionMenuFloat(
                    float_.content,
                    input_window,
                    transparent=float_.transparent,
                    z_index=float_.z_index,
                )

    if menu_content is not None:
        _install_menu_spacer(session, menu_content)


def _frame_branch_index(session: PromptSession[str]) -> int:
    """Index of the root child that wraps the input frame (default: append at end)."""
    root = session.layout.container
    frame_split = _find_frame_split(session)
    if frame_split is None:
        return len(root.children)
    for index, child in enumerate(root.children):
        if any(node is frame_split for node in _iter_containers(child)):
            return index
    return len(root.children)


def _install_menu_spacer(session: PromptSession[str], menu_content: Any) -> None:
    """Reserve a gap between the input frame and the status bar for the completion menu.

    The menu is a Float just below the frame, so without this spacer its rows would land
    on the status bar. A content-less window whose height follows the menu's preferred
    height (0 while the menu is closed) pushes the status bar down only while completions
    are shown -- and grows automatically as more slash commands are added.
    """
    root = session.layout.container
    if not hasattr(root, "children"):
        return

    def height() -> Dimension:
        rows = _completion_menu_height(menu_content)
        return Dimension(min=rows, max=rows)

    root.children.insert(
        _frame_branch_index(session) + 1,
        Window(
            content=FormattedTextControl(text=""),
            height=height,
            dont_extend_height=True,
        ),
    )


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
