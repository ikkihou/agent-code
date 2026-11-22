# Review verdict — token-ratio context compaction (final, round 3)

**Verdict: APPROVED**

Independently verified on the current tree: `just check` green (ruff clean, pyright
0 errors, ty clean, **202 tests passed**), `just fmt-check` clean (63 files formatted).
I re-ran the plan's acceptance criteria, my own adversarial probes, and the two
reproduction scenarios that drove rounds 1 and 2.

## Review history

| Round | Verdict | Findings |
| --- | --- | --- |
| 1 | CHANGES_REQUESTED | (1) estimate never seeded → no compaction before the first request (regression vs `len(messages) > 40`); (2) `test_loop_without_usage_keeps_running_and_never_compacts` passed vacuously; (3) stale `CLAUDE.md` compaction bullet |
| 2 | CHANGES_REQUESTED | seed was guarded by `observed`, so a REPL budget stayed stale while every later turn reloaded the uncompacted `session.history` → turn 2's first request at 303% of the window |
| 3 | **APPROVED** | `ContextBudget.reset()` (assignment) called unconditionally at the start of every run; `observed`/`seed` removed; per-turn recompaction test added |

Both regressions were fixed by replacing the one-shot max-floor seed with an
unconditional assignment reset of the estimate from the assembled message list — which is
exactly the list the next request carries.

## Evidence for the hard invariants

1. **`tool_use` / `tool_result` pairing is never split.** Read `is_safe_boundary` /
   `_safe_start` / `_safe_cut_at_or_before`: one predicate ("no `tool_use` in the prefix
   is still waiting for its `tool_result`") covers both failure modes, and cuts only move
   backwards. My own probe over five hand-built histories (serial pairs, parallel
   multi-`tool_use` batch, a pin-splitting pair, tiny-token-budget variants) found **zero**
   orphans in `compact()` and `compact_to_budget()` output. The suite covers the same
   ground plus the regression that the old count-based cut really did orphan a
   `tool_result`.
2. **Deterministic, LLM-free.** No model or network call is involved in a compaction;
   `_summarize` is pure string assembly; previous summaries nest under `<previous-summary>`.
3. **`None`-safety.** `observe(None)` keeps the estimate; `_to_token_usage` tolerates a
   missing `usage` and missing fields; the loop accumulates chars/4 estimates for
   usage-less providers (`test_loop_without_usage_accumulates_heuristic_estimates`,
   which now actually proves growth 1000 → >1000 across requests).
4. **`just check` type-clean under both checkers** — pyright 0 errors, ty clean; the new
   `TokenUsage | None` and `ContextCompacted` union member needed no ignores.
5. **Compaction only between a finished batch and the next request** — the check sits at
   the top of the loop, after the previous `tool_result` batch was appended, never inside
   `execute_one_tool_call`.
6. **Window resolution / CLI surface / status.** `resolve_window` precedence
   (override > `CONTEXT_WINDOW_TOKENS` > provider default, invalid values ignored),
   `--context-window` on both paths, `/context` with a live budget (`[###---] 42%`,
   estimate, threshold, last compaction) and without one (static config).

## Behavioural checks I ran myself (not from the suite)

- Fresh/resumed long history (60 messages, window 2000): `ContextCompacted` reaches the
  wire **before** the provider's first call; the first request carries 799 estimated
  tokens instead of 6051 (302% of the window).
- REPL shape (one `Session` + one `RuntimeState`/`ContextBudget` across two `run_agent`
  calls, same history): turn 2's first request is now 808 tokens (40%) with
  `budget.compactions == 2`, versus 6060 tokens (303%) and no compaction before the fix.
- PEP 758 justification checked: ruff 0.16.0 with `--target-version py314` rewrites
  `except (A, B):` → `except A, B:`, so the parenthesized form really would fail
  `just fmt-check`. The style is correct as written.

## Non-blocking notes (carried forward, no action required)

- `observe()` intentionally replaces the estimate (documented; a max-floor would
  re-trigger compaction forever). A pathological partial report (`input_tokens=0,
  output_tokens=5`) would still shrink the estimate to 5 — low risk on this wire.
- A history that already contains an orphan `tool_result` — only producible by the old
  buggy `compact()` and persisted in an old session JSONL — is passed through untouched;
  `is_safe_boundary` guards new cuts only. A defensive "drop leading orphan
  `tool_result` in the suffix" would heal old sessions.
- When compaction is impossible the "context compaction skipped: no safe boundary" line
  repeats once per remaining iteration (bounded by `max_steps`); cosmetic.
- `compact(messages, keep=8)` is kept as a compatibility wrapper per the plan and is now
  unused by production code — fine, it documents the old semantics and is fully tested.
- `docs/plan-context-compaction.md` and `docs/executor-done.txt` are working artefacts of
  this exercise; neither belongs in the eventual commit (the plan file is the contract,
  the done file is the executor's report).

**APPROVED** — the implementation meets the plan's goals and all five hard invariants,
with acceptance tests that fail on the pre-fix code and pass now.
