"""
##
##       filename: model.py
##        created: 2026/06/14
##         author: Paul_Bao
##            IDE: Neovim
##       Version : 1.0
##       Contact : paulbao@mail.ecust.edu.cn
"""

# here put the import lib
from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from anthropic import Anthropic


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class ModelResponse:
    text: str | None = None
    tool_calls: list[ToolCall] | None = None
    assistant_content: list[dict[str, Any]] | None = None
    stop_reason: str = "end_turn"


class CancellationSignal(Protocol):
    """Provider-facing cancellation contract implemented by threading.Event."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class ModelRequestAborted(RuntimeError):
    """Raised when the caller cancels an in-flight model request."""


@dataclass(frozen=True)
class ModelStreamEvent:
    type: Literal["text_delta", "completed"]
    text: str | None = None
    response: ModelResponse | None = None


class ModelProvider(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
    ) -> ModelResponse: ...

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: CancellationSignal | None = None,
    ) -> Iterator[ModelStreamEvent]: ...


def _load_claude_settings() -> dict[str, str]:
    """从 ~/.claude/settings.json 的 env 字段加载环境变量。"""
    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(settings_path.read_text())
        return data.get("env", {})
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _to_anthropic_tools(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }
        for tool in tools
    ]


def _parse_tool_input(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _content_block_to_dict(block: Any) -> dict[str, Any]:
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)

    if hasattr(block, "dict"):
        return block.dict(exclude_none=True)

    data = {"type": block.type}
    for name in ("text", "id", "name", "input", "thinking", "signature"):
        if hasattr(block, name):
            data[name] = getattr(block, name)

    return data


def _to_model_response(response: Any) -> ModelResponse:
    """Convert an Anthropic-compatible response into the provider-neutral model."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    assistant_content: list[dict[str, Any]] = []

    for block in response.content:
        # Preserve thinking/signature and unknown compatible fields for later turns.
        assistant_content.append(_content_block_to_dict(block))

        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=_parse_tool_input(block.input),
                )
            )

    return ModelResponse(
        text="\n".join(text_parts) or None,
        tool_calls=tool_calls or None,
        assistant_content=assistant_content,
        stop_reason=response.stop_reason or "end_turn",
    )


def _completed_response(events: Iterator[ModelStreamEvent]) -> ModelResponse:
    for event in events:
        if event.type == "completed" and event.response is not None:
            return event.response
    raise RuntimeError("provider stream ended without a completed response")


class MockProvider:
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
    ) -> ModelResponse:
        return _completed_response(self.complete_stream(messages, tools, system))

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: CancellationSignal | None = None,
    ) -> Iterator[ModelStreamEvent]:
        if signal is not None and signal.is_set():
            raise ModelRequestAborted()

        # 一个假模型，固定回一句话，够用来打通 CLI <-> Provider 这条边界。
        response = ModelResponse(text="我是 MockProvider，你说了：mocking")
        yield ModelStreamEvent(type="text_delta", text=response.text)

        if signal is not None and signal.is_set():
            raise ModelRequestAborted()
        yield ModelStreamEvent(type="completed", response=response)


class AnthropicProvider:
    def __init__(
        self,
        model: str = "deepseek-v4-flash",
        max_tokens: int = 4096,
        base_url: str | None = None,
    ) -> None:
        # 优先从 ~/.claude/settings.json 的 env 字段读取，其次回退到 os.environ。
        claude_env = _load_claude_settings()

        api_key = (
            claude_env.get("ANTHROPIC_AUTH_TOKEN")
            or claude_env.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY")
        )
        if not api_key:
            raise RuntimeError(
                "请先设置 ANTHROPIC_AUTH_TOKEN，例如：export ANTHROPIC_AUTH_TOKEN='sk-...'"
            )

        self.model = model
        self.max_tokens = max_tokens
        self.base_url = (
            base_url
            or claude_env.get("ANTHROPIC_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.deepseek.com/anthropic"
        )
        self.client = Anthropic(api_key=api_key, base_url=self.base_url)

    def _request_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }

        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)

        if system:
            kwargs["system"] = system

        return kwargs

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
    ) -> ModelResponse:
        return _completed_response(self.complete_stream(messages, tools, system))

    def complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        system: str | None = None,
        *,
        signal: CancellationSignal | None = None,
    ) -> Iterator[ModelStreamEvent]:
        """Yield text deltas and actively close the HTTP stream on cancellation."""
        if signal is not None and signal.is_set():
            raise ModelRequestAborted()

        kwargs = self._request_kwargs(messages, tools, system)
        with self.client.messages.stream(**kwargs) as stream:
            finished = threading.Event()
            watcher: threading.Thread | None = None

            if signal is not None:

                def close_on_abort() -> None:
                    while not finished.is_set():
                        if signal.wait(0.05):
                            try:
                                stream.close()
                            except Exception:
                                # The consumer maps the resulting read failure to
                                # ModelRequestAborted when the signal is set.
                                pass
                            return

                watcher = threading.Thread(
                    target=close_on_abort,
                    name="model-stream-canceller",
                    daemon=True,
                )
                watcher.start()

            try:
                for event in stream:
                    if signal is not None and signal.is_set():
                        raise ModelRequestAborted()

                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield ModelStreamEvent(
                            type="text_delta",
                            text=event.delta.text,
                        )

                if signal is not None and signal.is_set():
                    raise ModelRequestAborted()

                yield ModelStreamEvent(
                    type="completed",
                    response=_to_model_response(stream.get_final_message()),
                )
            except ModelRequestAborted:
                raise
            except Exception:
                if signal is not None and signal.is_set():
                    raise ModelRequestAborted() from None
                raise
            finally:
                finished.set()
                stream.close()
                if watcher is not None:
                    watcher.join(timeout=0.1)


def create_provider(
    name: str,
    model: str,
    base_url: str | None = None,
) -> ModelProvider:
    if name == "anthropic":
        return AnthropicProvider(model=model, base_url=base_url)
    if name == "mock":
        return MockProvider()
    raise ValueError(f"unknown provider: {name}")
