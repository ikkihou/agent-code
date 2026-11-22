"""Token-ratio context accounting: budget math, window resolution, estimation, /context."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_code.context import (
    DEFAULT_COMPACT_THRESHOLD,
    ContextBudget,
    TokenUsage,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    ratio_bar,
    resolve_window,
)
from agent_code.model import MockProvider, _to_model_response
from agent_code.runtime import RuntimeState
from agent_code.slash import SlashContext, dispatch_slash

# ---- estimation ------------------------------------------------------------


def test_estimate_tokens_is_chars_over_four_and_never_zero() -> None:
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("") == 1


def test_estimate_message_tokens_handles_text_and_structured_content() -> None:
    assert estimate_message_tokens({"role": "user", "content": "x" * 40}) == 10
    structured = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "a", "content": "y" * 40}],
    }
    # Structured content is counted through its JSON serialization, so it is not zero.
    assert estimate_message_tokens(structured) >= 10
    assert estimate_message_tokens({"role": "user", "content": None}) == 0
    assert (
        estimate_messages_tokens(
            [{"role": "user", "content": "x" * 40}, {"role": "user", "content": "x" * 40}]
        )
        == 20
    )


# ---- TokenUsage ------------------------------------------------------------


def test_token_usage_effective_context_uses_input_tokens() -> None:
    # Anthropic's input_tokens already includes cache reads: no double counting.
    usage = TokenUsage(input_tokens=500, cache_read_tokens=400, output_tokens=20)
    assert usage.effective_context == 500


def test_token_usage_falls_back_to_cache_reads() -> None:
    usage = TokenUsage(input_tokens=0, cache_read_tokens=400)
    assert usage.effective_context == 400


def test_model_response_parses_usage_from_the_api() -> None:
    raw = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=120,
            output_tokens=30,
            cache_read_input_tokens=40,
        ),
    )
    response = _to_model_response(raw)
    assert response.usage == TokenUsage(
        input_tokens=120,
        cache_read_tokens=40,
        output_tokens=30,
    )


def test_model_response_without_usage_is_none() -> None:
    raw = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason="end_turn",
        usage=None,
    )
    assert _to_model_response(raw).usage is None


def test_mock_provider_reports_deterministic_usage() -> None:
    messages = [{"role": "user", "content": "x" * 400}]
    response = MockProvider().complete(messages)
    assert response.usage is not None
    assert response.usage.input_tokens == estimate_messages_tokens(messages) + 50


# ---- ContextBudget ---------------------------------------------------------


def test_budget_observe_ratio_and_compaction_threshold() -> None:
    budget = ContextBudget(
        window_tokens=1000,
        compact_threshold=0.80,
        target_ratio=0.55,
        reserve_tokens=0,
    )
    assert budget.usage_ratio == 0.0
    assert not budget.should_compact()

    budget.observe(TokenUsage(input_tokens=700, output_tokens=90))
    assert budget.estimated_tokens == 790
    assert budget.usage_ratio == pytest.approx(0.79)
    # 790 < 800: not yet.
    assert not budget.should_compact()

    budget.add_estimate(20)  # messages appended after the report
    assert budget.estimated_tokens == 810
    assert budget.usage_ratio == pytest.approx(0.81)
    assert budget.should_compact()


def test_budget_reserve_triggers_before_the_threshold() -> None:
    budget = ContextBudget(window_tokens=1000, compact_threshold=0.80, reserve_tokens=100)
    budget.observe(TokenUsage(input_tokens=700))
    # 700 + 100 >= 800 -> the next response would not fit, compact now.
    assert budget.should_compact()


def test_budget_target_tokens() -> None:
    budget = ContextBudget(window_tokens=1000, target_ratio=0.55)
    assert budget.target_tokens() == 550
    assert ContextBudget(window_tokens=101, target_ratio=0.5).target_tokens() == 50


def test_budget_observe_missing_usage_keeps_previous_estimate() -> None:
    budget = ContextBudget(window_tokens=1000)
    budget.observe(TokenUsage(input_tokens=400))
    budget.observe(None)
    assert budget.estimated_tokens == 400


def test_budget_reset_assigns_the_assembled_estimate() -> None:
    budget = ContextBudget(window_tokens=1000)
    budget.reset(400)
    assert budget.estimated_tokens == 400

    # Assignment, not a floor: the assembled list is exactly what the next request sends.
    budget.reset(300)
    assert budget.estimated_tokens == 300

    budget.add_estimate(50)
    assert budget.estimated_tokens == 350
    budget.reset(900)
    assert budget.estimated_tokens == 900


def test_budget_reset_never_goes_negative() -> None:
    budget = ContextBudget(window_tokens=1000)
    budget.reset(-5)
    assert budget.estimated_tokens == 0


def test_budget_observe_zero_usage_resets_to_zero() -> None:
    budget = ContextBudget(window_tokens=1000)
    budget.reset(500)
    budget.observe(TokenUsage(input_tokens=0, output_tokens=0))
    assert budget.estimated_tokens == 0


def test_budget_record_compaction_updates_stats_and_estimate() -> None:
    budget = ContextBudget(window_tokens=1000)
    budget.observe(TokenUsage(input_tokens=940))
    budget.record_compaction(0.94, 0.51, 11)

    assert budget.compactions == 1
    assert budget.last_before_ratio == pytest.approx(0.94)
    assert budget.last_after_ratio == pytest.approx(0.51)
    assert budget.last_summarized_messages == 11
    assert budget.estimated_tokens == 510


def test_budget_window_is_never_zero() -> None:
    assert ContextBudget(window_tokens=0).window_tokens == 1


# ---- window resolution -----------------------------------------------------


def test_resolve_window_precedence_override_then_env_then_default() -> None:
    env = {"CONTEXT_WINDOW_TOKENS": "2048"}
    assert resolve_window("mock", env=env, override=4096) == 4096
    assert resolve_window("mock", env=env) == 2048
    assert resolve_window("mock", env={}) == 8192


def test_resolve_window_provider_defaults() -> None:
    assert resolve_window("anthropic", env={}) == 131072
    assert resolve_window("deepseek", env={}) == 131072
    assert resolve_window("mock", env={}) == 8192
    # Unknown providers get the generic fallback rather than failing.
    assert resolve_window("unknown-provider", env={}) == 131072


def test_resolve_window_ignores_invalid_values() -> None:
    assert resolve_window("mock", env={"CONTEXT_WINDOW_TOKENS": "not-a-number"}) == 8192
    assert resolve_window("mock", env={"CONTEXT_WINDOW_TOKENS": "0"}) == 8192
    assert resolve_window("mock", env={"CONTEXT_WINDOW_TOKENS": "2048"}, override=0) == 2048


# ---- ratio bar -------------------------------------------------------------


def test_ratio_bar_clamps_and_shows_percent() -> None:
    assert ratio_bar(0.55, width=20).endswith("55%")
    assert ratio_bar(0.55, width=20).count("#") == 11
    assert ratio_bar(-1.0, width=10).count("#") == 0
    assert ratio_bar(2.0, width=10).count("#") == 10


# ---- /context --------------------------------------------------------------


def _slash_context(
    state: RuntimeState | None,
    provider: str = "mock",
    context_window: int = 0,
) -> SlashContext:
    return SlashContext(
        cwd=Path("/tmp/project"),
        permission_mode="default",
        model="mock-model",
        provider=provider,
        session_id=None,
        state=state,
        context_window=context_window,
    )


def test_context_command_renders_without_a_live_budget() -> None:
    result = dispatch_slash("/context", _slash_context(None))

    assert result.handled
    assert "model: mock/mock-model" in result.message
    assert "context window: 8,192 tokens" in result.message
    assert f"compact threshold: {DEFAULT_COMPACT_THRESHOLD:.0%}" in result.message
    assert "no live budget" in result.message


def test_context_command_reports_the_cli_window_override() -> None:
    result = dispatch_slash("/context", _slash_context(None, context_window=4096))

    assert "context window: 4,096 tokens" in result.message


def test_context_command_renders_with_a_live_budget() -> None:
    state = RuntimeState(provider="mock")
    budget = ContextBudget(window_tokens=1000, compact_threshold=0.8)
    budget.observe(TokenUsage(input_tokens=600, output_tokens=50))
    budget.record_compaction(0.65, 0.4, 3)
    state.context_budget = budget

    result = dispatch_slash("/context", _slash_context(state))

    assert result.handled
    assert "context window: 1,000 tokens" in result.message
    assert "40%" in result.message
    assert "400 tokens estimated" in result.message
    assert "last compaction: 65% → 40% (3 messages summarized)" in result.message
