"""prompt_toolkit ChoiceInput 试验。

ChoiceInput().prompt() 会创建真实 Application 并绑定当前终端输出；在
pytest 下 stdout 被捕获、没有真实 Windows 控制台，因此一跑就抛
NoConsoleScreenBufferError（"Are you running cmd.exe?"）。模块顶层代码
在 pytest 收集（import）阶段就会执行，所以交互演示绝不能放在顶层。

- pytest 收集/运行时只执行下面不碰控制台的构造级断言；
- 想真机试交互选择，请在真实终端里直接运行本文件：
      uv run python tests/test_prompt_toolkit.py
  （方向键/Tab 选择、Enter 确认、Ctrl+C 中断。）

ChoiceInput 的 default 契约（见其文档字符串）：不传 default 时按第一个
option 的 value 作为默认选择；该回退发生在 RadioList 构建期，属第三方
控件内部行为，这里不做断言。
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
    """构造 ChoiceInput 不触碰控制台，只把参数原样收进实例。"""
    picker = ChoiceInput(message=MESSAGE, options=OPTIONS, default=DEFAULT)

    assert picker.message == MESSAGE
    assert list(picker.options) == OPTIONS
    assert picker.default == DEFAULT
    # 每个 option 都是 (value, label) 二元组，value 即 prompt() 的返回值。
    for value, label in picker.options:
        assert isinstance(label, str) and label
    assert picker.options[0][0] == DEFAULT


def _demo() -> None:
    """真实终端交互演示：让用户从菜单里选一道菜并打印结果。"""
    result = ChoiceInput(
        message=MESSAGE,
        options=OPTIONS,
        default=DEFAULT,
    ).prompt()
    print(f"\nYou picked: {result}")


if __name__ == "__main__":
    _demo()
