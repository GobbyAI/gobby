# Smarter memory recall via backgrounded Haiku helper agent

## Overview

Replace score-only synchronous memory recall with an LLM-judgment-driven backgrounded Haiku helper agent. The helper runs in parallel with the existing fast vector recall on every `turn_start`, takes a holistic view of the parent's session digest + prompt, runs iterative `search_memories` calls, and either `send_message`s 0–3 selected memories back to the parent or finishes silently. Existing fast vector recall stays in place as the immediate baseline; the helper supplements it with smarter selections delivered at the parent's next `turn_start` via the existing inter-session messaging rule (with its `is_spawned_agent` gate dropped so parents receive too). On every parent `turn_start`, three rules fire in priority order: cancel any in-flight helper from a prior turn (priority 5, via a new `cancel_stale_helpers` MCP tool); deliver pending P2P messages with cross-source dedup and a cancelled-session filter (priority 10); spawn a fresh helper for the new prompt (priority 12). The strict ordering guarantees that no stale helper output ever lands on an unrelated later prompt.

## Constraints

- The synchronous fast-recall path (`memory-recall-on-prompt`) and the rolling digest pipeline (`digest-on-response`) are out of scope — they continue unchanged. The helper consumes the digest produced at `turn_end` of the previous turn; it never produces it.
- Helper must run backgrounded. Adding LLM latency to `turn_start` is unacceptable.
- `PreToolUse` (`before_tool`) does not fire on text-only assistant turns in Claude Code, so delivery happens at the next `turn_start` only.
- **Dedup tracking is on the parent's delivery side, on the inline `inject_result` path inside `_apply_effect` (`src/gobby/workflows/engine/effects.py:57+`), NOT in `HookManager._evaluate_workflow_rules` (`src/gobby/hooks/hook_manager.py:597–698`).** The hook-manager dedup loop only runs on deferred `dispatch_result` items; the `inject_result: true` path is inline-dispatched directly inside `_apply_effect` and never produces a dispatch_result. Phase 2.4 implements the full delivery-time pipeline (normalize → drop-cancelled-from-session → dedup → strip handled messages → format) on the inline path so it actually fires for `deliver_pending_messages` results.
- **Helper is read-only on `injected_memory_ids`.** Helper reads it before selecting (to avoid re-surfacing already-seen memories) but never writes. Writing happens in 2.4 via `SessionVariableManager.append_to_set_variable` — the existing atomic primitive `_dedup_memory_results` uses. This eliminates the round-1 race where IDs got marked injected before the parent ever saw them and the read-modify-write loss between concurrent writers. The read-only contract is enforced at the runtime layer by 2.2, which makes agent `blocked_tools` override the default infrastructure-tool exempt so the helper's `mcp__gobby__set_variable` calls are actually blocked (the `is_infrastructure_tool` exempt path in `_check_agent_tool_enforcement` (`src/gobby/workflows/engine/enforcement.py`) currently returns before any block-list is consulted, which makes `blocked_mcp_tools` and a naive `blocked_tools` placement non-functional for proxy infra tools).
- **Same-turn cross-source dedup is owned by the inline `inject_result` path, keyed off the canonical platform session id.** Both fast-recall (`memory-recall-on-prompt`, priority 10) AND helper-delivery (`deliver-pending-messages`, priority 10 firing on the next turn) inject memories on `turn_start`. Without explicit handling, the same memory id can render twice in the same turn — once from fast recall's inline `search_memories` formatter, once from helper-delivery's `memory_recall` payload — because fast recall today does NOT consult or write `injected_memory_ids` on the inline path. Phase 2.4 places dedup-against-and-append-to `injected_memory_ids` inside the `_apply_effect` pipeline for BOTH `("gobby-memory", "search_memories")` AND `("gobby-agents", "deliver_pending_messages")`. **Both formatters MUST resolve the session id via `event.metadata.get('_platform_session_id')`**, NOT `event.session_id`. `HookEvent.session_id` is the CLI external id (Claude `external_id`, Codex `thread_id`); the canonical Gobby session row uses the platform session id, and `SessionVariableManager` is keyed by it. The existing deferred memory-dedup (`_dedup_memory_results` at `src/gobby/hooks/hook_manager.py:737`) reads `_platform_session_id` for exactly this reason. If the formatters used `event.session_id`, fast recall and helper delivery would write `injected_memory_ids` under the wrong key and same-turn/session dedup would silently fail. Both writers go through `SessionVariableManager.append_to_set_variable`, race-free across concurrent rule evaluations.
- **Freshness contract: two independent guards.** A backgrounded helper for prompt N can produce a `memory_recall` payload that lands in the inter-session message queue at one of three times relative to prompt N+1's `turn_start`: (i) before N+1's spawn fires → in-flight, gets cancelled; (ii) before N+1's deliver runs → message in queue from a `success` run, intended delivery; (iii) after N+1's deliver runs → message in queue from a `success` run, missed its window, will deliver at N+2 against an unrelated prompt — STALE. Cancellation alone catches (i) but does NOT catch (iii) because the source run's status is `success`, not `cancelled`. Two guards together close the hole: **(A) Cancellation guard.** A dedicated `cancel-stale-memory-recall-helpers` rule (3.2) at priority 5 invokes a new MCP tool `cancel_stale_helpers(parent_session_id, agent_name)` (added in 2.5). The cancel rule's effect uses `inject_result: true` purely as a sync marker so it is inline-awaited before priority-10 delivery (per `EffectsMixin._apply_effect`, only `inject_result: true` non-background effects are awaited inline; everything else is queued and dispatched after the workflow handler returns — too late). 2.4's delivery formatter has a dedicated `cancel_stale_helpers` formatter case that returns None so the cancel call injects no context. After cancellation, 2.4's delivery formatter drops queued messages whose `from_session` belongs to a cancelled run via `LocalAgentRunManager.get_cancelled_session_ids` (added in 2.3). **(B) Turn-sequence guard.** A monotonic per-parent session variable `parent_turn_seq` (seeded `0` at session_start in 1.3; incremented at every parent `turn_start` by a new priority-1 rule in 3.4) gives every turn a unique number. The 3.3 spawn rule's helper prompt includes the current `parent_turn_seq` value (the helper is spawned at this turn, so its intended-delivery turn is `parent_turn_seq + 1`). The helper instructions (1.4) require including `"origin_turn_seq": <int>` in every `memory_recall` payload. 2.4's delivery formatter drops payloads where `payload.origin_turn_seq != current_parent_turn_seq - 1` — i.e., not from the immediately previous turn. This catches the (iii) case: a `success`-status helper from turn N whose message lands at N+2 has `origin_turn_seq=N`, but `current_parent_turn_seq - 1 = N+1`, so the payload is dropped. **Both guards together** ensure: at every parent turn_start, the order is **increment turn-seq (1) → cancel stale (5) → deliver pending with both filters (10) → spawn fresh (12)**, and no helper output ever injects against an unrelated later prompt regardless of whether the prior helper was cancelled-mid-run or completed-too-late. Note: this design replaces the rejected round-1 design that added a `supersede: bool` to `spawn_agent`; supersede couldn't address the rule-priority race (delivery at 10 fired before the spawn rule at 12 had a chance to cancel) AND `spawn_agent`'s factory does not have access to the lifecycle/process-kill deps that proper cancellation requires.
- **Empty pending-message queues must be no-op injections.** Without explicit handling, `inject_result: true` would inject `{"success": true, "messages": [], "count": 0}` as visible JSON on every routine turn where no helper has anything to surface. Phase 2.4's pipeline includes an early empty-payload short-circuit so the inline path skips injection cleanly.
- **First-time deliveries must render each helper memory exactly once.** Without explicit handling, `inject_result` would dump the raw `messages[*].content` AND the normalized top-level `memories`, so a fresh memory would render twice on the first delivery. Phase 2.4's pipeline strips handled `memory_recall` messages out of the `messages` array before formatting, so rendered output contains the deduped helper memories once and any non-`memory_recall` messages still passing through.
- **The existing `deliver-pending-messages` rule needs an explicit `arguments: { target_session_id: "{{ event.metadata.get('_platform_session_id') }}" }` block.** The dispatcher does not auto-inject `target_session_id` (only `session_id`, which `deliver_pending_messages` does not accept). The current rule, with no `arguments`, would not actually invoke the tool successfully. The templated value MUST resolve via `event.metadata['_platform_session_id']` (the canonical Gobby session row id), NOT `event.session_id` (the CLI external id — Claude `external_id` / Codex `thread_id`). Phase 3.1 fixes this in the rule body. The same canonical-id rule applies to `parent_session_id` in 3.2 (`cancel-stale-memory-recall-helpers`) and 3.3 (`spawn-memory-recall-helper` — both the `arguments.parent_session_id` field AND the helper prompt's `Parent session:` line).
- **The helper sends with `from_session=<helper's own child session id>`.** `send_message`'s schema requires `from_session`. The helper does not know its child session id at prompt-construction time (the spawn rule cannot capture the spawn return value because `background: true`). Phase 2.1 makes `from_session` optional in `send_message` and defaults it from `SessionContext` (the proxy's session-context header) when omitted, so callers running through the proxy do not need to know their own session id explicitly. The helper's instructions then say "omit from_session — it auto-fills from your session context."
- The existing `deliver-pending-messages` rule is gated on `variables.get('is_spawned_agent')`, which excludes user-facing parents. The gate must be removed; the underlying tool is session-scoped, so removing it does not cross-contaminate sessions.
- Helper must be hard-bounded: `max_turns: 3`, `timeout: 60s`. `AgentLifecycleMonitor` enforces both. These values live in the helper YAML, not in user-tunable config — the only runtime configurable for this feature is the `enabled` master kill-switch.
- Helper's `prompt` must contain dynamic per-turn content (parent_session_id, the parent's user prompt). `spawn_agent` has no separate `inputs:` parameter — everything dynamic is composed into the `prompt` string. Static instructions live on the agent definition.
- One new MCP tool: `gobby-agents.cancel_stale_helpers` (added in 2.5; consumed by the new 3.2 cancel rule). No new prompt-template files. The helper itself uses existing `gobby-memory.search_memories`, `gobby-agents.send_message`, `gobby-sessions.get_session`, top-level `get_variable`. The new tool is internal-only — only the cancel rule calls it; users do not need to know about it.
- The runtime master kill-switch is `DaemonConfig.memory_recall_helper.enabled`. It must be readable from the spawn rule's `when:` clause via a session variable seeded at `session_start` from the daemon's loaded config (rules cannot read `DaemonConfig` directly — `_build_eval_context` at `src/gobby/workflows/engine/templating.py:36–105` exposes only `event`, `variables`, `tool_input`, `source`, `project`).

## Phase 1: Foundation

**Goal**: Establish the monolith-gate prerequisite for `_session_start.py`, add the helper agent's master-toggle config, thread it through `EventHandlers` so its `enabled` flag is seeded into every new session as a variable, and create the helper's YAML definition.

The expander compiles each `## Phase N` independently and prefixes every task and dependency id with the phase prefix; cross-phase dependency edges do NOT survive the compile. The monolith gate must therefore live in the SAME phase as the task that depends on it (1.3, the `_session_start.py` edit). That's why the gate is 1.1 here, not in a separate Phase 0.

### 1.1 `_session_start.py` monolith gate [category: manual] (external observable: #12919)

Target: a `manual` gate task created by the expander. Performs no code changes itself.

The gate task is the in-Phase-1 dependency edge that 1.3's task hard-blocks on, with explicit close conditions tied to the external #12919:

- The implementer (or the conductor in autonomous mode) must verify two conditions before closing this gate task:
  1. Task **#12919** ("Refactor _session_start.py below the 1,000-line project limit") is in `closed` status. **Normative check (works for both humans and autonomous agents)**: call `gobby-tasks.get_task(task_id="#12919")` via the MCP `call_tool` path and confirm `state.is_closed == true`. The MCP path is required because (a) the bare `gobby tasks` CLI is blocked for agent sessions by the `block-gobby-tasks-cli` hook, and (b) the available human CLI command `gobby tasks show #12919` returns a plain-text `Lifecycle: closed` line but has no `--json` option, so it is unsuitable for automated parsing. Humans may run `gobby tasks show #12919` for a quick visual sanity check, but the MCP path is what the gate-check should rely on.
  2. `wc -l src/gobby/hooks/event_handlers/_session_start.py` reports a value < 1000.
- If either condition is unmet, the gate stays open. 1.3's task is blocked. The implementer escalates to the human owner if #12919 is stalled.

This plan does NOT prescribe the extraction shape that #12919 chooses; that's #12919's call. This plan only owns the intra-Phase-1 dependency edge from 1.3 → 1.1 and the close-condition documentation on 1.1.

**Operational gate, not engine-enforced.** Round 8's adversary established that the current task expander does not deterministically parse markdown header annotations like `(depends: 1.1, 1.2)` into `tasks.dependencies` edges; whether a `depends_on` edge gets emitted depends on the LLM compiler's behavior, and `validate_applied_run` only checks task mappings, not plan-required edges. Asserting "the expander MUST abort if the edge is absent" is therefore not enforceable from current CLI/code.

Instead, this gate is enforced **operationally** by 1.3's own preconditions:

1. The task expander materializes 1.1 as a `category: manual` task with the close conditions above as `validation_criteria`. (If the expander's LLM compiler also emits a `1.3 depends_on 1.1` edge, that's a bonus belt-and-suspenders, but the design does not depend on it.)
2. **1.3's task body itself includes a hard precondition** (added in 1.3's prose) requiring the implementer to verify both close conditions BEFORE making any code changes: (a) `gobby-tasks.get_task(task_id="#12919")` via the MCP `call_tool` path returns `state.is_closed == true`, (b) `wc -l src/gobby/hooks/event_handlers/_session_start.py` reports < 1000. If either is unmet, the implementer must escalate the task with reason `"#12919 not yet closed; _session_start.py monolith gate (1.1) still open"` rather than starting the edit.
3. 1.1 stays open as a tracking task and is closed by hand (or by an automation watching #12919) when both conditions hold.

This is operational, not DB-enforced, but is enforceable by current tooling: implementers (human or autonomous) read task bodies before claiming, and the explicit "MUST verify before any code changes" precondition is the same shape as other gating criteria in this codebase.

Validation criteria: 1.1 task exists with `category: manual` and the documented `validation_criteria`. 1.3's task body contains the explicit pre-edit gate-check prose (verifiable by reading the rendered task description). If the LLM compiler does happen to emit a `1.3 depends_on 1.1` edge, `gobby-tasks.get_task(task_id="<1.3-task-ref>")` returns `1.1` in `dependencies.blocked_by` — but absence of that edge does NOT fail this validation; the operational gate in 1.3's body is the load-bearing mechanism.

### 1.2 Add `MemoryRecallHelperConfig` (single field) to `DaemonConfig` [category: code]

Target: `src/gobby/config/sessions.py` (config class) and `src/gobby/config/app.py` (`DaemonConfig` field).

Add a minimal `MemoryRecallHelperConfig` (Pydantic `BaseModel`, NOT extending `FeatureDefaultConfig`) with a single `enabled: bool` field, and attach it to `DaemonConfig` as a sibling of the existing `digest: DigestConfig` field at `src/gobby/config/app.py:288+`. The helper's model, timeouts, and search-tuning values are intentionally hardcoded in the helper agent YAML (1.4) — they are not user-tunable and adding orphan config fields would just be dead surface.

In `src/gobby/config/sessions.py`, add the class right after `DigestConfig` (which ends at line 151):

```python
class MemoryRecallHelperConfig(BaseModel):
    """Backgrounded Haiku memory-recall helper agent runtime toggle."""

    enabled: bool = Field(
        default=True,
        description="Enable the backgrounded LLM-driven memory recall helper agent.",
    )
```

`BaseModel` is the right base here; we are not exposing provider/model/tier overrides because the helper's runtime values are pinned in its YAML definition (1.4). If a future requirement exposes any of those for tuning, it can extend this class then.

Then in `src/gobby/config/app.py`, in the `DaemonConfig` class (around line 288, sub-config block), add immediately after the `digest: DigestConfig = Field(...)` declaration:

```python
    memory_recall_helper: MemoryRecallHelperConfig = Field(
        default_factory=MemoryRecallHelperConfig,
        description="Backgrounded Haiku memory-recall helper agent configuration",
    )
```

Add the import at the top of `src/gobby/config/app.py`:

```python
from gobby.config.sessions import (
    # ... existing imports ...
    MemoryRecallHelperConfig,
)
```

(Adjust to match the existing import style — likely the import line already exists for `DigestConfig`; just append `MemoryRecallHelperConfig`.)

Validation criteria: `MemoryRecallHelperConfig` exists in `src/gobby/config/sessions.py` extending Pydantic `BaseModel`, with exactly one field `enabled: bool` defaulting to `True`. `DaemonConfig.memory_recall_helper` field is present in `src/gobby/config/app.py` with `default_factory=MemoryRecallHelperConfig`. `DaemonConfig().memory_recall_helper.enabled` evaluates to `True`. Loading a YAML config containing `memory_recall_helper: {enabled: false}` deserializes to `False`. Loading with no `memory_recall_helper:` block leaves the field at its default. The class deliberately has no other fields — this is verified by an explicit test asserting the model's field set is exactly `{"enabled"}`.

### 1.3 Thread `memory_recall_helper` config to `EventHandlers` and seed `memory_recall_helper_enabled` on session_start [category: code] (depends: 1.1, 1.2)

**HARD PRECONDITION (gate-check; do BEFORE any code changes):**

This task adds ~15 lines to `src/gobby/hooks/event_handlers/_session_start.py`. HEAD has that file at 1,021 lines, over the project's 1,000-line monolith limit (principle #2). 1.1 is the manual gate task tracking the external refactor #12919. Before claiming or editing for 1.3:

1. Verify task #12919 is closed. **Normative check (humans and agents both)**: call `gobby-tasks.get_task(task_id="#12919")` via the MCP `call_tool` path; confirm `state.is_closed == true` in the response. The bare `gobby tasks` CLI is blocked for autonomous agents by the `block-gobby-tasks-cli` hook, and `gobby tasks show #12919` (the human CLI) has no `--json` option (its plain-text `Lifecycle: closed` line is fine for visual confirmation but not for automated parsing), so the MCP path is the only programmatic option valid for both audiences.
2. Run `wc -l src/gobby/hooks/event_handlers/_session_start.py` (allowed via Bash for both humans and autonomous agents) and confirm the value is `< 1000`.

If either condition is unmet, **DO NOT** start the edits. Escalate the task with reason `"#12919 not yet closed; _session_start.py monolith gate (1.1) still open"`. The LLM-driven expander may or may not emit a `1.3 depends_on 1.1` edge in `tasks.dependencies` (current expander behavior is non-deterministic per the round-8 adversary finding), so this precondition check is the load-bearing gate — the dependency edge, if present, is bonus belt-and-suspenders.

Targets:

- `src/gobby/hooks/event_handlers/_base.py` (`EventHandlersBase` — add typed slot for the config)
- `src/gobby/hooks/event_handlers/__init__.py` (`EventHandlers.__init__` — accept and store the config)
- `src/gobby/hooks/factory.py` (factory call site at line 232 — pass `config.memory_recall_helper`)
- `src/gobby/hooks/event_handlers/_session_start.py` (`SessionStartMixin._activate_default_agent` — write the seeded variable into `changes` before `sv_mgr.merge_variables`)

Mirror the existing `skills_config: SkillsConfig | None` pattern (`src/gobby/hooks/event_handlers/_base.py:34`, `__init__.py:110`, `factory.py:241`).

In `src/gobby/hooks/event_handlers/_base.py`, add the typed slot inside `class EventHandlersBase` alongside `_skills_config`:

```python
from gobby.config.sessions import MemoryRecallHelperConfig  # add to top-of-file imports

class EventHandlersBase:
    """Base class for EventHandlers mixins with type hints for shared state."""
    # ... existing slots ...
    _skills_config: SkillsConfig | None
    _memory_recall_helper_config: MemoryRecallHelperConfig | None
    # ... existing slots ...
```

In `src/gobby/hooks/event_handlers/__init__.py`, in `EventHandlers.__init__` (line 52–151), add a new keyword param and assignment paralleling `skills_config`:

```python
def __init__(
    self,
    # ... existing params ...
    skills_config: SkillsConfig | None = None,
    memory_recall_helper_config: MemoryRecallHelperConfig | None = None,
    # ... existing params ...
) -> None:
    # ...
    self._skills_config = skills_config
    self._memory_recall_helper_config = memory_recall_helper_config
    # ...
```

Add a docstring entry for the new param matching the surrounding style.

In `src/gobby/hooks/factory.py` at line 232 where `EventHandlers(...)` is constructed, add the keyword argument right after `skills_config`:

```python
event_handlers = EventHandlers(
    # ... existing args ...
    skills_config=config.skills if config else None,
    memory_recall_helper_config=config.memory_recall_helper if config else None,
    workflow_config=config.workflow if config else None,
    # ... existing args ...
)
```

In `src/gobby/hooks/event_handlers/_session_start.py`, in `_activate_default_agent` (lines 841–965), insert the two new `changes[...]` writes **BEFORE the existing-variable filter pass**. HEAD's flow is (verified at this file):

```
... <changes is built up> ...
existing = sv_mgr.get_variables(session_id)
if existing:
    _ALWAYS_REAPPLY = { ... }
    changes = {k: v for k, v in changes.items() if k in _ALWAYS_REAPPLY or k not in existing}
sv_mgr.merge_variables(session_id, changes)
```

Both new writes must land in the "changes is built up" region, BEFORE the `existing = sv_mgr.get_variables(session_id)` read. The filter then handles preservation correctly: `memory_recall_helper_enabled` is added to `_ALWAYS_REAPPLY` so it re-applies every session_start; `parent_turn_seq` is NOT added to `_ALWAYS_REAPPLY`, so the `k not in existing` clause preserves its existing value on compact/restart and only seeds it (to 0) on the first session_start when the key is absent. Inserting these writes AFTER the filter (e.g., right before `sv_mgr.merge_variables(...)`) would bypass the preservation logic and reset `parent_turn_seq` to 0 on every re-activation — breaking the freshness guard.

Concrete edit, placed before the `existing = sv_mgr.get_variables(session_id)` line (no `setdefault` — unconditional assignment is correct because the filter at line 88 owns preservation):

```python
# Seed runtime toggle for memory-recall-helper from DaemonConfig.
# Re-applied on every session_start so a config change at restart
# propagates to existing sessions on next session_start (because
# `memory_recall_helper_enabled` is in `_ALWAYS_REAPPLY` below).
helper_cfg = self._memory_recall_helper_config
changes["memory_recall_helper_enabled"] = (
    bool(helper_cfg.enabled) if helper_cfg is not None else True
)
# Seed the monotonic per-parent turn counter used by the freshness
# turn-sequence guard (see Constraints / freshness contract guard B).
# Incremented at every parent turn_start by the priority-1 rule in 3.4.
# Spawn rule (3.3) reads it; delivery formatter (2.4) compares
# payload.origin_turn_seq against (current value - 1). NOT in
# `_ALWAYS_REAPPLY`, so the existing-variable filter preserves the
# counter across compact/restart and only seeds 0 on first session_start.
changes["parent_turn_seq"] = 0
```

Add `"memory_recall_helper_enabled"` to the `_ALWAYS_REAPPLY` literal set defined inside `_activate_default_agent` (the set is built inside the `if existing:` block) so the value re-applies on compact/restart rather than being preserved as a stale truthy value. **Do NOT add `"parent_turn_seq"` to `_ALWAYS_REAPPLY`** — its preservation across compact/restart is what makes the freshness guard correct, and that preservation is provided by the `k not in existing` clause of the filter. The unconditional `changes["parent_turn_seq"] = 0` write above is fine: the filter drops it on subsequent activations because the key already exists in `existing`, and keeps it on the first activation because the key is absent.

```python
_ALWAYS_REAPPLY = {
    "_agent_type",
    "_active_rule_names",
    "_active_skill_names",
    "_skill_format",
    "_agent_blocked_tools",
    "_agent_blocked_mcp_tools",
    "is_spawned_agent",
    "memory_recall_helper_enabled",
    # `parent_turn_seq` is INTENTIONALLY OMITTED. The seed write above
    # is unconditional, but preservation is owned by the `k not in
    # existing` clause of the filter below: when `parent_turn_seq` IS
    # already in `existing` (i.e., a non-first activation), the filter
    # drops the seed write so `sv_mgr.merge_variables` does not clobber
    # the runtime-incremented counter. When `parent_turn_seq` is ABSENT
    # from `existing` (i.e., a first activation, or a session whose
    # variable row was created without this key), the filter keeps the
    # write so the counter is seeded to 0. Note: per HEAD comments at
    # this site, `existing` may be truthy even on a "fresh" session
    # because `get_variables()` merges definition defaults — the
    # relevant condition is "is `parent_turn_seq` in `existing`",
    # NOT "is `existing` empty".
}
```

Also add `"memory_recall_helper_enabled"` to the `internal_keys` set (~line 907) so `variables_count` continues to report only user-facing variables, not this internal flag.

Validation criteria: `EventHandlersBase._memory_recall_helper_config` exists with the `MemoryRecallHelperConfig | None` type. `EventHandlers(memory_recall_helper_config=...)` round-trips the value to `self._memory_recall_helper_config`. `factory.py` line 232+ passes `config.memory_recall_helper` when `config` is non-None and `None` otherwise. After daemon start with default config, every new session has `variables.get("memory_recall_helper_enabled") == True` AND `variables.get("parent_turn_seq") == 0`. Setting `memory_recall_helper.enabled: false` in the config YAML and restarting the daemon causes new sessions to have `variables.get("memory_recall_helper_enabled") == False`. Existing sessions on compact/restart re-apply `memory_recall_helper_enabled` (because it is in `_ALWAYS_REAPPLY`) but DO NOT reset `parent_turn_seq` (because it is NOT in `_ALWAYS_REAPPLY`, the `k not in existing` filter clause drops the seed write when the variable already exists). **Preservation test (required, not optional)**: a test that exercises the actual `_activate_default_agent` merge flow end-to-end — set `parent_turn_seq=42` on a session via `SessionVariableManager.merge_variables`, then trigger another `_activate_default_agent` call for that session, then read back via `SessionVariableManager.get_variables` and assert `parent_turn_seq == 42`. Use the same fixture as the existing `tests/hooks/test_event_handlers.py::test_activate_default_agent` (or whichever covers the merge path today — verify the suite name in HEAD before adding) so the test actually goes through the filter logic, not a stub. **Fresh-session test**: simulate a first activation where `existing` may already contain definition defaults but does NOT contain `parent_turn_seq`. Trigger `_activate_default_agent` and assert `parent_turn_seq == 0` after the call (the seed write reaches `merge_variables` because `"parent_turn_seq" not in existing`, regardless of whether `existing` is otherwise empty or contains defaults). The condition that matters is "key absent from `existing`", NOT "`existing` is empty" — the latter is rarely true at HEAD because `get_variables()` merges definition defaults. Both new tests must fail if `parent_turn_seq` is incorrectly added to `_ALWAYS_REAPPLY` (which would clobber preservation), and must fail if the seed write is moved AFTER the existing-variable filter (which would also clobber preservation).

### 1.4 Create `memory-recall-helper` agent definition [category: config]

Target: `src/gobby/install/shared/workflows/agents/memory-recall-helper.yaml` (new file).

This agent definition is consumed by the spawn rule in 3.3. It conforms to the `AgentDefinitionBody` schema (`src/gobby/workflows/definitions.py:303–408`) — specifically `name`, `description`, `enabled`, `surfaces`, `provider`, `model`, `timeout`, `max_turns`, `role`, `goal`, `instructions`, `blocked_tools`, `blocked_mcp_tools`. There is **no** `inputs:`, `system_prompt:`, or `allowed_tools:` block at the agent level — the schema does not accept them.

The helper does NOT mutate the parent's `injected_memory_ids` itself. It only reads it (to avoid re-surfacing already-seen memories). Tracking newly-surfaced IDs as injected is the parent-side delivery pipeline's job (2.4). The helper's read-only contract is enforced at the runtime layer by 2.2 (which makes the `blocked_tools` listing of `mcp__gobby__set_variable` actually take effect for the helper's session).

The helper omits `from_session` on `send_message` calls — Phase 2.1 makes the tool default it from SessionContext (which the MCP proxy populates from the helper's session header automatically), so the helper does not need to look up its own child session id.

File contents (write verbatim):

```yaml
name: memory-recall-helper
description: |
  Backgrounded Haiku helper that decides what (if anything) from semantic
  memory should be surfaced for the parent's current turn. Reads the parent's
  digest and prompt, runs iterative searches, self-filters against the
  parent's injected_memory_ids, and either send_messages selected memories
  or finishes silently. Does NOT write to injected_memory_ids — that is
  done on the parent side at delivery time.
enabled: true
surfaces: [spawn]

provider: claude
model: claude-haiku-4-5
timeout: 60
max_turns: 3

role: |
  You are a memory-recall helper. You decide whether anything from semantic
  memory should be surfaced to the parent agent for the current turn.

goal: |
  Surface 0–3 memories that are clearly, directly relevant to the parent's
  current prompt. A turn with no surfaced memories is a valid outcome — do
  not pad.

instructions: |
  You will be given: the parent session ID, the parent's user prompt for
  the current turn, AND an integer `origin_turn_seq` (the parent's
  monotonic turn counter at spawn time). Follow this process within your
  3-turn budget.

  1. Fetch the parent's session digest:
       gobby-sessions.get_session(session_id=<parent_session_id>)
     Read the `digest_markdown` field. It may be empty for fresh sessions —
     that's fine.

  2. Read the parent's already-injected memory IDs:
       get_variable(name="injected_memory_ids", session_id=<parent_session_id>)
     Treat the returned list as memories the parent has already seen this
     session. NEVER re-surface any of these IDs. (Do NOT write to this
     variable yourself — the parent's delivery flow updates it when it
     actually injects your payload.)

  3. Form a focused 2–10 word semantic query from the prompt + digest.
     Strip conversational filler ("hey by the way, can you also…"). Call:
       gobby-memory.search_memories(query=<refined>, limit=8, min_score=0.5)

  4. If results are weak or off-topic, run ONE more search with a different
     angle. Do not loop further.

  5. From all candidates seen, select 0–3 memories that are clearly,
     directly relevant. Skip any whose ID is in the already-injected set.
     Prefer fewer; quality over quantity. NEVER surface a memory just
     because it scored above threshold.

  6. If you have selections:
       gobby-agents.send_message with:
         to_session = <parent_session_id>
         content    = JSON-encoded string with this exact shape:
                      {"type": "memory_recall",
                       "origin_turn_seq": <integer from your prompt>,
                       "memories": [<full memory records>],
                       "rationale": "<one short sentence>"}
     OMIT the `from_session` argument — the proxy auto-fills it from your
     session context (this is the runtime change made in 2.1). Each memory
     record MUST include `id` so the parent's delivery-side dedup can track
     it. The literal string "memory_recall" in the `type` field is what
     2.4's normalization pipeline keys off; do not use a different value.
     The `origin_turn_seq` field MUST be the integer you were given in the
     spawn prompt — copy it verbatim. The parent's delivery formatter
     uses it to drop payloads that miss their delivery window (see
     Constraints / freshness contract guard B).

  7. If nothing is clearly relevant, finish your turn without calling
     send_message. Do not pad.

  Hard constraints:
  - Cap: 3 surfaced memories per turn.
  - Never surface generic memories ("user prefers X" with no tie to current
    topic).
  - Never re-surface an ID already in injected_memory_ids.
  - Never write to injected_memory_ids yourself. Read-only.
  - ALWAYS include `origin_turn_seq` in your `memory_recall` payload as
    the integer you were given in the spawn prompt. Omitting it or using
    a different value will cause the parent's delivery formatter to drop
    your payload as stale.
  - You have at most 3 turns. Spend them on judgment, not exhaustive search.

# Belt-and-suspenders: keep the helper out of file edits and tool zoos.
# Top-level blocked_tools is a denylist (no `allowed_tools` at agent level
# in AgentDefinitionBody — only `blocked_tools` and `blocked_mcp_tools`).
#
# mcp__gobby__set_variable is a top-level proxy tool (not a wrapped MCP tool),
# so it goes in blocked_tools, NOT blocked_mcp_tools. Phase 2.2's enforcement
# reorder makes this listing override the default infrastructure-tool exempt
# in _check_agent_tool_enforcement so the helper is actually denied at the
# tool-routing layer (without 2.2, blocked_tools would lose to the infra
# exempt and the helper could still call set_variable). The helper
# legitimately needs get_variable (read-only on injected_memory_ids), so
# only set_variable is blocked.
blocked_tools:
  - Read
  - Write
  - Edit
  - Bash
  - Grep
  - Glob
  - mcp__gobby__set_variable

blocked_mcp_tools: []
```

This file is synced to `workflow_definitions` in the DB on the next daemon startup as a bundled agent template — the sync entry point is `sync_bundled_agents` in `src/gobby/agents/sync.py` (NOT the workflow loader, which handles a different definition class). Existing coverage for the sync contract lives in `tests/agents/test_sync.py`; agent-definition resolution coverage lives in `tests/workflows/test_agent_resolver.py`. Both are the canonical extension points for validating the new template's load path.

Validation criteria: file exists at the listed path; the YAML parses against `AgentDefinitionBody` (`src/gobby/workflows/definitions.py:303–408`) without validation errors; `model` field is `claude-haiku-4-5`; `max_turns` is `3`; `timeout` is `60`; `blocked_tools` contains `"mcp__gobby__set_variable"` (NOT `blocked_mcp_tools` — that field is for inner `call_tool` wrapped invocations and does not apply to top-level proxy tools); `blocked_mcp_tools` is empty.

Definition-load verification: after daemon restart, run `gobby agents list` and confirm `memory-recall-helper` appears in the output (per the post-cleanup CLI: `gobby agents list/show` inspect agent **definitions**, `gobby agents runs list/show` inspect runs). Then run `gobby agents show memory-recall-helper --json` and assert the returned JSON's `model` field equals `"claude-haiku-4-5"`, `max_turns` equals `3`, `timeout` equals `60`, and `blocked_tools` includes `"mcp__gobby__set_variable"`. Equivalent direct-DB check (use either, prefer the CLI for human verification): query `workflow_definitions` for `name='memory-recall-helper' AND workflow_type='agent'` and confirm exactly one row with `enabled=1` and matching definition_json fields. Add a test case to `tests/agents/test_sync.py` asserting that after `sync_bundled_agents` runs against this template, the DB row exists with the documented fields. Add a test case to `tests/workflows/test_agent_resolver.py` asserting `resolve_agent("memory-recall-helper", db)` returns a non-None body whose `model`, `max_turns`, `timeout`, and `blocked_tools` match the documented values.

The helper's `instructions` block contains explicit "OMIT the `from_session` argument", "Do NOT write to injected_memory_ids", "ALWAYS include `origin_turn_seq`", and the literal strings `"memory_recall"` and `"origin_turn_seq"` in the documented JSON content shape. A spawned helper that attempts to call top-level `mcp__gobby__set_variable` is blocked at the tool-routing layer with a `[agent-enforcement:memory-recall-helper]` reason (made functional by 2.2's enforcement reorder — verify by integration test in `tests/workflows/test_step_enforcement.py` that spawns a helper-equivalent agent definition with `mcp__gobby__set_variable` in `blocked_tools` and asserts the call is blocked).

## Phase 2: Runtime correctness fixes (pre-wiring)

**Goal**: Five targeted runtime changes that make Phase 3 wiring correct out of the box: (2.1) auto-fill `from_session` on `send_message` so the helper does not need its own child session id; (2.2) reorder `_check_agent_tool_enforcement` so explicit `blocked_tools` listings override the infrastructure-tool exempt and the helper's read-only contract is actually enforced; (2.3) add `LocalAgentRunManager.get_cancelled_session_ids` storage helper for the delivery filter to reference; (2.4) implement helper-aware delivery + same-turn cross-source dedup on `_apply_effect`'s inline `inject_result` path — applies BOTH freshness guards (cancelled-session AND `origin_turn_seq` matches `parent_turn_seq - 1`), dedupes against `injected_memory_ids` keyed by `_platform_session_id`, includes a no-op formatter case for `cancel_stale_helpers` that returns None (so the cancel rule's `inject_result: true` sync marker injects no context), and renders helper memory payloads exactly once; (2.5) add a `cancel_stale_helpers` MCP tool sharing `stop_agent`'s lifecycle path via an extracted `_stop_run` helper so the priority-5 cancel rule has a correctly-wired cancellation primitive that performs the full process-kill + lifecycle-monitor + terminal-cleanup chain. All five come BEFORE the wiring (Phase 3) so the wiring works correctly the first time the helper actually runs end-to-end.

**Phase 2 entry criteria (operational, not DB-enforced):** Phase 2 has no hard cross-phase dependency on Phase 1's CODE changes — it touches different files (`mcp_proxy/tools/agent_messaging.py`, `workflows/engine/enforcement.py`, `storage/agents.py`, `workflows/engine/effects.py`, `mcp_proxy/tools/agents.py`). Phase 2 tasks may be claimed and worked in parallel with Phase 1's later sections. The cross-phase `(depends: ...)` annotations seen in earlier rounds of this plan have been removed — the current task expander does NOT deterministically emit cross-phase `tasks.dependencies` edges from header annotations (round-8 adversary finding), so they were misleading rather than load-bearing. Coordination between Phase 1 and Phase 2 happens at PR-merge time and via the conductor's task ordering, not via DB dependency edges. The implementer is responsible for not merging Phase 3 until both Phase 1 and Phase 2 are complete (see Phase 3 entry criteria).

**Intra-phase dependencies inside Phase 2** (which the expander DOES handle reliably for same-phase deps): 2.4 depends on 2.1 (uses 2.1's `from_session` auto-fill semantics) and on 2.3 (uses the storage helper). 2.5 depends on 2.3. These same-phase deps are encoded in the section headers below.

### 2.1 Default `from_session` on `send_message` from SessionContext when omitted [category: code]

Target: `src/gobby/mcp_proxy/tools/agent_messaging.py`, the `send_message` function (around line 88–157) and its registered MCP schema.

Today `send_message` requires `from_session: str` (`get_tool_schema(gobby-agents, send_message)` confirms `required: ["from_session", "to_session", "content"]`). For a spawned helper, the helper does not know its own child session id at prompt-construction time (the spawn rule cannot capture `child_session_id` from the spawn return value because the spawn is `background: true`). We have two options: (a) ask the helper to look itself up at runtime, (b) make `from_session` optional at the tool boundary and default it from the proxy's `SessionContext` (which the MCP proxy already populates from the calling session's `X-Gobby-Session-Id` header — see the `mcp__gobby__call_tool` docstring: "Propagated to the daemon via X-Gobby-Session-Id header so tools can read it from the SessionContext ContextVar").

Choose (b) — it generalizes to any future caller running through the proxy and matches the existing pattern used by other gobby MCP tools.

Concrete change to `send_message`:

```python
# In src/gobby/mcp_proxy/tools/agent_messaging.py, send_message function:
async def send_message(
    to_session: str,
    content: str,
    from_session: str | None = None,  # was: from_session: str (required)
    priority: str = "normal",
) -> dict[str, Any]:
    # Resolve from_session: explicit argument > SessionContext > error.
    if from_session is None:
        from gobby.utils.session_context import get_current_session_id

        ctx_session_id = get_current_session_id()
        if not ctx_session_id:
            return {
                "success": False,
                "error": "from_session is required and no SessionContext session_id is available",
            }
        from_session = ctx_session_id

    # ... rest of function unchanged: resolve, validate same project, write inter-session message,
    #     auto-write to agent_runs.result if to_session is parent ...
```

Update the MCP schema declaration so `from_session` is no longer in the required list. The exact location of the schema declaration is wherever this tool is registered — grep for `"send_message"` in `src/gobby/mcp_proxy/tools/agent_messaging.py` and adjust the registration's `required` list. Document the auto-fill behavior in the tool's docstring (which becomes its description in the MCP schema): "from_session defaults to the calling session's id from SessionContext when omitted."

The HEAD-correct accessor is `gobby.utils.session_context.get_current_session_id` (`src/gobby/utils/session_context.py:61` — returns the calling session's UUID from a ContextVar populated by the proxy from the `X-Gobby-Session-Id` header, or `None` if no session context is set). Do NOT use `gobby.mcp_proxy.session_context.SessionContext.get_session_id()` — that module/accessor does not exist on HEAD (round-13 F2 finding). Other MCP tools in `src/gobby/mcp_proxy/tools/` already use `get_current_session_id` directly; mirror that pattern.

Do NOT relax cross-session validation: the function still validates that `from_session` and `to_session` are in the same project after defaulting. The default just resolves the unknown, it does not bypass authorization.

Validation criteria: calling `send_message(to_session="<peer>", content="hi")` from within a session context (e.g. through the proxy with `X-Gobby-Session-Id` set) succeeds with `from_session` resolved to the calling session's id, verifiable in `agent_runs.result` / inter-session message DB row. Calling `send_message(to_session="<peer>", content="hi")` from outside any session context returns `{"success": False, "error": "from_session is required and no SessionContext session_id is available"}` rather than crashing. Existing callers that pass `from_session` explicitly (e.g. `tests/e2e/test_inter_agent_messages.py`) continue to work unchanged. The MCP tool schema fetched via `get_tool_schema("gobby-agents", "send_message")` no longer lists `from_session` in `required`. Adding new test cases to `tests/mcp_proxy/tools/test_agent_messaging.py` covers both the default-fill path and the no-context error path.

### 2.2 Reorder `_check_agent_tool_enforcement` so `blocked_tools` overrides the infrastructure-tool exempt [category: code]

Target: `src/gobby/workflows/engine/enforcement.py`, `EnforcementMixin._check_agent_tool_enforcement` method.

Current ordering (verified by reading the source — adversary round-1 finding F1):

```python
def _check_agent_tool_enforcement(self, event, session_id, variables):
    blocked_tools = variables.get("_agent_blocked_tools") or []
    blocked_mcp_tools = variables.get("_agent_blocked_mcp_tools") or []
    if not blocked_tools and not blocked_mcp_tools:
        return None

    tool_name = event.data.get("tool_name", "")
    agent_type = variables.get("_agent_type", "unknown")

    # Discovery/infrastructure tools always pass    <-- runs BEFORE block-list
    if tool_name.startswith("mcp__gobby__"):
        mcp_suffix = tool_name[len("mcp__gobby__"):]
        if is_discovery_tool(mcp_suffix) or is_infrastructure_tool(mcp_suffix):
            return None

    # Check native tool block-list
    if blocked_tools and tool_name in blocked_tools:
        return HookResponse(decision="block", ...)

    # ... blocked_mcp_tools only matches inside call_tool wrappers ...
```

So `mcp__gobby__set_variable` is exempt before block-list consultation, and `blocked_mcp_tools` doesn't apply to top-level proxy tools (it only matches `gobby-server:tool` pairs inside `call_tool`/`mcp__gobby__call_tool`). Result: a naive helper config can't actually block `mcp__gobby__set_variable`, and the helper's read-only-on-`injected_memory_ids` contract is non-functional at the runtime layer.

Fix: reorder so explicit `blocked_tools` listings take precedence. Discovery/infra tools still pass by default — only when an agent explicitly opts in by naming a proxy tool in `blocked_tools` does the block-list take effect. New ordering:

```python
def _check_agent_tool_enforcement(self, event, session_id, variables):
    blocked_tools = variables.get("_agent_blocked_tools") or []
    blocked_mcp_tools = variables.get("_agent_blocked_mcp_tools") or []
    if not blocked_tools and not blocked_mcp_tools:
        return None

    tool_name = event.data.get("tool_name", "")
    agent_type = variables.get("_agent_type", "unknown")

    # Explicit native tool block-list runs FIRST so agents can deny
    # specific infrastructure/proxy tools (e.g. mcp__gobby__set_variable).
    # Default infra-exempt only applies when the tool is NOT explicitly listed.
    if blocked_tools and tool_name in blocked_tools:
        return HookResponse(
            decision="block",
            reason=(
                f"Rule enforced by Gobby: [agent-enforcement:{agent_type}]\n"
                f"Tool '{tool_name}' is blocked for the '{agent_type}' agent."
            ),
        )

    # Discovery/infrastructure tools pass by default (no explicit block above).
    if tool_name.startswith("mcp__gobby__"):
        mcp_suffix = tool_name[len("mcp__gobby__"):]
        if is_discovery_tool(mcp_suffix) or is_infrastructure_tool(mcp_suffix):
            return None

    # MCP tool restrictions (call_tool wrapper) — UNCHANGED below this line.
    if blocked_mcp_tools and tool_name in (
        "call_tool",
        "mcp__gobby__call_tool",
        "mcp_gobby_call_tool",
    ):
        # ... existing logic unchanged ...

    return None
```

This is a single-block reorder. Default behavior unchanged for any existing agent (none currently lists infra tool names in `blocked_tools`). The only opt-in is explicit listing.

Existing tests in `tests/workflows/test_step_enforcement.py` that assert `mcp__gobby__set_variable` always passes need to be reviewed and updated:

- Tests that set up an agent WITHOUT `mcp__gobby__set_variable` in `blocked_tools` → MUST continue to pass (call is permitted by infra exempt).
- Add a NEW test that sets up an agent WITH `mcp__gobby__set_variable` in `blocked_tools` → MUST be blocked with the documented reason. This is the new contract.
- Same pattern for `mcp__gobby__get_variable` and any other infra tools — explicit block overrides exempt.

Audit: search for tests asserting "infrastructure tools always pass" without an agent setup; these likely use empty `blocked_tools` and continue to work. Run `tests/workflows/test_step_enforcement.py` after the change and resolve any regressions.

Validation criteria: in `_check_agent_tool_enforcement`, the explicit-block check appears before the infrastructure-exempt check (verifiable by reading the new method body). With `_agent_blocked_tools = ["mcp__gobby__set_variable"]` set on a session and a `set_variable` call attempted, `_check_agent_tool_enforcement` returns a `HookResponse(decision="block", reason="...[agent-enforcement:<agent>]...Tool 'mcp__gobby__set_variable' is blocked...")`. With `_agent_blocked_tools = []` and same call, returns `None` (passes via infra exempt). All existing passing tests in `tests/workflows/test_step_enforcement.py` continue to pass. New test cases cover both directions explicitly. `mcp__gobby__get_variable` is NOT blocked for the helper (helper's `blocked_tools` does not include it), so the helper can still read `injected_memory_ids`.

### 2.3 Add `LocalAgentRunManager.get_cancelled_session_ids` storage helper [category: code]

Target: `src/gobby/storage/agents.py` — `LocalAgentRunManager` class.

Read-only helper used by 2.4's delivery formatter to identify queued P2P messages whose `from_session` belongs to an agent run that has been cancelled (e.g., by 2.5's `cancel_stale_helpers` tool). Recency-bounded so the query stays fast even with many historical cancellations.

```python
def get_cancelled_session_ids(
    self,
    since_hours: int = 24,
    agent_name: str | None = None,
) -> set[str]:
    """Return child_session_ids of agent_runs cancelled within the last `since_hours`.

    Used by the inline delivery formatter to drop queued memory_recall
    payloads from cancelled (superseded) helper runs whose send_message
    landed in the inter-session message queue before/during the
    cancellation. The recency window is wider than typical helper turn
    budgets (60s) so even slow delivery races resolve correctly; older
    rows are pruned by routine cleanup so the window does not bloat.

    Args:
        since_hours: Recency window (default 24h).
        agent_name: When set, restricts the result to runs of this agent.
            The delivery formatter passes "memory-recall-helper" so the
            cancelled-session drop is scoped narrowly: plain P2P messages
            from cancelled non-helper children must still be deliverable
            to their parent.
    """
    # Normalize the left side via datetime() so the comparison is
    # datetime semantics, not lexicographic. agent_runs.created_at is
    # stored as Python's `datetime.now(UTC).isoformat()` (e.g.
    # "2026-04-25T22:00:00+00:00"); SQLite's `datetime('now', ?)`
    # returns "YYYY-MM-DD HH:MM:SS" with a space separator. Without
    # the wrapping `datetime(created_at)` call, lexicographic
    # comparison treats the "T" in stored timestamps as greater than
    # the space in the cutoff, which can either include rows older
    # than the recency window or exclude rows that should match.
    sql = (
        "SELECT child_session_id FROM agent_runs "
        "WHERE status = 'cancelled' "
        "AND child_session_id IS NOT NULL "
        "AND datetime(created_at) > datetime('now', ?)"
    )
    params: list[Any] = [f"-{since_hours} hours"]
    if agent_name is not None:
        sql += " AND agent_name = ?"
        params.append(agent_name)
    with self.db.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {row["child_session_id"] for row in rows}
```

This is purely additive — no existing call sites touch `agent_runs` filtered by cancelled status with this signature, so there's no risk of regression. The class is `LocalAgentRunManager` (verified against HEAD); the prior plan's reference to `AgentRunStorage` was wrong.

Validation criteria: unit test in `tests/storage/test_agent_runs.py` (or the equivalent existing test file for `LocalAgentRunManager`) creates rows with mixed statuses (`success`, `running`, `cancelled` recent, `cancelled` old) and asserts `get_cancelled_session_ids(since_hours=24)` returns exactly the recent-cancelled set. Test with `since_hours=1` and a row cancelled 2h ago confirms recency window is honored. Test with no rows returns empty set without error. **Agent-name scoping test (required, not optional)**: with three cancelled-recent rows (`agent_name='memory-recall-helper'`, `agent_name='other-agent'`, `agent_name=NULL`), assert `get_cancelled_session_ids(agent_name='memory-recall-helper')` returns only the helper row's child_session_id; assert `get_cancelled_session_ids()` (no `agent_name`) returns all three. This test guards the F1 round-5 fix — without the scoping, the delivery formatter would silently discard cancelled non-helper children's plain P2P messages. **Datetime-normalization test (required, not optional)**: insert two cancelled-recent rows with `created_at` set to ISO-8601 with `T` separator and `+00:00` offset (matching what `datetime.now(UTC).isoformat()` actually produces), one stamped 30 minutes ago and one stamped 90 minutes ago. Assert `get_cancelled_session_ids(since_hours=1)` returns only the 30-minutes-ago row. Without the SQL `datetime(created_at)` wrap, lexicographic string comparison would either include the 90-minutes-ago row (wrong) or exclude the 30-minutes-ago row (also wrong) depending on how the cutoff string formats — this test catches the F1 round-6 regression.

### 2.4 Helper-aware delivery + same-turn dedup on the inline `inject_result` path [category: code] (depends: 2.1, 2.3)

Target: `src/gobby/workflows/engine/effects.py`, inside `EffectsMixin._apply_effect` (around line 100–119 — the `effect.type == "mcp_call"` branch where `effect.inject_result and not effect.background and self._mcp_dispatcher` is true).

This is the path that BOTH the existing `memory-recall-on-prompt` rule (priority 10, calling `gobby-memory.search_memories`) AND 3.1's modified `deliver-pending-messages` rule (calling `gobby-agents.deliver_pending_messages`) invoke. Today fast recall renders raw `search_memories` results without consulting `injected_memory_ids`, so on the same `turn_start` where the helper-delivery path also tries to surface a memory id the fast path already rendered, the parent sees the same memory twice. 2.4 places the dedup-against-and-append-to `injected_memory_ids` filter inside this inline path so both writers share one source of truth.

**Critical**: both formatters MUST resolve the session id for `SessionVariableManager` calls via `event.metadata.get('_platform_session_id')`, NOT `event.session_id`. The latter is the CLI external id (Claude `external_id`, Codex `thread_id`); the former is the canonical Gobby session row id under which `injected_memory_ids` is stored. The existing `_dedup_memory_results` path (`src/gobby/hooks/hook_manager.py:737`) reads `_platform_session_id` for exactly this reason — verifiable at `hook_manager.py:478, 570, 632`. Mis-keying would silently break dedup across fast recall and helper delivery without any visible error. Both formatters in 2.4 use the same resolution helper.

The current inline path:

```python
if effect.inject_result and not effect.background and self._mcp_dispatcher:
    dr = await self._mcp_dispatcher(effect.server, effect.tool, rendered_args, event)
    success = isinstance(dr, dict) and dr.get("success", False)
    if success and dr.get("result"):
        from gobby.hooks.dispatchers.mcp import format_discovery_result
        formatted = format_discovery_result({"tool": effect.tool, "result": dr["result"]})
        if formatted:
            context_parts.append(formatted)
    elif not success:
        # ... error handling, abort sibling effects ...
        return False
    return True
```

That path appends `format_discovery_result(...)`'s output verbatim. With `deliver_pending_messages` and `inject_result: true`:
- Empty-queue results (`{"success": true, "messages": [], "count": 0}`) get formatted as visible JSON noise on every routine turn.
- Helper `memory_recall` payloads inside `messages[*].content` get JSON-dumped through the generic formatter — there is no `deliver_pending_messages`-specific formatter today.
- Dedup against `injected_memory_ids` never runs (the inline path returns before `HookManager._evaluate_workflow_rules` sees a `dispatch_result`).
- First-time deliveries render each memory twice: once embedded in the raw `messages[*].content`, once if any later code adds a top-level `memories` key.

Implement the full delivery-time pipeline inline, gated on the calling tool. Replace the success branch above with:

```python
if success and dr.get("result"):
    raw_result = dr["result"]
    formatted: str | None = None

    event_obj = ctx.get("event")
    platform_session_id = (
        event_obj.metadata.get("_platform_session_id")
        if event_obj is not None and event_obj.metadata
        else None
    )
    if (effect.server, effect.tool) == ("gobby-agents", "deliver_pending_messages"):
        # Helper-aware delivery: normalize, drop-cancelled, drop-stale-by-turn-seq,
        # dedup against injected_memory_ids, strip handled messages, format remaining.
        formatted = self._format_delivery_result(raw_result, platform_session_id, variables)
    elif (effect.server, effect.tool) == ("gobby-memory", "search_memories"):
        # Fast-recall: dedup against injected_memory_ids, atomic-append survivors,
        # then format remaining via existing search_memories formatter.
        formatted = self._format_search_memories_result(raw_result, platform_session_id, variables)
    elif (effect.server, effect.tool) == ("gobby-agents", "cancel_stale_helpers"):
        # Side-effect-only tool: returning None forces inline-await (because the
        # call is on the inject_result-true path) without injecting cancellation
        # output as visible context. The cancel rule (3.2) sets inject_result:true
        # solely to coerce inline awaiting — per `_apply_effect`, calls without
        # inject_result are queued in mcp_calls and dispatched only AFTER the
        # workflow handler returns, which would let priority-10 delivery run
        # before cancellation completes.
        formatted = None
    else:
        # Generic path: empty short-circuit, then existing formatter.
        if not _is_empty_inject_payload(raw_result):
            from gobby.hooks.dispatchers.mcp import format_discovery_result
            formatted = format_discovery_result({"tool": effect.tool, "result": raw_result})

    if formatted:
        context_parts.append(formatted)
```

Add the `_is_empty_inject_payload` helper (covers all `inject_result: true` consumers, not just `deliver_pending_messages`):

```python
def _is_empty_inject_payload(result: Any) -> bool:
    """Decide whether an mcp_call result represents 'nothing worth injecting.'

    Bookkeeping keys treated as non-content: 'success', 'count', 'response_time_ms'.
    """
    if not isinstance(result, dict):
        return result is None or not result
    if result.get("count") == 0:
        return True
    BOOKKEEPING = {"success", "count", "response_time_ms"}
    content_keys = {k for k in result.keys() if k not in BOOKKEEPING}
    if content_keys == {"messages"} and not result.get("messages"):
        return True
    if content_keys == {"memories"} and not result.get("memories"):
        return True
    return False
```

Audit existing `inject_result: true` consumers (grep `inject_result: true` in `src/gobby/install/shared/workflows/rules/`; `memory-recall-on-prompt.yaml` is one) and confirm none currently rely on empty-payload injection. If any test asserts an empty payload IS injected, update it explicitly and document.

Add `_format_delivery_result` as a method on `EffectsMixin` (so it has access to `self.db` for SessionVariableManager):

```python
def _format_delivery_result(
    self,
    result: dict[str, Any],
    platform_session_id: str | None,
    variables: dict[str, Any],
) -> str | None:
    """Inline delivery-time pipeline for deliver_pending_messages results.

    `platform_session_id` is the CANONICAL Gobby session id (from
    event.metadata['_platform_session_id']), NOT event.session_id. It is the
    key SessionVariableManager uses; mis-keying breaks cross-source dedup
    silently. Caller in `_apply_effect` resolves it once and passes it down.

    Steps:
      1. Empty short-circuit (count=0 or empty messages) → no injection.
      2. Drop messages whose `from_session` belongs to a cancelled run
         (read via LocalAgentRunManager.get_cancelled_session_ids — added
         in 2.3). This handles the supersede-mid-send_message race
         (freshness guard A).
      3. Partition surviving messages into helper memory_recall payloads
         and other messages.
      4. **Drop memory_recall payloads whose `origin_turn_seq` does not
         equal `current_parent_turn_seq - 1`** (freshness guard B). This
         handles the success-status-but-too-late race where a helper from
         turn N completed successfully but its message landed AFTER turn
         N+1's deliver ran, so it would otherwise inject at turn N+2.
         `current_parent_turn_seq` is read from the `variables` dict
         passed in (the priority-1 rule in 3.4 has already incremented
         it for this turn). Payloads missing `origin_turn_seq` entirely
         are dropped (defensive — a well-behaved helper always sets it
         per 1.4's instructions).
      5. Collect all surviving helper memories, dedupe by id within this
         delivery.
      6. Filter out memory ids already in the parent's injected_memory_ids
         (read via SessionVariableManager keyed by platform_session_id).
      7. If any newly-injected ids remain, atomic-append them via
         sv_mgr.append_to_set_variable(platform_session_id,
         "injected_memory_ids", new_ids). Race-free across writers.
      8. Format output:
         a. If newly-injected helper memories exist, render them via the
            existing search_memories-style memory formatter (reuse whatever
            format_discovery_result does for {"memories": [...]}).
         b. If non-memory_recall messages exist, render those via the
            generic formatter (format_discovery_result on a synthetic
            result {"messages": <other_messages>, "count": <len>}).
         c. Concatenate (a) + (b), separated by a blank line, or return None
            if both are empty.
    """
    import json
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.hooks.dispatchers.mcp import format_discovery_result

    if _is_empty_inject_payload(result):
        return None

    messages = result.get("messages") or []
    helper_memories: dict[str, dict[str, Any]] = {}  # id -> memory record
    other_messages: list[dict[str, Any]] = []

    # Freshness guard A: when we identify a memory_recall payload below,
    # check whether its from_session belongs to a cancelled
    # memory-recall-helper run and drop the payload if so. Scoped narrowly
    # to memory_recall payloads (NOT plain P2P messages from any cancelled
    # child) — after 3.1 removes the is_spawned_agent gate, this delivery
    # path also handles plain parent<->child messaging, and silently
    # discarding cancelled non-helper child messages would be data loss
    # outside this feature's contract.
    from gobby.storage.agents import LocalAgentRunManager
    helper_cancelled_sessions: set[str] = set()
    if messages:
        try:
            run_storage = LocalAgentRunManager(self.db)
            # Scope to memory-recall-helper runs only. The helper accepts
            # an agent_name filter (added in 2.3) so we don't sweep up
            # cancelled non-helper children — see 2.3 for the signature.
            helper_cancelled_sessions = run_storage.get_cancelled_session_ids(
                agent_name="memory-recall-helper",
            )
        except Exception as e:  # noqa: BLE001 — fail open
            logger.debug(f"Failed to load cancelled helper session ids: {e}")

    # Freshness guard B: payload's origin_turn_seq must equal current - 1.
    # Fails CLOSED for memory_recall payloads when the counter is missing
    # or non-int (the per-message check below logs and drops). Non-int /
    # absent counter is treated as "cannot verify freshness", and the
    # freshness contract says no unverified helper memory ever injects.
    current_turn_seq = variables.get("parent_turn_seq")
    if isinstance(current_turn_seq, int):
        expected_origin_turn_seq: int | None = current_turn_seq - 1
    else:
        expected_origin_turn_seq = None

    # Kill-switch belt-and-suspenders: when the helper feature is disabled,
    # drop all memory_recall payloads regardless of their origin_turn_seq.
    # Without this, a helper spawned while enabled could send a payload
    # during a disabled interval; even though 3.4's counter has advanced
    # in the meantime (3.4 is intentionally NOT toggle-gated for this
    # very reason), this catches any race where the queued payload would
    # have otherwise injected once the user re-enables the feature.
    helper_enabled = variables.get("memory_recall_helper_enabled", True)

    for msg in messages:
        if not isinstance(msg, dict):
            other_messages.append(msg)
            continue
        content = msg.get("content")
        parsed: Any = None
        if isinstance(content, dict):
            parsed = content
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                parsed = None
        if isinstance(parsed, dict) and parsed.get("type") == "memory_recall":
            # Kill-switch catch-all: feature disabled → drop all
            # memory_recall payloads (round 9 fix).
            if not helper_enabled:
                logger.debug(
                    "Dropping memory_recall: memory_recall_helper_enabled is False"
                )
                continue
            # Freshness guard A: drop memory_recall payloads from cancelled
            # helper runs. Scoped narrowly to memory_recall + helper runs
            # so plain P2P messages from cancelled non-helper children
            # still reach the parent.
            if msg.get("from_session") in helper_cancelled_sessions:
                logger.debug(
                    f"Dropping memory_recall from cancelled helper session "
                    f"{msg.get('from_session')!r}"
                )
                continue
            # Freshness guard B: drop memory_recall payloads whose
            # origin_turn_seq does not match the immediately previous turn.
            # Fail-CLOSED for memory_recall when counter is missing/non-int:
            # the freshness contract requires we never inject a payload we
            # cannot prove is fresh. Other (non-memory_recall) messages
            # are unaffected — they fall through to other_messages below
            # and render via the generic formatter.
            if expected_origin_turn_seq is None:
                logger.warning(
                    "Dropping memory_recall: parent_turn_seq missing or "
                    "non-int — cannot verify freshness"
                )
                continue
            payload_origin = parsed.get("origin_turn_seq")
            if not isinstance(payload_origin, int) or payload_origin != expected_origin_turn_seq:
                logger.debug(
                    f"Dropping stale memory_recall: origin={payload_origin!r} "
                    f"expected={expected_origin_turn_seq}"
                )
                continue
            for mem in parsed.get("memories") or []:
                mid = mem.get("id") if isinstance(mem, dict) else None
                if mid:
                    helper_memories[mid] = mem  # last-write-wins
        else:
            # Non-memory_recall messages (plain P2P, command results, etc.)
            # are NOT subject to the helper-cancellation or turn-seq
            # filters — they belong to the generic delivery contract.
            other_messages.append(msg)

    # Dedup against parent's injected_memory_ids; atomic append survivors.
    # Keyed off the canonical platform_session_id, NOT event.session_id.
    new_memories: list[dict[str, Any]] = []
    if helper_memories:
        sv_mgr = SessionVariableManager(self.db) if platform_session_id else None
        already: set[str] = set()
        if sv_mgr is not None:
            try:
                existing_vars = sv_mgr.get_variables(platform_session_id)
                already = set(existing_vars.get("injected_memory_ids", []) or [])
            except Exception as e:  # noqa: BLE001 — fail open on dedup-state read
                logger.debug(f"Failed to read injected_memory_ids for dedup: {e}")
                already = set()
        new_memories = [m for m in helper_memories.values() if m.get("id") not in already]
        new_ids = [m["id"] for m in new_memories if m.get("id")]
        if new_ids and sv_mgr is not None and platform_session_id:
            try:
                sv_mgr.append_to_set_variable(platform_session_id, "injected_memory_ids", new_ids)
            except Exception as e:  # noqa: BLE001 — fail open; injection still proceeds
                logger.debug(f"Failed to append injected_memory_ids: {e}")

    parts: list[str] = []
    if new_memories:
        mem_formatted = format_discovery_result(
            {"tool": "search_memories", "result": {"memories": new_memories}}
        )
        if mem_formatted:
            parts.append(mem_formatted)
    if other_messages:
        msg_formatted = format_discovery_result(
            {
                "tool": "deliver_pending_messages",
                "result": {"messages": other_messages, "count": len(other_messages)},
            }
        )
        if msg_formatted:
            parts.append(msg_formatted)

    return "\n\n".join(parts) if parts else None
```

Add the parallel `_format_search_memories_result` method on `EffectsMixin` so fast-recall (`memory-recall-on-prompt`) writes through the same dedup primitive helper-delivery uses. Without this, on the same `turn_start` where helper-delivery surfaces memory `m1`, the fast path can also surface `m1` because it never consults `injected_memory_ids`:

```python
def _format_search_memories_result(
    self,
    result: dict[str, Any],
    platform_session_id: str | None,
    variables: dict[str, Any],
) -> str | None:
    """Inline pipeline for search_memories results.

    `platform_session_id` is the canonical Gobby session id (from
    event.metadata['_platform_session_id']). Same critical contract as
    `_format_delivery_result` — using event.session_id would mis-key the
    variable store and silently break dedup.

    Steps:
      1. Empty short-circuit (no `memories`) → no injection.
      2. Filter result.memories down to ones whose id is NOT in the parent's
         injected_memory_ids (read via SessionVariableManager).
      3. If empty, return None (everything was already injected this session).
      4. Atomic-append survivor ids via append_to_set_variable.
      5. Format via existing search_memories formatter on the filtered set.
    """
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.hooks.dispatchers.mcp import format_discovery_result

    if not isinstance(result, dict):
        return None
    memories = result.get("memories") or []
    if not memories:
        return None

    sv_mgr = SessionVariableManager(self.db) if platform_session_id else None
    already: set[str] = set()
    if sv_mgr is not None:
        try:
            existing_vars = sv_mgr.get_variables(platform_session_id)
            already = set(existing_vars.get("injected_memory_ids", []) or [])
        except Exception as e:  # noqa: BLE001 — fail open
            logger.debug(f"Failed to read injected_memory_ids for fast-recall dedup: {e}")
            already = set()

    survivors = [m for m in memories if isinstance(m, dict) and m.get("id") and m["id"] not in already]
    if not survivors:
        return None

    new_ids = [m["id"] for m in survivors]
    if sv_mgr is not None and platform_session_id:
        try:
            sv_mgr.append_to_set_variable(platform_session_id, "injected_memory_ids", new_ids)
        except Exception as e:  # noqa: BLE001 — fail open; injection still proceeds
            logger.debug(f"Failed to append injected_memory_ids from fast-recall: {e}")

    return format_discovery_result(
        {"tool": "search_memories", "result": {"memories": survivors}}
    )
```

Notes for the implementer:

- The `format_discovery_result` synthetic dispatch with `tool="search_memories"` is a deliberate reuse of the existing memory formatter so helper-surfaced memories render identically to fast-recall memories (consistent UX, single audit format). If `format_discovery_result` does not have a `search_memories` formatter today, add a minimal one alongside this work — verify by reading `src/gobby/hooks/dispatchers/mcp.py`'s formatter dispatch.
- `SessionVariableManager.append_to_set_variable` is the same atomic primitive `_dedup_memory_results` (`src/gobby/hooks/hook_manager.py:737`) uses for the same variable. Race-free across concurrent writers because it goes through the same DB transaction path.
- Empty short-circuit comes first so the empty-queue noise case ALSO covers any future tool with `inject_result: true`. Helper-aware and search_memories-aware paths fire only on their specific `(server, tool)` match.
- Errors in dedup-state read or write are fail-open (log debug, proceed) — same posture as `_dedup_memory_results`.
- `HookManager._evaluate_workflow_rules`'s dedup loop is left untouched. We do NOT need it to fire on `deliver_pending_messages` results; 2.4 handles the full pipeline inline. The existing `_dedup_memory_results` continues to fire for `dispatch_result` items from non-`inject_result` paths (no functional change).

Validation criteria: unit tests in a new `tests/workflows/test_delivery_pipeline.py` cover both formatters. For `_format_delivery_result`: (1) empty result `{"messages": [], "count": 0}` → returns `None`, no `injected_memory_ids` mutation; (2) result with one `memory_recall` message containing memory `m1` and `injected_memory_ids` initially empty → returns formatted string containing `m1` rendered through the search_memories formatter, and `injected_memory_ids` after the call contains `["m1"]`; (3) result with `memory_recall` containing `m1` when `injected_memory_ids` already contains `m1` → returns `None` (or empty) and `injected_memory_ids` unchanged; (4) result with `memory_recall` (`m1`) AND a non-memory_recall plain text message → returned formatted string contains `m1` once AND the plain text message; (5) result with malformed message content (not JSON) → message falls through to "other_messages" and renders via generic formatter; (6) two concurrent calls to `append_to_set_variable` from different rule evaluations do not lose either's IDs (race test); (7) **freshness guard A scoped to helper memory_recall payloads: result with one `memory_recall` message whose `from_session` belongs to a `cancelled` `memory-recall-helper` run → the message is dropped, returns `None` or content without it, no `injected_memory_ids` mutation. (7-aux) **scope test: result with one `plain` (non-memory_recall) text message whose `from_session` belongs to a `cancelled` non-helper child run → the message is NOT dropped; it falls through to `other_messages` and renders via the generic formatter. This is the F1 round-5 guard — without it, dropping the `is_spawned_agent` gate on `deliver-pending-messages` would cause data loss for cancelled non-helper children.** (7b) **session-key correctness: with a `HookEvent` whose `event.session_id` (external id) is `"ext-X"` and `event.metadata['_platform_session_id']` is `"plat-Y"`, both formatters MUST read/write `injected_memory_ids` under session id `"plat-Y"` and NEVER under `"ext-X"`. Concrete assertion: after the formatter runs, `SessionVariableManager(...).get_variables("plat-Y")['injected_memory_ids']` includes the new ids and `SessionVariableManager(...).get_variables("ext-X")['injected_memory_ids']` is unchanged (or absent).**; (7c) **freshness guard B: with `variables['parent_turn_seq'] == 5`, a `memory_recall` payload with `origin_turn_seq=4` is accepted (matches current-1), `origin_turn_seq=3` is dropped (too old), `origin_turn_seq=5` is dropped (impossible — a helper from this very turn cannot have replied yet), `origin_turn_seq=6` is dropped (impossible / future), and a payload missing `origin_turn_seq` entirely is dropped. (7d) **fail-CLOSED behavior: with `variables['parent_turn_seq']` missing or non-int, ALL `memory_recall` payloads are dropped (a warning is logged) — the freshness contract requires we never inject a payload we cannot prove is fresh. Non-memory_recall messages from the same delivery are unaffected (they fall through to `other_messages` and render via the generic formatter). This is the F2 round-5 guard — without it, a misconfiguration could let stale helper memory inject indefinitely.** (7e) **kill-switch catch-all (round 9 guard): with `variables['memory_recall_helper_enabled'] == False`, ALL `memory_recall` payloads are dropped regardless of `origin_turn_seq` match. Non-memory_recall messages still flow through. Concrete test: prior turn enabled → helper spawned → user sets `memory_recall_helper_enabled=False` → helper completes and queues `memory_recall` payload → next parent turn_start fires deliver → payload is dropped, `injected_memory_ids` unchanged. This catches the across-disable/re-enable race the round-9 adversary identified: even if 3.4 still advances `parent_turn_seq` while the toggle is off (which it does — see 3.4's "Intentionally NOT gated" rationale), a queued payload's `origin_turn_seq` could happen to match the new `current - 1` value if disable-then-re-enable timing aligns; the catch-all drops it unambiguously.** For `_format_search_memories_result`: (8) empty `{"memories": []}` → `None`; (9) `{"memories": [{"id":"m1",...}]}` with `injected_memory_ids` empty → returns formatted string with `m1`, `injected_memory_ids` becomes `["m1"]`; (10) `{"memories": [{"id":"m1"},{"id":"m2"}]}` with `injected_memory_ids = ["m1"]` → returns formatted string containing `m2` only, `injected_memory_ids` becomes `["m1","m2"]`. End-to-end (manual): submit a real prompt, observe both fast-recall (priority 10) and helper-delivery (priority 10, also turn_start) on the same turn — verify a memory selected by both surfaces only ONCE in injected context and `injected_memory_ids` accumulates both writers' picks. On a subsequent turn where the helper or fast recall selects the same id, it does NOT re-appear. `tests/e2e/test_inter_agent_messages.py` continues to pass (parent ↔ child messaging via `send_message` + `deliver_pending_messages` is unaffected because non-memory_recall messages still flow through `other_messages`).

### 2.5 Add `cancel_stale_helpers` MCP tool sharing `stop_agent`'s lifecycle path [category: code] (depends: 2.3)

Target: `src/gobby/mcp_proxy/tools/agents.py` — the registry factory that owns `stop_agent` (lines ~304–419) and `kill_agent` (lines ~420+). Both `stop_agent` and the new `cancel_stale_helpers` will share an extracted private helper so the same lifecycle/process-kill/terminal-cleanup path runs for every cancellation.

Why not put cancellation into `spawn_agent`'s factory? The round-2 adversary correctly observed that `create_spawn_agent_registry` does NOT currently receive the lifecycle monitor, hook cleanup, or terminal-cleanup dependencies that `stop_agent` requires. Falling back to `runner.cancel_run(run_id)` alone would mark the DB row cancelled but leave the helper's subprocess alive and able to keep issuing MCP calls — defeating the freshness contract. Putting cancellation alongside `stop_agent` (which already has all the right deps wired) is the simpler, correct shape.

Step 1 — extract the per-run stop body into a private helper inside the same registry closure (so it captures `runner`, `agent_run_manager`, `db`, `lifecycle_monitor`, `completion_registry`, `session_manager`, `hook_manager_resolver`, `_kill_agent_process`, `_cleanup_terminal_artifacts` from the existing closure). The body is the verbatim contents of the current `async def stop_agent(run_id)` at `src/gobby/mcp_proxy/tools/agents.py:304-371` (verified line range against HEAD). It MUST preserve every step in that body; specifically, in this exact order:

```python
async def _stop_run(run_id: str) -> dict[str, Any]:
    """Shared cancellation: stop a single agent run end-to-end.

    Performs the same work the existing `stop_agent` MCP tool does, in
    the same order, with the same error semantics. Both `stop_agent` and
    `cancel_stale_helpers` delegate here so process-kill, lifecycle
    teardown, completion notification, and terminal cleanup are
    guaranteed to happen for every cancellation.
    """
    # Step 1: Look up the run; bail if missing or not pending/running.
    run = runner.get_run(run_id)
    if not run:
        return {"success": False, "error": f"Agent run {run_id} not found"}
    if run.status not in ("pending", "running"):
        return {"success": False, "error": f"Cannot stop agent in status: {run.status}"}

    # Step 2: Kill the underlying subprocess + close its terminal.
    kill_db = db or agent_run_manager.db
    result = await _kill_agent_process(
        run, kill_db, signal_name="TERM", close_terminal=True,
    )
    if not result.get("success") and result.get("error") != "No target PID found":
        return result  # Real kill failure — abort early.

    # Step 3: Transition the DB row to cancelled.
    # If lifecycle_monitor is wired, it owns this transition AND emits
    # the completion notification + terminalization side effects.
    # Otherwise fall back to runner.cancel_run + manual completion notify.
    transitioned = False
    if lifecycle_monitor is not None:
        transitioned = await lifecycle_monitor.terminalize_cancelled_run(
            run_id, terminal_reason="user_cancelled",
        )
    else:
        transitioned = runner.cancel_run(run_id)
        if transitioned and completion_registry is not None:
            await completion_registry.notify(
                run_id,
                {"status": "cancelled", "terminal_reason": "user_cancelled", "run_id": run_id},
                message=f"Agent {run_id} cancelled",
            )

    if not transitioned:
        current = runner.get_run(run_id)
        logger.debug(
            "stop_run no-op for run %s; current status=%s",
            run_id, current.status if current else "missing",
        )

    # Step 4: Tear down terminal artifacts (firing synthetic stop hook
    # internally if applicable). MUST run regardless of whether the DB
    # transition succeeded — terminal/tmux state still needs cleanup.
    await _cleanup_terminal_artifacts(
        tmux_session_name=run.tmux_session_name,
        agent_session_id=run.child_session_id,
        debug=False,
        session_manager=session_manager,
        hook_manager_resolver=hook_manager_resolver,
        result=result,
    )
    return {
        "success": True,
        "message": f"Agent run {run_id} stopped",
        "run_id": run_id,
        "status": "cancelled",
        "terminal_reason": "user_cancelled",
    }
```

Required cleanup steps the implementer MUST preserve (any omission is a regression vs HEAD's `stop_agent`):
- `runner.get_run(run_id)` lookup + status guard.
- `_kill_agent_process(run, kill_db, signal_name="TERM", close_terminal=True)` — process kill + terminal close.
- Conditional `lifecycle_monitor.terminalize_cancelled_run(run_id, terminal_reason="user_cancelled")` when `lifecycle_monitor` is not None.
- Fallback `runner.cancel_run(run_id)` + `completion_registry.notify(...)` when `lifecycle_monitor` is None.
- `_cleanup_terminal_artifacts(tmux_session_name=..., agent_session_id=..., session_manager=..., hook_manager_resolver=..., result=result)` — fires the synthetic stop hook (`_fire_synthetic_stop`) internally per its body; this is how the SessionStop hook chain stays intact for cancelled runs.
- Return-shape parity: `{"success": True, "message": ..., "run_id": ..., "status": "cancelled", "terminal_reason": "user_cancelled"}`.

Refactor existing `async def stop_agent(run_id: str) -> dict[str, Any]:` to delegate: `return await _stop_run(run_id)`. No external behavior change — this is a pure extract.

Step 2 — add the new public MCP tool in the same factory closure, using the same `@registry.tool(...)` decorator pattern as the surrounding tools (`stop_agent`, `kill_agent`, `end_agent_run`, etc.) — HEAD's `create_agents_registry` constructs `registry = InternalToolRegistry(...)` and registers tools via that decorator. There is no `server` variable in this closure; using `@server.tool()` would be a NameError:

```python
@registry.tool(
    name="cancel_stale_helpers",
    description=(
        "Cancel all still-running runs of an agent spawned by a parent "
        "session. Used by the priority-5 cancel rule to ensure freshness "
        "before delivery on each parent turn."
    ),
)
async def cancel_stale_helpers(
    parent_session_id: str,
    agent_name: str,
) -> dict[str, Any]:
    """Cancel all still-running runs of `agent_name` spawned by `parent_session_id`.

    Used by the priority-5 cancel rule (3.2) to ensure the freshness contract
    on memory-recall-helper. Best-effort: per-run failures are logged and the
    other runs still get cancelled.

    Returns:
        {
            "success": true,
            "cancelled": [<run_id>, ...],
            "errors": [{"run_id": ..., "error": "..."}, ...],
            "count": <len(cancelled)>,
        }
    """
    if not parent_session_id:
        return {"success": False, "error": "parent_session_id is required"}
    if not agent_name:
        return {"success": False, "error": "agent_name is required"}

    resolved_parent = _resolve_session_id(parent_session_id)
    # Reuse the registry closure's `agent_run_manager` instead of
    # constructing `LocalAgentRunManager(db)` directly. HEAD's
    # `create_agents_registry` initializes that variable as
    # `agent_run_manager = LocalAgentRunManager(db) if db else runner.run_storage`
    # so db-less test contexts (where the registry is built from a mocked
    # runner only) fall back to `runner.run_storage`. Constructing
    # `LocalAgentRunManager(db)` directly here would bypass that fallback
    # and break in those tests with a `db is None` failure.
    run_manager = agent_run_manager

    stale = [
        r for r in run_manager.list_by_parent(resolved_parent)
        if r.agent_name == agent_name and r.status in ("pending", "running")
    ]

    cancelled: list[str] = []
    errors: list[dict[str, str]] = []
    for run in stale:
        try:
            result = await _stop_run(run.id)
            if result.get("success"):
                cancelled.append(run.id)
            else:
                errors.append({"run_id": run.id, "error": result.get("error", "unknown")})
        except Exception as e:  # noqa: BLE001 — best-effort, keep going
            errors.append({"run_id": run.id, "error": str(e)})
            logger.warning(f"cancel_stale_helpers: failed to stop {run.id}: {e}")

    return {
        "success": True,
        "cancelled": cancelled,
        "errors": errors,
        "count": len(cancelled),
    }
```

Notes:
- `_resolve_session_id` is already defined locally in the factory (used by `stop_agent`). Reuse, do not re-resolve.
- `LocalAgentRunManager` is the storage class on HEAD (verified at `src/gobby/storage/agents.py:179`); the round-2 plan's `AgentRunStorage` reference was wrong.
- The (`pending`, `running`) status filter mirrors what 2.3's storage helper assumes "needs cancellation" — both are not-yet-terminal states that could still emit `send_message`.
- The MCP rule schema and the rule-engine's effect dispatcher both already accept arbitrary kwargs as `arguments:`; no changes needed to the rule layer to call this tool.

Validation criteria: tool callable via `mcp__gobby__call_tool(server_name="gobby-agents", tool_name="cancel_stale_helpers", arguments={"parent_session_id": "#X", "agent_name": "memory-recall-helper"})`. With no running helpers for `#X`, returns `{"success": True, "cancelled": [], "errors": [], "count": 0}`. With one running helper for `#X`, returns `{"success": True, "cancelled": ["run-…"], "errors": [], "count": 1}`, the run's `agent_runs.status` becomes `cancelled` (DB-verifiable), AND the helper's tmux pane is dead (per `_cleanup_terminal_artifacts` in the shared `_stop_run`). With two stale helpers where stopping the first raises an exception, the second is still cancelled and `errors` contains the first's failure — best-effort guarantee. Missing `parent_session_id` or `agent_name` returns `{"success": False, "error": "..."}`. **Cleanup-step parity**: integration test that asserts `_stop_run` invokes `_kill_agent_process(..., close_terminal=True)`, then `lifecycle_monitor.terminalize_cancelled_run(...)` (or the fallback `runner.cancel_run(...)` + `completion_registry.notify(...)`), then `_cleanup_terminal_artifacts(...)` — in that order — for every successful path. Use mocks/spies on these functions in the registry closure and assert call order; this protects against accidentally dropping a step during the extract. **db-less registry contract test (round 10 guard)**: instantiate `create_agents_registry(runner=mock_runner, db=None, ...)` where `mock_runner.run_storage.list_by_parent` returns a list with one running helper. Call the registered `cancel_stale_helpers` tool with the corresponding parent and `agent_name="memory-recall-helper"`. Assert (a) no `db is None` failure is raised, (b) the cancellation succeeds via the runner.run_storage fallback. Without reusing the closure's `agent_run_manager`, this test fails. Existing `stop_agent` still works identically (delegates to `_stop_run` now); existing tests `tests/mcp_proxy/tools/test_agents_*.py` pass without modification. New unit test `tests/mcp_proxy/tools/test_cancel_stale_helpers.py` covers all the cases above.

## Phase 3: Wiring

**Goal**: At every parent `turn_start`, in priority order: increment turn counter (3.4 at priority 1) → cancel any stale helper (3.2 at priority 5) → deliver pending P2P messages with dedup, cancelled-session filter, and origin_turn_seq freshness filter (3.1 at priority 10) → spawn fresh helper for the new prompt with the current `parent_turn_seq` baked into its prompt (3.3 at priority 12). This rule ordering is what makes the freshness contract correct: by the time delivery runs, the counter has advanced and any stale helper is already DB-marked `cancelled`, and 2.4's delivery formatter applies BOTH freshness guards (cancelled-session AND origin_turn_seq) before injecting any helper memory_recall payload.

**Phase 3 entry criteria (operational, NOT DB-enforced; verify before claiming any Phase 3 task):**

Phase 3 wires up rules and an agent definition that REFERENCE Phase 1 and Phase 2 outputs. None of Phase 3 will function correctly until those outputs are merged. Per the round-8 adversary finding, the current task expander does NOT deterministically emit cross-phase `tasks.dependencies` edges from header annotations, so we cannot rely on the dependency engine to block Phase 3 on Phase 1/2 outputs. The implementer is operationally responsible for this gating. Before claiming or working any Phase 3 task, verify ALL of:

- Phase 1: 1.2 (`MemoryRecallHelperConfig`) merged. 1.3 (config thread + `parent_turn_seq` seed in `_session_start.py`) merged AND 1.1 monolith gate closed. 1.4 (helper YAML) present in `src/gobby/install/shared/workflows/agents/memory-recall-helper.yaml` and synced to `workflow_definitions` (verifiable via `gobby agents show memory-recall-helper --json`).
- Phase 2: 2.1 (`send_message` `from_session` default), 2.2 (`_check_agent_tool_enforcement` reorder), 2.3 (`get_cancelled_session_ids`), 2.4 (delivery + same-turn dedup formatters in `EffectsMixin`), 2.5 (`cancel_stale_helpers` MCP tool) ALL merged. Verify 2.5 by calling `mcp__gobby__call_tool(server_name="gobby-agents", tool_name="cancel_stale_helpers", arguments={"parent_session_id":"#<self>","agent_name":"memory-recall-helper"})` and observing a successful `{"success": true, ...}` response. (Note: the wrapper schema uses `server_name` and `tool_name`, NOT `server`/`tool` — the latter are valid only inside rule `mcp_call` effects, not the top-level wrapper call.)

If any output is missing, escalate the Phase 3 task with a specific reason naming the missing output. Do NOT proceed.

**Intra-phase dependencies inside Phase 3** (which the expander handles reliably for same-phase deps): 3.3 (spawn rule) depends on 3.1 (deliver), 3.2 (cancel), and 3.4 (counter) — all same-phase. 3.4 has no Phase 3 deps. 3.1 has no Phase 3 deps (touches an existing rule). 3.2 has no Phase 3 deps. These same-phase deps are encoded in the section headers below.

### 3.1 Modify `deliver-pending-messages` rule to fire for parent sessions [category: config]

**Cross-phase preconditions (operational; verify before editing): 2.4 merged.** This rule's behavior is meaningless without 2.4's `_format_delivery_result` formatter — without it the inline `inject_result: true` path injects raw `messages[*].content` JSON. Without 3.4 merged (the priority-1 counter rule), the `parent_turn_seq` variable is missing, which 2.4 treats as fail-closed (drops all `memory_recall` payloads with a warning). 3.1 itself does not technically depend on 3.4 at expansion time (no same-phase edge), but the e2e behavior is tested only after 3.4 is also wired.

Target: `src/gobby/install/shared/workflows/rules/messaging/deliver-pending-messages.yaml` (existing file).

Three changes vs the current file:

1. Drop the `when: "variables.get('is_spawned_agent')"` line so the rule fires for parents too (the underlying tool is session-scoped).
2. Add an explicit `arguments: { target_session_id: "{{ event.metadata.get('_platform_session_id') }}" }` block — the dispatcher does not auto-inject `target_session_id` (only `session_id`), and `deliver_pending_messages`'s schema requires `target_session_id`. The template MUST resolve via `event.metadata['_platform_session_id']` (canonical Gobby session row id), NOT `event.session_id` (CLI external id — Claude `external_id` / Codex `thread_id`). Using the external id would force `deliver_pending_messages` through the proxy's external-id fallback resolver, which is ambiguous for non-UUID externals and entirely wrong when the external id maps to a different Gobby session than the platform id (legitimate mid-session reattach scenarios).
3. Add `inject_result: true` to the `mcp_call` effect. Phase 2.4's pipeline is the consumer of `inject_result` for this tool.

Replace the entire file contents with:

```yaml
tags: [messaging, p2p, commands, gobby, default]

rules:
  deliver-pending-messages:
    description: "Deliver pending inter-session messages on each agent turn"
    event: turn_start
    enabled: true
    priority: 10
    effects:
      - type: mcp_call
        server: gobby-agents
        tool: deliver_pending_messages
        arguments:
          target_session_id: "{{ event.metadata.get('_platform_session_id') }}"
        inject_result: true
```

Existing tests touching this rule (`tests/e2e/test_inter_agent_messages.py::test_parent_child_message_exchange`) must continue to pass — child → parent and parent → child messaging both still rely on this rule, so gate removal must not regress those flows.

Validation criteria: file at the listed path matches the YAML above exactly. Daemon restart loads the rule; `gobby rules show deliver-pending-messages --json` returns a payload where (a) `when` is `null`/empty (the gate is removed), (b) `enabled` is `true`, (c) `priority` is `10`, (d) `event` is `turn_start`, (e) `effects[0].type` is `mcp_call`, `effects[0].server` is `gobby-agents`, `effects[0].tool` is `deliver_pending_messages`, `effects[0].inject_result` is `true`, and `effects[0].arguments.target_session_id` is the literal templated string `"{{ event.metadata.get('_platform_session_id') }}"` (NOT `"{{ event.session_id }}"` — the external id resolution path is wrong here and was the round-12 F1 finding). (`gobby rules list` only returns summaries — name/event/priority/enabled — and CANNOT verify `when`/`arguments`/`inject_result`. Use `gobby rules show <name> --json` for structural assertions; `gobby rules list` is acceptable only as an existence check.)

**Rule-definition tests (required, not optional)**: update `tests/workflows/test_messaging_rules.py::TestDeliverPendingMessages` (which currently hard-codes the old `is_spawned_agent` gate and no-arguments effect) to assert the new contract:

- No `when:` clause on the rule definition (the test must explicitly check `rule.condition is None` or equivalent — failing if a stale `is_spawned_agent` gate is reintroduced).
- Effect's `arguments` field equals `{"target_session_id": "{{ event.metadata.get('_platform_session_id') }}"}` (string match on the templated value, exactly as written in the YAML — must NOT contain the external-id form `"{{ event.session_id }}"`).
- Effect's `inject_result` field is `True`.
- Effect's `server` is `"gobby-agents"` and `tool` is `"deliver_pending_messages"`.
- Rule's `event` is `"turn_start"` and `priority` is `10`.

`tests/e2e/test_inter_agent_messages.py` passes after the change.

A manual end-to-end test (must include valid `origin_turn_seq` to match 2.4's fail-closed freshness guard B — without it, the formatter drops the payload):
1. Read `current_seq = get_variable(name="parent_turn_seq", session_id=#<self>)`.
2. From a parent session, call `send_message(to_session=#<self>, content='{"type":"memory_recall","origin_turn_seq":<current_seq>,"memories":[{"id":"test1","content":"test memory"}],"rationale":"manual"}')` (omitting `from_session` to verify 2.1's auto-fill). The `origin_turn_seq` value MUST equal the parent's current `parent_turn_seq` because the next `turn_start` will increment it to `current_seq + 1` (via 3.4's priority-1 rule), and the formatter's filter accepts payloads where `origin_turn_seq == new_seq - 1 == current_seq`.
3. Trigger a `turn_start`. Result is (a) the test memory rendered ONCE via the search_memories formatter in the parent's context, (b) `get_variable(name="injected_memory_ids", session_id=#<self>)` containing `"test1"` after the turn, (c) repeating the same content with the same (now-stale) `origin_turn_seq` and another `turn_start` results in `test1` being filtered (no re-injection) — both because the turn-seq guard now drops it AS stale (origin no longer matches new_seq - 1) AND because `injected_memory_ids` already contains `"test1"`.
4. **Negative case (proves fail-closed guard B)**: send a `memory_recall` payload with NO `origin_turn_seq` field, then trigger `turn_start`; result is the payload is dropped (no injection, no `injected_memory_ids` mutation), with a warning logged.
5. **Negative case (proves scoped guard A)**: from a different child session whose corresponding `agent_runs.status` is `cancelled` and `agent_name='other-agent'` (NOT memory-recall-helper), `send_message` a plain text P2P payload (NOT a memory_recall envelope) to the parent. Trigger `turn_start`. Result: the plain message renders via the generic formatter — proves the cancelled-session filter does not silently discard non-helper child output. A `memory_recall` payload from a `cancelled` `memory-recall-helper` run, by contrast, IS dropped.

A turn_start with no pending messages results in NO `inject_result` noise in the parent's context.

### 3.2 Create `cancel-stale-memory-recall-helpers` rule (priority 5, before delivery) [category: config]

**Cross-phase precondition (operational; verify before claiming): 2.5 merged.** This rule invokes `cancel_stale_helpers` which is added in 2.5. Verify the tool exists by `list_tools(server_name='gobby-agents')` showing `cancel_stale_helpers` in the result before working this task.

Target: `src/gobby/install/shared/workflows/rules/memory-lifecycle/cancel-stale-memory-recall-helpers.yaml` (new file).

This rule fires at every parent `turn_start` and invokes the `cancel_stale_helpers` MCP tool from 2.5 with `agent_name="memory-recall-helper"`. Priority `5` ensures it runs strictly before `deliver-pending-messages` at priority `10`. By the time the delivery rule reads the inter-session message queue and 2.4's formatter checks `LocalAgentRunManager.get_cancelled_session_ids`, any in-flight helper from the previous turn has already been cancelled — so any of its racy-queued `memory_recall` messages get dropped instead of injected against an unrelated prompt.

File contents (write verbatim):

```yaml
tags: [memory-lifecycle, memory, helper-agent, gobby, default]

rules:
  cancel-stale-memory-recall-helpers:
    description: "Cancel any in-flight memory-recall-helper from a prior parent turn before delivery runs"
    event: turn_start
    enabled: true
    priority: 5
    when: >
      not variables.get('is_spawned_agent')
    effects:
      - type: mcp_call
        server: gobby-agents
        tool: cancel_stale_helpers
        arguments:
          parent_session_id: "{{ event.metadata.get('_platform_session_id') }}"
          agent_name: memory-recall-helper
        inject_result: true   # forces inline-await; 2.4 formatter returns None so no context noise injects
```

Why each clause:
- `priority: 5` — strictly less than `deliver-pending-messages` (10) and `memory-recall-on-prompt` (10). The rule engine evaluates rules in ascending priority order, but priority alone is NOT enough to guarantee the cancel call completes before delivery reads the queue — see the `inject_result: true` bullet below.
- `when: not is_spawned_agent` — only parent sessions need stale-helper cancellation. Spawned helpers themselves should not call this on `turn_start`.
- **Intentionally NOT gated on `memory_recall_helper_enabled`.** The kill-switch only controls whether NEW helpers are spawned. If a helper was spawned while enabled and the user toggles disable BEFORE that helper completes, the helper is still in the runtime; this rule must continue cancelling it on the next parent turn even though the feature is disabled, otherwise its eventual `send_message` payload would sit in the queue and inject when the feature is re-enabled. (Round 9 adversary finding: gating cancel on the toggle reintroduced a stale-injection path across disable/re-enable cycles.) When the feature is disabled and no helpers exist, this rule's `cancel_stale_helpers` call is a cheap no-op (returns `cancelled: []`).
- **`inject_result: true` is REQUIRED** as a synchronous-await marker, not because the cancellation result is meant to be injected. Per `EffectsMixin._apply_effect` in HEAD (`src/gobby/workflows/engine/effects.py:57+`), an `mcp_call` effect is inline-awaited ONLY when `effect.inject_result and not effect.background and self._mcp_dispatcher` is true; otherwise it is appended to `mcp_calls` metadata and dispatched only after `workflow_handler.handle(event)` returns. Without `inject_result: true` here, the cancel call would defer, the priority-10 delivery (which DOES set `inject_result: true`) would run inline first, and delivery would read the queue with the stale helper still `running` — exactly the bug round-3 found. 2.4's `_apply_effect` formatter switch has a dedicated `("gobby-agents", "cancel_stale_helpers") → return None` case so this awaited call injects no visible context. **`background:` MUST remain unset/false**: `background: true` would also defer the call regardless of `inject_result`, breaking the sync contract. An expansion worker who removes `inject_result: true` or sets `background: true` for "tidiness" reintroduces the round-3 race.

Validation criteria: file at the listed path; daemon restart loads the rule; `gobby rules show cancel-stale-memory-recall-helpers --json` returns a payload where `priority` is `5`, `event` is `turn_start`, `enabled` is `true`, `when` (string) contains `is_spawned_agent` AND does NOT contain `memory_recall_helper_enabled` (the toggle gate must NOT be on this rule — see freshness rationale above), `effects[0].type` is `mcp_call`, `effects[0].server` is `gobby-agents`, `effects[0].tool` is `cancel_stale_helpers`, `effects[0].arguments.parent_session_id` is `"{{ event.metadata.get('_platform_session_id') }}"` (canonical platform id, NOT external `"{{ event.session_id }}"` — round-12 F1 finding), `effects[0].arguments.agent_name` is `"memory-recall-helper"`, AND `effects[0].inject_result` is `true` (this is the sync marker — the formatter returns None so it injects nothing).

**Ordering regression test (required, not optional)**: add `tests/workflows/test_memory_recall_helper_ordering.py`. Construct a `RuleEngine` with both `cancel-stale-memory-recall-helpers` (priority 5, with `inject_result: true`) and `deliver-pending-messages` (priority 10, with `inject_result: true`) loaded. Stub `_mcp_dispatcher` to record (server, tool, timestamp, arguments) for each call. Fire a `turn_start` event. Assert: (a) the `cancel_stale_helpers` dispatch's timestamp strictly precedes the `deliver_pending_messages` dispatch's timestamp; (b) BOTH appear in the inline-await order, NEITHER appears in the deferred `mcp_calls` list returned by `_evaluate_workflow_rules`. This protects against a future regression where someone removes `inject_result: true` from the cancel rule (which would silently make it deferred and break the freshness contract). A second test: with a real DB and a manually-inserted `agent_runs` row of `status='running'` for `agent_name='memory-recall-helper'` and a queued `inter_session_messages` row from that run's child session, fire a `turn_start` and assert: the run's status transitions to `cancelled` BEFORE `_format_delivery_result` runs (verifiable by checking `LocalAgentRunManager.get(...).status` between the two dispatcher invocations).

**Session-id resolution regression test (required, not optional — round-12 F1 guard)**: add a third test in the same file. The "turn_start" rule event in HEAD is `HookEventType.BEFORE_AGENT` (`src/gobby/hooks/events.py:33` — `BEFORE_AGENT = "before_agent"`); there is no `PROMPT_SUBMIT` member. The spawn rule's `when:` clause requires four conditions to fire: a `>=6`-word `event.data["prompt"]`, `variables["memory_recall_helper_enabled"] == True`, `variables.get("is_spawned_agent")` falsy, and a seeded `parent_turn_seq` (so the `{{ variables.parent_turn_seq | int }}` in the rendered prompt resolves to a real integer). Construct:

```python
event = HookEvent(
    event_type=HookEventType.BEFORE_AGENT,
    session_id="external-X",                     # CLI external id (Claude external_id / Codex thread_id)
    source=SessionSource.CLAUDE_CODE,
    timestamp=datetime.now(timezone.utc),
    data={"prompt": "six or more words for helper spawn rule"},
    metadata={"_platform_session_id": "platform-Y"},  # canonical Gobby session row id, deliberately != session_id
)
```

Fire it via `RuleEngine.evaluate(event, session_id="platform-Y", variables={"memory_recall_helper_enabled": True, "parent_turn_seq": 7, "servers_listed": True})` (the `servers_listed=True` short-circuit avoids the auto-discovery side-rules polluting the dispatcher transcript). All three new rules MUST be loaded (`cancel-stale-memory-recall-helpers`, `deliver-pending-messages`, `spawn-memory-recall-helper`). Stub `_mcp_dispatcher` to record `(server, tool, arguments)` for every inline dispatch.

Assert:

- (a) `cancel_stale_helpers` was dispatched inline with `arguments["parent_session_id"] == "platform-Y"` and NOT `"external-X"`.
- (b) `deliver_pending_messages` was dispatched inline with `arguments["target_session_id"] == "platform-Y"` and NOT `"external-X"`.
- (c) The `spawn_agent` call appears NOT in the inline dispatcher transcript (the spawn rule has `background: true`) but in `response.metadata["mcp_calls"]` (the deferred list). That deferred entry has `arguments["agent"] == "memory-recall-helper"`, `arguments["parent_session_id"] == "platform-Y"`, and `arguments["prompt"]` is a rendered string containing the literal substring `"Parent session: platform-Y"` (NOT `"Parent session: external-X"`) AND `"origin_turn_seq: 7"` (proving `parent_turn_seq` resolved through `_render_template`).
- (d) Sensitivity check: temporarily monkey-patch the three rule definitions in-test to substitute `event.metadata.get('_platform_session_id')` → `event.session_id` in the rendered argument templates, re-run `RuleEngine.evaluate` with the same event, and assert the same assertions flip from pass to fail (now resolving to `"external-X"`). This proves the test actually exercises the resolution path rather than passing trivially.

This is the explicit guard against round-12 F1: any future PR that switches the rule YAMLs from `event.metadata.get('_platform_session_id')` back to `event.session_id` fails this test.

**Rule-definition tests (required, not optional)**: add a new test class `TestCancelStaleMemoryRecallHelpers` to `tests/workflows/test_memory_lifecycle_rules.py` paralleling the structural assertions made for the spawn rule below. Add `"cancel-stale-memory-recall-helpers"` to the `MEMORY_RULES` set at the top of that file (line 33) so `TestMemoryLifecycleSync` covers it.

End-to-end (manual): start a session, submit a 6+-word prompt to spawn helper N. Before helper N completes, submit a second prompt. Observe in `agent_runs` that helper N's status transitions to `cancelled` (set by 2.5's tool, fired by this rule at priority 5) BEFORE `deliver-pending-messages` at priority 10 runs. If helper N had time to call `send_message`, observe in the parent's context at the second turn that the cancelled helper's memory payload was NOT injected.

### 3.3 Create `spawn-memory-recall-helper` rule [category: config] (depends: 3.1, 3.2, 3.4)

**Cross-phase preconditions (operational; verify before claiming):** 1.3 merged (`parent_turn_seq` seeded; this rule reads it via `{{ variables.parent_turn_seq }}` in the helper prompt template). 1.4 merged (helper YAML; this rule references `agent: memory-recall-helper`). 2.2 merged (enforcement reorder; without it the helper's `blocked_tools` listing of `mcp__gobby__set_variable` does not actually take effect). Verify 1.4 sync via `gobby agents show memory-recall-helper --json` returning a non-error payload before claiming this task.

Target: `src/gobby/install/shared/workflows/rules/memory-lifecycle/spawn-memory-recall-helper.yaml` (new file).

This rule fires at every parent `turn_start` and spawns a backgrounded `memory-recall-helper` agent. The helper's runtime context (parent session ID, parent's user prompt) is composed into the `prompt` argument — `spawn_agent` (`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py:141–336`) takes a single `prompt: str` plus static knobs; there is no separate `inputs:` parameter.

File contents (write verbatim):

```yaml
tags: [memory-lifecycle, memory, helper-agent, gobby, default]

rules:
  spawn-memory-recall-helper:
    description: "Spawn backgrounded Haiku helper to filter memory recall on each prompt"
    event: turn_start
    enabled: true
    priority: 12
    when: >
      len((event.data.get('prompt') or '').split()) >= 6
      and not variables.get('is_spawned_agent')
      and variables.get('memory_recall_helper_enabled', True)
    effects:
      - type: mcp_call
        server: gobby-agents
        tool: spawn_agent
        arguments:
          agent: memory-recall-helper
          parent_session_id: "{{ event.metadata.get('_platform_session_id') }}"
          prompt: |
            Parent session: {{ event.metadata.get('_platform_session_id') }}
            origin_turn_seq: {{ variables.parent_turn_seq | int }}
            Parent's user prompt for this turn:

            {{ event.data.prompt }}

            Follow your standing instructions: fetch the parent's digest via
            get_session, read injected_memory_ids on the parent, run focused
            search_memories, select 0–3 clearly-relevant memories that
            haven't already been surfaced, and either send_message them to
            the parent (omitting from_session — the proxy auto-fills it
            from your session context) or finish silently. Do NOT write to
            injected_memory_ids yourself — the parent's delivery flow
            handles that. ALWAYS include the origin_turn_seq value above
            (verbatim, as an integer) in your memory_recall payload — if
            you omit it or change it, the parent's delivery formatter
            will drop your payload as stale.
        background: true
```

Why each clause:

- `event: turn_start` — fires once per user prompt, in parallel with the existing `memory-recall-on-prompt` (priority 10).
- `priority: 12` — runs *after* the fast vector recall so the immediate baseline ships first.
- `when: len(prompt.split()) >= 6` — mirrors the existing recall rule's skip-trivial-prompts check.
- `when: not variables.get('is_spawned_agent')` — prevents the helper from spawning *another* helper if a spawned agent ever issues a prompt (the helper itself is a spawned agent, so without this it would self-fork).
- `when: variables.get('memory_recall_helper_enabled', True)` — runtime master kill-switch. Seeded from `DaemonConfig.memory_recall_helper.enabled` at every `session_start` by 1.3. Default-True means a fresh session with a misconfigured daemon (no daemon_config available to `EventHandlers`) still spawns the helper rather than silently disabling it.
- `background: true` — `mcp_call` effect runs without blocking turn_start (per `src/gobby/workflows/engine/effects.py:142–166`).
- `parent_session_id` and the prompt's `Parent session:` line are composed via `{{ event.metadata.get('_platform_session_id') }}` (NOT `event.session_id`); the user prompt body is composed via `{{ event.data.prompt }}`. The rule template engine exposes `event` directly per `_build_eval_context` (`src/gobby/workflows/engine/templating.py:36–105`), and `HookEvent.metadata` is a `dict[str, Any]` field on `HookEvent` (`src/gobby/hooks/events.py:85–124`), so `event.metadata.get(...)` resolves at template time. `event.session_id` is the CLI external id (Claude `external_id` / Codex `thread_id`); the helper needs the canonical Gobby platform session id for `get_session`, `get_variable`, and `injected_memory_ids` reads.
- The prompt explicitly tells the helper to omit `from_session` on `send_message` calls. 2.1's runtime change auto-fills it from the helper's SessionContext (the proxy populates it from the helper's session header), so the helper does not need to know its own child session id.
- The freshness contract — "at most one running helper per parent, no stale memory_recall payload ever injects regardless of how the prior helper terminated" — is owned by two guards: (A) 3.2 (`cancel-stale-memory-recall-helpers` at priority 5) + 2.4's cancelled-session filter, and (B) 3.4 (priority-1 `parent_turn_seq` increment) + 2.4's `origin_turn_seq` freshness check. 3.3 itself stays simple: it always spawns. By the time 3.3 fires at priority 12, 3.4 has already incremented `parent_turn_seq`, 3.2 has cancelled any in-flight helper from the prior turn, and 3.1 has delivered the queue with both filters applied. There is no per-spawn `supersede` flag — that approach was rejected because (a) the rule-priority race meant delivery at 10 would inject stale payloads before a spawn-time supersede at 12 could cancel them, and (b) `spawn_agent`'s factory does not have access to the lifecycle/process-kill deps that proper cancellation requires.
- `origin_turn_seq: {{ variables.parent_turn_seq | int }}` is templated into the helper prompt. At priority 12, `parent_turn_seq` has already been incremented by 3.4 (priority 1), so the helper receives the CURRENT turn's number. The helper echoes that integer in its `memory_recall` payload. At the next parent turn_start, 2.4's delivery formatter compares the payload's echoed value against `current_parent_turn_seq - 1` (where `current_parent_turn_seq` is THIS turn's value, also already incremented). Match → fresh, accept. Mismatch (older or future) → stale, drop.

Validation criteria: file exists at the listed path; daemon restart loads the rule; `gobby rules show spawn-memory-recall-helper --json` returns a payload where (a) `enabled` is `true`, (b) `priority` is `12`, (c) `event` is `turn_start`, (d) `when` (string) contains all three guards as substrings: `event.data.get('prompt')`, `is_spawned_agent`, and `memory_recall_helper_enabled`, (e) `effects[0].type` is `mcp_call`, `effects[0].server` is `gobby-agents`, `effects[0].tool` is `spawn_agent`, `effects[0].background` is `true`, (f) `effects[0].arguments.agent` is `"memory-recall-helper"`, `effects[0].arguments.parent_session_id` is `"{{ event.metadata.get('_platform_session_id') }}"` (NOT external `"{{ event.session_id }}"`), `effects[0].arguments` does NOT contain a `supersede` key, and `effects[0].arguments.prompt` contains all three template references: `"{{ event.metadata.get('_platform_session_id') }}"` (the `Parent session:` line — NOT `"{{ event.session_id }}"`), `"{{ event.data.prompt }}"`, AND `"{{ variables.parent_turn_seq"`. **The plan MUST NOT contain any literal `{{ event.session_id }}` reference inside Phase 3 rule YAMLs or their validation criteria — this is the round-12 F1 fix.** `gobby rules list` may be used as an existence check (it shows name/event/priority/enabled summary only) but cannot verify `when`/`arguments`/`effects` internals — use `--json` for those.

**Rule-definition tests (required, not optional)**: add a new rule-level test class `TestSpawnMemoryRecallHelper` to `tests/workflows/test_memory_lifecycle_rules.py` paralleling the existing `TestMemoryRecallOnPrompt` class in the same file (which is the closest structural analog — both are `turn_start` rules with a `when:` clause and a single `mcp_call` effect). The new class asserts the rule's contract:

- Rule's `event` is `"turn_start"`, `priority` is `12`, `enabled` is `True`.
- Rule's `condition` (the `when:` clause) contains all three guards as substrings (or parses to an AST including all three): (a) `len((event.data.get('prompt') or '').split()) >= 6`, (b) `not variables.get('is_spawned_agent')`, (c) `variables.get('memory_recall_helper_enabled', True)`.
- Rule has exactly one effect of type `mcp_call`.
- Effect's `server` is `"gobby-agents"`, `tool` is `"spawn_agent"`, `background` is `True`.
- Effect's `arguments` includes `agent: "memory-recall-helper"` and `parent_session_id: "{{ event.metadata.get('_platform_session_id') }}"` (string match — NOT external `"{{ event.session_id }}"`). MUST NOT include `supersede` (the round-2 design that placed cancellation at spawn time was rejected — cancellation now lives in rule 3.2).
- Effect's `arguments.prompt` is a non-empty string containing the literal `"{{ event.metadata.get('_platform_session_id') }}"` (the `Parent session:` line), `"{{ event.data.prompt }}"`, AND `"{{ variables.parent_turn_seq"` (template references the helper needs). It MUST NOT contain `"{{ event.session_id }}"` anywhere.

Additionally, add `"spawn-memory-recall-helper"` to the `MEMORY_RULES` set defined at the top of `tests/workflows/test_memory_lifecycle_rules.py` (line 33). That manifest is consulted by `TestMemoryLifecycleSync` (lines 73, 82, 92) for cross-rule sync checks; omitting the new rule here would leave it outside the file's existing coverage net.

Behavioral validation: submitting a real prompt of ≥ 6 words to a parent (non-spawned-agent) session triggers a spawn — `gobby agents runs list --status running --json` shows a new run shortly after the prompt with `agent_name == "memory-recall-helper"` (use the JSON variant — plain text output may not include the agent name). Equivalent: `gobby agents runs show <run_id_prefix> --json` and assert `agent_name`. Direct-DB equivalent: query `agent_runs` for `agent_name='memory-recall-helper'` ordered by `created_at DESC` and inspect the most recent row. Submitting a 1-word prompt does not spawn. Manually setting `is_spawned_agent: true` on a session via `set_variable` and submitting a prompt does NOT spawn. Setting `memory_recall_helper.enabled: false` in the daemon config and restarting causes new sessions to NOT spawn the helper on prompts. The parent session's `turn_start` is not blocked — Claude Code starts streaming a response within the normal latency window (no Haiku-call wait inserted into the critical path). When the helper completes and `send_message`s a `memory_recall` payload (omitting `from_session`), the parent's NEXT `turn_start` (a) injects the helper's selected memories once via the search_memories formatter (NOT as raw JSON dump of the message body), (b) appends the surfaced IDs to the parent's `injected_memory_ids` (verifiable by `get_variable`), and (c) on a subsequent helper turn that re-selects those IDs, dedup filters them out before injection.

### 3.4 Create `increment-parent-turn-seq` rule (priority 1, before all other turn_start rules) [category: config]

**Cross-phase precondition (operational; verify before claiming): 1.3 merged.** This rule increments the `parent_turn_seq` session variable seeded at session_start by 1.3's edits to `_activate_default_agent`. Without 1.3 merged, the variable does not exist on new sessions and the increment template falls back to `0 + 1 = 1` on every turn (still functional, but the seed is bypassed). Verify 1.3 by checking that a fresh session has `variables['parent_turn_seq'] == 0` immediately after session_start (before any turn_start rule fires).

Target: `src/gobby/install/shared/workflows/rules/memory-lifecycle/increment-parent-turn-seq.yaml` (new file).

This rule fires at every parent `turn_start` BEFORE any other turn_start rule (priority 1) and increments the session-scoped `parent_turn_seq` variable seeded by 1.3. The increment uses Jinja2 templating (which `_apply_set_variable` calls via `_render_template`, then coerces the rendered string back to int via `_coerce_rendered_value` — verified at `src/gobby/workflows/engine/effects.py`'s `_apply_set_variable` and `_coerce_rendered_value`). The cast `| int` defends against the value somehow being stored as a string after a serialization round-trip.

File contents (write verbatim):

```yaml
tags: [memory-lifecycle, memory, helper-agent, gobby, default]

rules:
  increment-parent-turn-seq:
    description: "Increment per-parent monotonic turn counter on each turn_start (freshness guard B for memory-recall-helper)"
    event: turn_start
    enabled: true
    priority: 1
    when: >
      not variables.get('is_spawned_agent')
    effects:
      - type: set_variable
        variable: parent_turn_seq
        value: "{{ (variables.get('parent_turn_seq', 0) | int) + 1 }}"
```

Why each clause:
- `priority: 1` — strictly lower than 3.2 (5), 3.1 (10), 3.3 (12). The increment must happen before the cancel rule, before the delivery formatter reads `variables['parent_turn_seq']`, and before the spawn rule reads `variables.parent_turn_seq` for the helper prompt. The rule engine evaluates rules in ascending priority order; `set_variable` is a synchronous in-memory effect (no async dispatch) so the result is immediately visible to subsequent rules.
- `when: not is_spawned_agent` — only parent sessions need this counter. Spawned helpers do not spawn other helpers, so they do not need the counter on their own turn_start.
- **Intentionally NOT gated on `memory_recall_helper_enabled`.** The counter must advance on every parent turn_start regardless of the toggle, otherwise a helper queued while enabled could send a payload during a disabled interval and have its `origin_turn_seq` accidentally match `current_parent_turn_seq - 1` when the feature is later re-enabled, injecting stale memory against an unrelated prompt. (Round 9 adversary finding: gating the counter on the toggle froze the sequence and broke freshness across enable/disable cycles.) The cost of running this rule while the feature is disabled is one `set_variable` call per parent turn — negligible.
- `set_variable` with a Jinja arithmetic expression — `_render_template` produces a numeric string ("1", "2", …), `_coerce_rendered_value` converts to int. Verified path in HEAD at `src/gobby/workflows/engine/effects.py`'s `_apply_set_variable` (templates render before expression eval) and `_coerce_rendered_value` (string → int coercion).

Validation criteria: file at the listed path; daemon restart loads the rule; `gobby rules show increment-parent-turn-seq --json` returns a payload where `priority` is `1`, `event` is `turn_start`, `enabled` is `true`, `when` (string) contains `is_spawned_agent` AND does NOT contain `memory_recall_helper_enabled` (counter must advance regardless of the toggle), `effects[0].type` is `set_variable`, `effects[0].variable` is `"parent_turn_seq"`, `effects[0].value` is the literal Jinja string `"{{ (variables.get('parent_turn_seq', 0) | int) + 1 }}"`.

**Rule-definition tests (required, not optional)**: add `TestIncrementParentTurnSeq` to `tests/workflows/test_memory_lifecycle_rules.py` paralleling the structural assertions for the other helper rules. Add `"increment-parent-turn-seq"` to the `MEMORY_RULES` set at the top of that file so `TestMemoryLifecycleSync` covers it.

**Behavioral test (required)**: in `tests/workflows/test_memory_recall_helper_ordering.py` (the same file added in 3.2's regression test), construct a `RuleEngine` with all four rules loaded (3.4 priority 1, 3.2 priority 5, 3.1 priority 10, 3.3 priority 12). Seed a session with `parent_turn_seq=0`. Fire two `turn_start` events. Assert: (a) after first turn_start, `variables['parent_turn_seq'] == 1`; (b) after second turn_start, `variables['parent_turn_seq'] == 2`; (c) the spawn rule's resolved prompt for turn 2 contains `"origin_turn_seq: 2"` (verifies the spawn rule reads the post-increment value); (d) a synthetic `memory_recall` message in the queue at turn 2 with `origin_turn_seq=1` is INJECTED (matches turn 2's expected origin = 2-1 = 1); (e) a synthetic `memory_recall` message at turn 2 with `origin_turn_seq=0` is DROPPED (stale).

End-to-end (manual): submit two prompts. After the first turn_start, `get_variable(name="parent_turn_seq", session_id=#<self>)` returns `1`. After the second, returns `2`. Verify the helper's prompt for the second turn (visible via `tmux capture-pane` on the helper's tmux session) contains `origin_turn_seq: 2`.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
