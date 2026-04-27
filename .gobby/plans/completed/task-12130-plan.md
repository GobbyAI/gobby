# Gobby-side block observability + upstream ghook reason forwarding

## Overview

Block responses currently reach the CLI with empty reason fields, surfacing as bare `"Blocked by hook"` with no rule identification. Intentional blocks (e.g., native Claude memory-tool routing to gobby-memory MCP) look identical to buggy blocks (e.g., the handoff's `escalate_task` symptom on parent sessions after plan-adversary runs). Nobody can tell which class they're in without grepping source.

The legacy `hook_dispatcher.py` that originally produced this fallback was deleted in commit `5e9753e` on 2026-04-18 ([gobby-#11842] "refactor: remove legacy hook dispatcher fallback", 850 lines removed). The file at `build/lib/gobby/install/shared/hooks/hook_dispatcher.py` is stale packaged output.

Current live gobby-side fallbacks (grep `"Blocked by"` across `src/`):
- `src/gobby/workflows/engine/core.py:500` — `"Blocked by rule"`
- `src/gobby/hooks/dispatchers/webhook.py:44` — `"Blocked by webhook"`
- `src/gobby/servers/chat_session_permissions.py:222` — `"Blocked by session lifecycle"`
- `src/gobby/servers/websocket/chat/_lifecycle.py:266, 272` — `"Blocked by webhook"` (both lines)

None produce the literal `"Blocked by hook"` seen in the handoff. Adapters forward `response.reason` when present (`src/gobby/adapters/claude_code.py:221`, `src/gobby/adapters/codex_impl/adapter.py:1057`). So the bare string is either (a) an outer runtime fallback substituted after gobby emits an empty `reason`, or (b) the ghook Rust binary's own block path. The ghook binary is installed from crates.io as a Rust crate per `src/gobby/cli/install_setup.py:1033-1041`. **Hypothesis, not confirmed origin.**

Fix splits into two parallel surfaces:
1. **Gobby-side Python audit + structured logging** — every `HookResponse(decision="block", ...)` production site carries a non-empty `reason`; every block emission logs a structured `BLOCK ...` line.
2. **Upstream ghook issue/PR + adapter-side sentinel** — file upstream asking for reason forwarding; intercept empty-reason responses at the adapter boundary and substitute a loud sentinel so the ghook-silent-fallback case becomes observable.

## Constraints

- **Fallback strings stay as sentinels.** Do not remove the fallback strings post-fix — they are defensive. Make them loud (WARN log on hit) so a regression that silently emits them is observable.
- **Do NOT add tests to monolith files.** `tests/workflows/test_hooks.py` (1,569 LOC) — route new coverage into a new focused file.
- **Adapter-side sentinel preserves CLI contract.** The CLI expects a non-empty reason or falls back itself. Substituting a loud-sentinel string keeps the contract while making the missing-reason case diagnosable.
- **Upstream ghook is out-of-tree.** File an issue/PR against the ghook crate; do not block this task on upstream merging. The adapter-side sentinel is the in-repo mitigation.
- **Unblocks re-investigation task #12128.** That task can't classify the handoff's `escalate_task` symptom without this logging.

## Phase 1: Gobby-side audit + reason propagation

### 1.1 Trace and populate reason at each block emission site [category: code]

For each of the four fallback sites, trace upstream: what rule/step/context triggered the block? Ensure the specific identifier (rule name, step-enforcement tag like `[step-enforcement:{wf}/{step}]`, dispatcher id, session-lifecycle reason code) is passed through to `HookResponse.reason`. Post-fix, the fallback string fires only on a genuinely missing identifier — a bug condition.

Targets:
- `src/gobby/workflows/engine/core.py:500` — `block_reason = deferred_block.reason or "Blocked by rule"`. Trace why `deferred_block.reason` could be empty; populate from the rule being evaluated (rule name is known at this code path).
- `src/gobby/workflows/engine/enforcement.py:126-203` — step-enforcement already includes `[step-enforcement:{wf}/{step}]` in messages at lines 153-156, 187-189. Verify this reason is propagated all the way to `HookResponse.reason` (trace the return path).
- `src/gobby/hooks/dispatchers/webhook.py:44` — `return HookResponse(decision="block", reason=reason or "Blocked by webhook")`. Trace `reason` source; populate from the webhook's own identifier.
- `src/gobby/servers/chat_session_permissions.py:222` — `"Blocked by session lifecycle"` fallback. Trace `resp.get("reason")` source; populate from the lifecycle state that blocked.
- `src/gobby/servers/websocket/chat/_lifecycle.py:266, 272` — both `"Blocked by webhook"` fallbacks. Populate from the webhook state.

No validation test per individual site — the integration tests in 1.2 cover each path.

### 1.2 Structured BLOCK log at every emission site [category: code]

Targets:
- All five sites from 1.1: emit a single INFO log line on every block:
  ```python
  logger.info(
      f"BLOCK session={session_id} event={event_type} tool={tool_name} "
      f"source={source} rule={rule_or_step_name} reason={reason}"
  )
  ```
  Where `source` is one of `rule` / `step-enforcement` / `webhook` / `session-lifecycle` / `websocket-lifecycle`.

Consistent schema across all sites so grep/query works end-to-end.

Verification: `tests/hooks/test_block_observability.py`:
- Trigger a block via each known gobby-side path (rule-engine, step-enforcement, webhook, session-lifecycle, websocket chat-lifecycle). Assert the response carries a non-empty `reason` with the rule/path identifier.
- Assert log output contains matching `BLOCK ...` line with structured schema.
- Assert the fallback sentinel string is NOT the one returned under normal conditions (fallback is defensive only).
- Include a web-chat-specific test case exercising `_lifecycle.py:266` to ensure that fallback site is covered.

## Phase 2: Adapter-side sentinel for ghook-origin empty reasons

### 2.1 Intercept empty-reason block responses at adapter boundary [category: code]

Targets:
- `src/gobby/adapters/claude_code.py:221` — adapter-side forward path. If the upstream response has `decision=="block"` but `reason` is empty/whitespace, substitute:
  ```
  Blocked by hook (ghook fallback — no reason forwarded; file a bug at <ghook-repo-url>)
  ```
  and emit a WARN log line with the full response payload.
- `src/gobby/adapters/gemini.py` — mirror the same intercept.
- `src/gobby/adapters/codex_impl/adapter.py:1057` — mirror the same intercept.

After this, hitting the sentinel string downstream is a clear signal that either (a) ghook upstream produced the empty reason, or (b) a gobby-side site from Phase 1 regressed. Either is a bug; both are observable.

Verification: `tests/hooks/test_adapter_empty_reason_sentinel.py`:
- Feed each adapter a simulated upstream response with `decision="block"` and empty `reason`. Assert the returned response contains the sentinel string and a WARN log fires.
- Feed each adapter a response with a populated `reason`. Assert the adapter passes it through untouched (no sentinel substitution).

## Phase 3: Upstream ghook issue

### 3.1 File issue/PR against ghook crate [category: docs]

Targets:
- External: ghook repository (identify URL; crate is on crates.io per `src/gobby/cli/install_setup.py:1033-1041` which queries `https://crates.io/api/v1/crates/ghook`).

Content to file:
- Summary: ghook's block-decision path should always forward the caller-provided reason verbatim; never substitute a bare `"Blocked by hook"` string when the caller supplied an empty reason.
- Concrete repro: reference the gobby-side examples surfaced by this task's Phase 1 logging.
- Recommendation: expose an environment variable or CLI flag to make the fallback loud (e.g., `GHOOK_LOUD_FALLBACK=1` prefixes with `"[ghook-fallback]"`).

No code change in this repo for Phase 3 beyond the adapter sentinel already in Phase 2. If upstream merges the fix, the adapter-side sentinel becomes a no-op in practice but remains as defensive code.

## Overall verification checklist

- [ ] `uv run pytest tests/hooks/test_block_observability.py tests/hooks/test_adapter_empty_reason_sentinel.py -v`
- [ ] Grep daemon logs after exercising gobby-originated blocks: all contain structured `BLOCK ...` line with rule/path identifier.
- [ ] Grep daemon logs for the sentinel substitution path: fires only when upstream provides empty reason (simulated).
- [ ] Post-fix, bare `"Blocked by hook"` never reaches the CLI — always either a specific reason or the loud sentinel.
- [ ] Upstream ghook issue URL recorded in the task close comment.
- [ ] `uv run ruff check src/ && uv run mypy src/` clean.

## Reference

Parent campaign plan: `~/.claude/plans/handoff-interactive-planning-for-twinkly-widget.md` Task 2 Workstream B.
Blocks: re-investigation task #12128 (can't classify the `escalate_task` symptom without this logging).
