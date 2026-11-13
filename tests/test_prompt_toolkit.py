"""An experiment with prompt_toolkit's ChoiceInput.

ChoiceInput().prompt() creates a real Application bound to the current terminal
output; under pytest stdout is captured and there is no real Windows console, so it
raises NoConsoleScreenBufferError ("Are you running cmd.exe?") immediately. Module-level
code runs during pytest collection (import), so the interactive demo must never live at
the top level.

- pytest collection/runs only execute the console-free constructor assertions below;
- to try the interactive picker for real, run this file directly in a real terminal:
      uv run python tests/test_prompt_toolkit.py
  (arrow keys/Tab to select, Enter to confirm, Ctrl+C to cancel.)

ChoiceInput's default contract (see its docstring): without a default, the value of the
first option becomes the default choice; that fallback happens while RadioList is built
and is third-party widget behavior, so it is not asserted here.
"""

from prompt_toolkit.shortcuts.choice_input import ChoiceInput

MESSAGE = "Please select a dish:"
OPTIONS = [
    ("pizza", "Pizza with mushrooms"),
    ("salad", "Salad with tomatoes"),
    ("sushi", "Sushi"),
]
DEFAULT = "pizza"


def test_choice_input_stores_message_options_default() -> None:
    """Constructing ChoiceInput never touches the console; it just stores the arguments."""
    picker = ChoiceInput(message=MESSAGE, options=OPTIONS, default=DEFAULT)

    assert picker.message == MESSAGE
    assert list(picker.options) == OPTIONS
    assert picker.default == DEFAULT
    # Every option is a (value, label) pair; value is what prompt() returns.
    for value, label in picker.options:
        assert isinstance(label, str) and label
    assert picker.options[0][0] == DEFAULT


def _demo() -> None:
    """Real-terminal interactive demo: let the user pick a dish from the menu and print it."""
    result = ChoiceInput(
        message=MESSAGE,
        options=OPTIONS,
        default=DEFAULT,
    ).prompt()
    print(f"\nYou picked: {result}")


if __name__ == "__main__":
    _demo()
