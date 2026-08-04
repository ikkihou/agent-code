from __future__ import annotations

import sys

from agent_code.hooks import _decode_hook_output, _run_hook_command


def test_run_hook_command_uses_utf8_for_non_gbk_stdin_and_stdout(tmp_path) -> None:
    text = "hello \U0001f60a"
    command = (
        f'"{sys.executable}" -c "import json,sys; '
        "data=json.load(sys.stdin); "
        "sys.stdout.write(data['tool_input']['text'])\""
    )

    success, output = _run_hook_command(
        command,
        {"tool_input": {"text": text}},
        tmp_path,
    )

    assert success is True
    assert output == text


def test_decode_hook_output_falls_back_to_preferred_legacy_encoding(monkeypatch) -> None:
    monkeypatch.setattr("locale.getpreferredencoding", lambda do_setlocale=True: "gbk")

    assert _decode_hook_output("\u4e2d\u6587".encode("gbk")) == "\u4e2d\u6587"
