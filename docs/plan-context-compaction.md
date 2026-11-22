# Plan: Token-Ratio Context Compaction

Replace the message-count trigger (`len(messages) > 40`) with a token-budget-based
compaction system driven by the ratio of context tokens to the model's context
window. This document is the implementation contract for the executor agent.

## Goals

1. Track real context token usage from API `usage` reports (no tokenizer dependency).
2. Trigger compaction by **percentage of the context window** (default: compact at
   ≥ 80%, compact down to ≤ 55%).
3. Compact in a **structurally safe** way: never orphan a `tool_use` block from its
   `tool_result` (the current `compact_basic.compact()` can — this is a live
   protocol bug for Anthropic-compatible APIs).
4. Emit wire events so any frontend can render compaction (replace the ad-hoc
   `emit("compacted: ...")` line).
5. Expose status: `/context` slash command shows the current usage ratio.

## Non-goals

- No LLM-based summarization in this iteration (deterministic only; the summary
  builder stays pluggable for later).
- No prompt-cache-aware compaction (DeepSeek caching is out of scope).

## Current state (verified)

- Trigger: `agent_code/agent.py:164-167` — `if len(messages) > 40: compact(messages, keep=8)`.
- Compaction: `agent_code/compact_basic.py` — pins first 2 messages, keeps last 8,
  middle → `<compacted-history>` stats block. **Bug: cut boundaries ignore
  tool_use/tool_result pairing** — `messages[-keep:]` can start with a
  `tool_result` whose `tool_use` lives in the dropped middle, and the middle may
  end mid-pair. Anthropic-compatible endpoints reject orphaned `tool_result`.
- No token accounting: `ModelResponse` (`agent_code/model.py:40-44`) drops the
  `usage` field the API returns on every response.
- No context-window knowledge anywhere; `RuntimeState` (`runtime.py`) has
  `model`/`provider` but no window size.
- Wire: `wire.py` events are `TextDelta`/`Line`/`ToolCallStarted`/`ToolCallCompleted`/
  `DiffPreview`/`CommandPreview`/`PromptEcho`/`TurnStarted`/`TurnEnded`.

## Design

### 1. Token accounting — `agent_code/context.py` (new module)

```python
class ContextBudget:
    """Tracks context usage against the model window and decides when to compact."""
    def __init__(self, window_tokens: int, compact_threshold: float = 0.80,
                 target_ratio: float = 0.55, reserve_tokens: int = 4096): ...
    def observe(self, usage: TokenUsage) -> None      # from each completed response
    @property def usage_ratio(self) -> float          # latest estimate / window
    def should_compact(self) -> bool                  # estimate + reserve >= threshold * window
    def target_tokens(self) -> int                    # floor(target_ratio * window)
```

- `TokenUsage`: `input_tokens: int`, `cache_read_tokens: int = 0`,
  `output_tokens: int = 0`. Effective context = `input_tokens` (Anthropic's
  `input_tokens` already includes cache reads on this wire; if a provider
  separates them, add `cache_read_tokens` in).
- **Estimation between API calls**: the agent loop appends messages after each
  response; before the *next* request we know last request's `input_tokens` +
  last response's `output_tokens` + a chars/4 estimate of newly appended
  message text (`estimate_tokens(text) = max(1, len(text) // 4)`, counting
  `json.dumps` for structured blocks). This heuristic is the fallback; the
  authoritative number is the next real `usage` report.
- Window resolution: `CONTEXT_WINDOW_TOKENS` env var → per-provider default
  (anthropic/deepseek: 131072, mock: 8192) → CLI `--context-window` override.
  Keep the resolution function pure and testable: `resolve_window(provider, env, override)`.

### 2. Capture usage — `agent_code/model.py`

- Add `usage: TokenUsage | None = None` to `ModelResponse`.
- In `_to_model_response`, read `response.usage` (attribute access with
  `getattr` guards — the anthropic SDK type has `input_tokens`, `output_tokens`,
  `cache_read_input_tokens`); missing usage → `None`.
- `MockProvider` reports deterministic usage so tests can drive thresholds:
  usage `input_tokens =` a cheap estimate of the last message list length in
  tokens (e.g. `sum(estimate over messages) + 50`), so a test can pad a message
  and force a compact.

### 3. Pairing-safe compaction — `agent_code/compact_basic.py` (extend, don't rewrite)

- Add `find_safe_cut(messages, start, end)` helpers: a cut index is safe iff
  it does not split an assistant `tool_use` from the following user
  `tool_result` (same `tool_use_id`). Concretely: a cut may fall (a) between
  messages where the message *before* the cut is not an assistant message
  containing `tool_use`, and (b) not immediately after a `tool_use`-bearing
  assistant message (i.e. the message at the cut must not be a bare
  `tool_result` user message whose results were requested earlier).
  Walk candidate boundaries; if a candidate is unsafe, move it back to the
  previous safe boundary (never forward — keep more, never orphan less).
- New entry point: `compact_to_budget(messages, target_tokens, keep_recent_tokens)`
  — walks from the end accumulating recent messages up to
  `keep_recent_tokens` (extends the current `keep=8` count rule), then picks
  the largest safe cut ≤ that point; everything between pin (first 2 messages,
  same as today) and the cut is summarized. Returns `None` if no safe compaction
  can free tokens (caller skips compaction rather than corrupting history).
- Keep the existing `compact(messages, keep=8)` for compatibility (it now
  delegates through the safe-cut logic with the old pin/keep semantics).
- Summary block: keep `<compacted-history>` format; add a
  `<context-note>` line stating approximate token size freed. Previous-summary
  nesting stays as-is.

### 4. Loop integration — `agent_code/agent.py`

- Create one `ContextBudget` per `run_agent` call, passed in via a new optional
  param `budget: ContextBudget | None = None` (constructed by callers from
  `resolve_window`; `None` → build the default from `state`).
- Per iteration, replace the count check:
  1. After `response` completes: `budget.observe(response.usage)` and add
     `response` output estimate.
  2. Before sending the next request: if `budget.should_compact()`, call
     `compact_to_budget(messages, budget.target_tokens(), ...)`. If it returns
     `None`, emit a warning line and continue (never crash the loop).
  3. Emit a wire event (see §5) instead of `Line` for compaction, e.g.
     `"context compacted: 94% → 51% (11 messages summarized)"`.
- The max_tokens continuation / empty-retry paths append messages too; the
  estimate covers them because the check runs before each request.

### 5. Wire event — `agent_code/wire.py`

- New event `ContextCompacted(before_ratio: float, after_ratio: float,
  summarized_messages: int)`. Frontends (`cli.py` one-shot sink,
  `ui/events.py`) render one dim line; unknown-event handling stays
  lenient (ignore unknown types, as today).

### 6. `/context` slash command — `agent_code/slash.py` + `ui/shell.py`

- `/context` prints: model, window size, current estimate, ratio bar, last
  compaction stats. Requires exposing the live `ContextBudget` on
  `RuntimeState` (`state.context_budget` set by callers before/at run start) —
  the REPL owns `state`, so it can render between turns. In one-shot mode the
  command reports the static config (window, threshold).

### 7. CLI surface — `agent_code/cli.py`

- `--context-window <int>` option (0 = provider default) forwarded into
  `resolve_window`. Both one-shot and REPL paths.

## Files touched

| File | Change |
| --- | --- |
| `agent_code/context.py` | **new** — `TokenUsage`, `ContextBudget`, `resolve_window`, `estimate_tokens` |
| `agent_code/model.py` | `ModelResponse.usage`, parse usage in `_to_model_response`, MockProvider usage |
| `agent_code/compact_basic.py` | safe-cut helpers, `compact_to_budget`, keep `compact()` as wrapper |
| `agent_code/agent.py` | budget wiring, threshold trigger, emit `ContextCompacted` |
| `agent_code/wire.py` | `ContextCompacted` event |
| `agent_code/runtime.py` | `context_budget` field on `RuntimeState` |
| `agent_code/slash.py`, `agent_code/ui/shell.py` | `/context` |
| `agent_code/cli.py` | `--context-window` |
| `agent_code/ui/events.py` + one-shot sink | render the new event |
| `tests/test_context.py` | **new** — budget math, window resolution, estimation |
| `tests/test_compact_basic.py` | **new** — safe-cut cases incl. regression: orphaned tool_result/tool_use |
| `tests/test_agent_loop.py` (or extend existing agent tests) | threshold-triggered compaction with MockProvider usage |

## Invariants to preserve (hard requirements)

1. `tool_result` blocks must answer `tool_use` calls in order and never be
   separated from them by compaction. Verify with a test that builds a history
   where the greedy cut lands mid-pair.
2. Compaction must remain deterministic and LLM-free.
3. The loop must never crash or stall when usage is missing (all `None`-safe).
4. `just check` (lint + pyright + ty + tests) must pass; `ModelResponse.usage`
   and the new event must type-clean under both checkers.
5. Compaction happens **before** a request, never mid-tool-batch.

## Acceptance tests (executor must add; reviewer verifies)

- Budget: observe → ratio → should_compact flips at threshold; target math.
- `resolve_window` precedence: override > env > provider default.
- Safe cut: history `[..., assistant(tool_use a), user(tool_result a), user(msg)]`
  — any cut that would place boundary between the pair is moved back.
- Regression: current `compact()` on a tool-heavy history produces no orphaned
  `tool_use` (this fails on the current code — capture it first as a failing
  test if practical).
- Loop: MockProvider whose usage crosses 80% triggers exactly one compaction;
  trace contains the compaction line; `messages` after compaction keep the
  pinned first-2 + summary + recent shape.
- `/context` renders without a live budget (one-shot path).

## Executor / reviewer protocol (Rex)

- Two pi agent panes in this session: `executor` and `reviewer`.
- Executor implements the plan file `docs/plan-context-compaction.md` (this
  file), running `just check` until green.
- Reviewer re-reads the plan, reviews `git diff`, checks every invariant and
  acceptance test, and either approves or returns concrete findings to the
  executor. Iterate until the reviewer approves.
- Both agents work in this repo; reviewer must not edit code.
