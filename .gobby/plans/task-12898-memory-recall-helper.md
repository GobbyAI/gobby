# Smarter memory recall via backgrounded Haiku helper agent

## Overview

`kind: framing`

Replace score-only synchronous memory recall with an LLM-judgment-driven backgrounded Haiku helper agent. The helper runs in parallel with the existing fast vector recall on every `turn_start`, takes a holistic view of the parent's session digest + prompt, runs iterative `search_memories` calls, and either `send_message`s 0–3 selected memories back to the parent or finishes silently. Existing fast vector recall stays in place as the immediate baseline; the helper supplements it with smarter selections delivered at the parent's next `turn_start` via the existing inter-session messaging rule (with its `is_spawned_agent` gate dropped so parents receive too). On every parent `turn_start`, three rules fire in priority order: cancel any in-flight helper from a prior turn (priority 5, via a new `cancel_stale_helpers` MCP tool); deliver pending P2P messages with cross-source dedup and a cancelled-session filter (priority 10); spawn a fresh helper for the new prompt (priority 12). The strict ordering guarantees that no stale helper output ever lands on an unrelated later prompt.

## Constraints

`kind: framing`

- The synchronous fast-recall path (`memory-recall-on-prompt`) and the rolling digest pipeline (`digest-on-response`) are out of scope — they continue unchanged. The helper consumes the digest produced at `turn_end` of the previous turn; it never produces it.
- Helper must run backgrounded. Adding LLM latency to `turn_start` is unacceptable.
- `PreToolUse` (`before_tool`) does not fire on text-only assistant turns in Claude Code, so delivery happens at the next `turn_start` only.
- **Dedup tracking is on the parent's delivery side, on the inline `inject_result` path inside `_apply_effect` (`src/gobby/workflows/engine/effects.py:57-255`), NOT in `HookManager._evaluate_workflow_rules`.** The hook-manager dedup loop (`_dedup_memory_results` at `src/gobby/hooks/hook_manager.py:490` on HEAD) only runs on deferred `dispatch_result` items; the `inject_result: true` path is inline-dispatched directly inside `_apply_effect` and never produces a dispatch_result. Phase 2.4 implements the full delivery-time pipeline (normalize → drop-cancelled-from-session → dedup → strip handled messages → format) on the inline path so it actually fires for `deliver_pending_messages` results.
- **Helper is read-only on `injected_memory_ids`.** Helper reads it before selecting (to avoid re-surfacing already-seen memories) but never writes. Writing happens in 2.4 via `SessionVariableManager.append_to_set_variable` — the existing atomic primitive `_dedup_memory_results` uses. This eliminates the round-1 race where IDs got marked injected before the parent ever saw them and the read-modify-write loss between concurrent writers. The read-only contract is enforced at the runtime layer by 2.2, which makes agent `blocked_tools` override the default infrastructure-tool exempt so the helper's `mcp__gobby__set_variable` calls are actually blocked (the `is_infrastructure_tool` exempt path in `_check_agent_tool_enforcement` (`src/gobby/workflows/engine/enforcement.py`) currently returns before any block-list is consulted, which makes `blocked_mcp_tools` and a naive `blocked_tools` placement non-functional for proxy infra tools).
- **Same-turn cross-source dedup is owned by the inline `inject_result` path, keyed off the canonical platform session id.** Both fast-recall (`memory-recall-on-prompt`, priority 10) AND helper-delivery (`deliver-pending-messages`, priority 10 firing on the next turn) inject memories on `turn_start`. Without explicit handling, the same memory id can render twice in the same turn — once from fast recall's inline `search_memories` formatter, once from helper-delivery's `memory_recall` payload — because fast recall today does NOT consult or write `injected_memory_ids` on the inline path. Phase 2.4 places dedup-against-and-append-to `injected_memory_ids` inside the `_apply_effect` pipeline for BOTH `("gobby-memory", "search_memories")` AND `("gobby-agents", "deliver_pending_messages")`. **Both formatters MUST resolve the session id via `event.metadata.get('_platform_session_id')`**, NOT `event.session_id`. `HookEvent.session_id` is the CLI external id (Claude `external_id`, Codex `thread_id`); the canonical Gobby session row uses the platform session id, and `SessionVariableManager` is keyed by it. The existing deferred memory-dedup (`_dedup_memory_results` at `src/gobby/hooks/hook_manager.py:490` on HEAD) and `HookManager`'s rule-evaluation paths read `_platform_session_id` (HEAD references at `hook_manager.py:312, 400, 490`) for exactly this reason. If the formatters used `event.session_id`, fast recall and helper delivery would write `injected_memory_ids` under the wrong key and same-turn/session dedup would silently fail. Both writers go through `SessionVariableManager.append_to_set_variable`, race-free across concurrent rule evaluations.
- **Freshness contract: three independent guards.** A backgrounded helper for prompt N can produce a `memory_recall` payload that lands in the inter-session message queue at one of three times relative to prompt N+1's `turn_start`: (i) before N+1's spawn fires → in-flight, gets cancelled; (ii) before N+1's deliver runs → message in queue from a `success` run, intended delivery; (iii) after N+1's deliver runs → message in queue from a `success` run, missed its window, will deliver at N+2 against an unrelated prompt — STALE. Cancellation alone catches (i) but does NOT catch (iii) because the source run's status is `success`, not `cancelled`. Three guards together close the hole: **(A) Cancellation guard.** A dedicated `cancel-stale-memory-recall-helpers` rule (3.2) at priority 5 invokes a new MCP tool `cancel_stale_helpers(parent_session_id, agent_name)` (added in 2.5). The cancel rule's effect uses `inject_result: true` purely as a sync marker so it is inline-awaited before priority-10 delivery (per `EffectsMixin._apply_effect`, only `inject_result: true` non-background effects are awaited inline; everything else is queued and dispatched after the workflow handler returns — too late). 2.4's delivery formatter has a dedicated `cancel_stale_helpers` formatter case that returns None so the cancel call injects no context. After cancellation, 2.4's delivery formatter drops queued messages whose `from_session` belongs to a cancelled run via `LocalAgentRunManager.get_cancelled_session_ids` (added in 2.3). **(B) Turn-sequence guard.** A monotonic per-parent session variable `parent_turn_seq` (seeded `0` at session_start in 1.3; incremented at every parent `turn_start` by a new priority-1 rule in 3.4) gives every turn a unique number. The 3.3 spawn rule's helper prompt includes the current `parent_turn_seq` value (the helper is spawned at this turn, so its intended-delivery turn is `parent_turn_seq + 1`). The helper instructions (1.4) require including `"origin_turn_seq": <int>` in every `memory_recall` payload. 2.4's delivery formatter drops payloads where `payload.origin_turn_seq != current_parent_turn_seq - 1` — i.e., not from the immediately previous turn. This catches the (iii) case: a `success`-status helper from turn N whose message lands at N+2 has `origin_turn_seq=N`, but `current_parent_turn_seq - 1 = N+1`, so the payload is dropped. **(C) Cancel-incomplete guard.** After the priority-5 cancel rule fires, 2.4's `_format_delivery_result` queries whether any `memory-recall-helper` run for the parent remains `pending` or `running` via `LocalAgentRunManager.list_by_parent`. If any do, the formatter sets a `cancel_incomplete` flag and drops all `memory_recall` payloads with a warning. This catches a gap neither guard A nor guard B covers on the immediate next turn: on turn N+1, a helper spawned at turn N has `origin_turn_seq=N`, and after the priority-1 increment `current_parent_turn_seq - 1 = N`, so guard B accepts it; the run was never cancelled (the cancel MCP call failed or returned best-effort errors), so guard A also accepts it. Guard C closes this gap by detecting the still-running state directly and failing closed. The fail-closed posture matches the cancelled-session lookup failure handling — if the still-running check itself raises, the flag is also set. Guard B catches the multi-turn stale case (payloads from turn N at turn N+2 or later, where `current - 1 > N`); guard C catches the single-turn failed-cancel case. **All three guards together** ensure: at every parent turn_start, the order is **increment turn-seq (1) → cancel stale (5) → deliver pending with all three filters (10) → spawn fresh (12)**, and no helper output ever injects against an unrelated later prompt regardless of whether the prior helper was cancelled-mid-run, completed-too-late, or failed to cancel.

**Fail-open posture on cancel error (rule level).** If `cancel_stale_helpers` returns `success: False` (e.g., transient daemon error or a partial best-effort failure), `_apply_effect` aborts ONLY the cancel rule's remaining effects — the delivery rule (different rule, same `turn_start` event, priority 10) and the spawn rule (priority 12) run independently. This is fail-open for guard A at the rule level: a stale helper may keep running into the next turn. Guard C (cancel-incomplete) is the safety net for the immediate-next-turn case: after the cancel call returns, the delivery formatter checks whether any `memory-recall-helper` run for the parent is still `pending` or `running`; if so, all `memory_recall` payloads are dropped. Guard B alone cannot catch a still-running helper on the immediate next turn — a helper from turn N has `origin_turn_seq=N`, and on turn N+1 `current_parent_turn_seq - 1 = N`, so guard B accepts it. Guard B catches the multi-turn stale case (payloads from turn N at turn N+2 or later); guard C catches the single-turn failed-cancel case. The three guards are intentionally non-redundant — guard A minimizes work (cancel fast so the helper stops burning tokens), guard C is the single-turn correctness backstop, and guard B is the multi-turn staleness backstop. Note: this design replaces the rejected round-1 design that added a `supersede: bool` to `spawn_agent`; supersede couldn't address the rule-priority race (delivery at 10 fired before the spawn rule at 12 had a chance to cancel) AND `spawn_agent`'s factory does not have access to the lifecycle/process-kill deps that proper cancellation requires.
- **Fail-closed posture on cancelled-session lookup failure (formatter level).** Distinct from the rule-level fail-open above: if `LocalAgentRunManager.get_cancelled_session_ids` raises inside 2.4's `_format_delivery_result` (e.g., Postgres connection error during the formatter's DB query), the formatter sets a `cancelled_lookup_failed` flag and drops ALL `memory_recall` payloads in that delivery with a warning log. Non-memory_recall messages from the same delivery are unaffected (they fall through to `other_messages`). The rationale: guard B alone is insufficient here because a DB-cancelled helper whose `origin_turn_seq` matches `parent_turn_seq - 1` would pass the turn-seq check — the payload was legitimately fresh at send_message time, but the run was cancelled (by the priority-5 rule) before the formatter reads the queue. Without the lookup, the formatter cannot distinguish this payload from a valid fresh delivery. Fail-closed preserves the freshness contract invariant: no unverifiable helper output ever injects. The cost is that a transient DB error during `_format_delivery_result` drops one delivery's helper memories — the next turn re-fires delivery with a fresh lookup. The distinction between the two levels: the *rule* level (cancel_stale_helpers MCP tool call) affects whether a stale helper *keeps running*; the *formatter* level (get_cancelled_session_ids DB query) affects whether a queued payload from an *already-cancelled* helper is *injected*. The former can fail open because guard C (cancel-incomplete, added in round 10) catches the correctness gap by checking for still-running helpers at delivery time; the latter must fail closed because the very data guard A needs to make its decision is unavailable.
- **Empty pending-message queues must be no-op injections.** Without explicit handling, `inject_result: true` would inject `{"success": true, "messages": [], "count": 0}` as visible JSON on every routine turn where no helper has anything to surface. Phase 2.4's pipeline includes an early empty-payload short-circuit so the inline path skips injection cleanly.
- **First-time deliveries must render each helper memory exactly once.** Without explicit handling, `inject_result` would dump the raw `messages[*].content` AND the normalized top-level `memories`, so a fresh memory would render twice on the first delivery. Phase 2.4's pipeline strips handled `memory_recall` messages out of the `messages` array before formatting, so rendered output contains the deduped helper memories once and any non-`memory_recall` messages still passing through.
- **The existing `deliver-pending-messages` rule needs an explicit `arguments: { target_session_id: "{{ event.metadata.get('_platform_session_id') }}" }` block.** The dispatcher does not auto-inject `target_session_id` (only `session_id`, which `deliver_pending_messages` does not accept). The current rule, with no `arguments`, would not actually invoke the tool successfully. The templated value MUST resolve via `event.metadata['_platform_session_id']` (the canonical Gobby session row id), NOT `event.session_id` (the CLI external id — Claude `external_id` / Codex `thread_id`). Phase 3.1 fixes this in the rule body. The same canonical-id rule applies to `parent_session_id` in 3.2 (`cancel-stale-memory-recall-helpers`) and 3.3 (`spawn-memory-recall-helper` — both the `arguments.parent_session_id` field AND the helper prompt's `Parent session:` line).
- **The helper sends with `from_session=<helper's own child session id>`.** `send_message`'s schema requires `from_session`. The helper does not know its child session id at prompt-construction time (the spawn rule cannot capture the spawn return value because `background: true`). Phase 2.1 makes `from_session` optional in `send_message` and defaults it from `SessionContext` (the proxy's session-context header) when omitted, so callers running through the proxy do not need to know their own session id explicitly. The helper's instructions then say "omit from_session — it auto-fills from your session context."
- **Helper completion must be silent at the subscription source.** `spawn_agent` currently auto-subscribes the parent session to child-run completion whenever `parent_session_id` is set, and `end_agent_run` notifies those subscribers through the durable wake/inter-session-message path. Filtering `completion_notification` messages inside delivery is not enough because the live wake and durable row have already been created. Phase 2.6 adds a `notify_parent_on_completion: bool = True` spawn option that preserves `parent_session_id` lineage/cancellation while skipping `subscribe_agent_completion` when false. Phase 3.3 sets that option to `false` for `memory-recall-helper`, so a no-memory helper truly finishes silently and a memory-sending helper delivers only its explicit `memory_recall` payload.
- The existing `deliver-pending-messages` rule is gated on `variables.get('is_spawned_agent')`, which excludes user-facing parents. The gate must be removed; the underlying tool is session-scoped, so removing it does not cross-contaminate sessions.
- Helper must be hard-bounded: `max_turns: 3`, `timeout: 60s`. `AgentLifecycleMonitor` enforces both. These values live in the helper YAML, not in user-tunable config — the only runtime configurable for this feature is the `enabled` master kill-switch.
- Helper's `prompt` must contain dynamic per-turn content (parent_session_id, the parent's user prompt). `spawn_agent` has no separate `inputs:` parameter — everything dynamic is composed into the `prompt` string. Static instructions live on the agent definition.
- One new MCP tool: `gobby-agents.cancel_stale_helpers` (added in 2.5; consumed by the new 3.2 cancel rule) and one new `spawn_agent` option: `notify_parent_on_completion` (added in 2.6; consumed by the 3.3 spawn rule). No new prompt-template files. The helper itself uses existing `gobby-memory.search_memories`, `gobby-agents.send_message`, `gobby-sessions.get_session`, top-level `get_variable`, and `gobby-agents.end_agent_run`. The new cancel tool and silent-completion option are internal wiring — users do not need to know about them.
- The runtime master kill-switch is `DaemonConfig.memory_recall_helper.enabled`. It must be readable from the spawn rule's `when:` clause via a session variable seeded at `session_start` from the daemon's loaded config (rules cannot read `DaemonConfig` directly — `_build_eval_context` at `src/gobby/workflows/engine/templating.py:36–105` exposes only `event`, `variables`, `tool_input`, `source`, `project`).

## P1 Phase 1: Foundation

`kind: framing`

**Goal**: Add the helper agent's master-toggle config, thread it through `EventHandlers` so its `enabled` flag is seeded into every new session as a variable, and create the helper's YAML definition.

Earlier rounds of this plan included a manual monolith-gate task (1.1) tracking the
external `_session_start.py` refactor (originally filed as #12919). That refactor has
since landed: commit `ac8a4114c` ("refactor: decompose session start handler")
decomposed the single file into a `src/gobby/hooks/event_handlers/_session_start/`
package. The new file 1.3 edits is `_session_start/agents.py` (`activate_default_agent`
at lines 82–199 on HEAD), which is well under the 1,000-line monolith limit. No gate
task is required and the section that previously carried it is intentionally absent
in this revision; section IDs in P1 jump directly from the phase header to 1.2.

### 1.2 Add `MemoryRecallHelperConfig` (single field) to `DaemonConfig` [category: code]

`kind: deliverable`

Target: `src/gobby/config/sessions.py` (config class) and `src/gobby/config/app.py` (`DaemonConfig` field).

Add a minimal `MemoryRecallHelperConfig` (Pydantic `BaseModel`, NOT extending `FeatureDefaultConfig`) with a single `enabled: bool` field, and attach it to `DaemonConfig` as a sibling of the existing `digest: DigestConfig = Field(...)` field (in `src/gobby/config/app.py` around lines 292–295 on HEAD). The helper's model, timeouts, and search-tuning values are intentionally hardcoded in the helper agent YAML (1.4) — they are not user-tunable and adding orphan config fields would just be dead surface.

In `src/gobby/config/sessions.py`, add the class right after `DigestConfig` (lines 140–151 on HEAD, ending at line 151):

```python
class MemoryRecallHelperConfig(BaseModel):
    """Backgrounded Haiku memory-recall helper agent runtime toggle."""

    enabled: bool = Field(
        default=True,
        description="Enable the backgrounded LLM-driven memory recall helper agent.",
    )
```

`BaseModel` is the right base here; we are not exposing provider/model/tier overrides because the helper's runtime values are pinned in its YAML definition (1.4). If a future requirement exposes any of those for tuning, it can extend this class then.

Then in `src/gobby/config/app.py`, in the `DaemonConfig` class (around lines 292–295 on HEAD), add immediately after the `digest: DigestConfig = Field(...)` declaration:

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

**Acceptance:**

- 1.2.1 — `MemoryRecallHelperConfig` Pydantic `BaseModel` with single `enabled: bool` field defaulting to `True`. symbol: `gobby.config.sessions.MemoryRecallHelperConfig`.
- 1.2.2 — `DaemonConfig.memory_recall_helper` field present with `default_factory=MemoryRecallHelperConfig`. symbol: `gobby.config.app.DaemonConfig.memory_recall_helper`.
- 1.2.3 — Default-construct, YAML round-trip, and exact-field-set tests cover the config shape. test: `tests/config/test_sessions.py::test_memory_recall_helper_config_shape`.

### 1.3 Thread `memory_recall_helper` config to `EventHandlers` and seed `memory_recall_helper_enabled` on session_start [category: code] (depends: 1.2)

`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_base.py` (`EventHandlersBase` — add typed slot for the config)
- `src/gobby/hooks/event_handlers/__init__.py` (`EventHandlers.__init__` — accept and store the config)
- `src/gobby/hooks/factory.py` (factory call site that constructs `EventHandlers(...)` — pass `config.memory_recall_helper`; the call site assigns `skills_config=config.skills if config else None` and the new keyword sits next to that pattern)
- `src/gobby/hooks/event_handlers/_session_start/agents.py` (`_seed_memory_recall_helper_vars` — module-level helper function defined here)
- `src/gobby/hooks/event_handlers/_session_start/flow.py` (`handle_session_start` and `handle_pre_created_session` — call `_seed_memory_recall_helper_vars` at the flow level before any activation guard)
- `tests/hooks/event_handlers/test_session_variable_preservation.py` (new preservation + fresh-session test cases for `parent_turn_seq`)

Mirror the existing `skills_config: SkillsConfig | None` pattern (`src/gobby/hooks/event_handlers/_base.py:37`, `__init__.py:108`, `factory.py:254`).

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

In `src/gobby/hooks/event_handlers/__init__.py`, in `EventHandlers.__init__` (line 52–118 on HEAD), add a new keyword param and assignment paralleling `skills_config` (current assignment lives at line 108):

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

In `src/gobby/hooks/factory.py` at the `EventHandlers(...)` construction (line 245–260 on HEAD; the `skills_config=...` assignment is at line 254), add the keyword argument right after `skills_config`:

```python
event_handlers = EventHandlers(
    # ... existing args ...
    skills_config=config.skills if config else None,
    memory_recall_helper_config=config.memory_recall_helper if config else None,
    workflow_config=config.workflow if config else None,
    # ... existing args ...
)
```

In `src/gobby/hooks/event_handlers/_session_start/agents.py`, in `activate_default_agent` (lines 82–199 on HEAD; module-level function, **not** a mixin method — first parameter is `handler: Any`, called from `flow.py` at lines 211 and 399 via `handler._activate_default_agent(...)` which is the mixin shim defined in `_session_start/__init__.py`).

**Critical: seeding MUST happen at the flow level, not inside `activate_default_agent`.** There are TWO layers that can skip seeding: (A) the flow-level `skip_default_agent_activation` guard in `flow.py:208` (`handle_session_start`) that prevents `_activate_default_agent` from being called at all — web chat sets this flag for persona-selected sessions (see Constraints for the reference); and (B) the within-function early exits inside `activate_default_agent` itself (session_manager is None, default_agent_name == "none", agent not found). Round 9 addressed layer (B) by placing seeding at the top of `activate_default_agent` before its early exits. But layer (A) bypasses the entire function — sessions where `skip_default_agent_activation` is set never call `activate_default_agent` at all, so the Round 9 seeding never runs. Without the seed, §3.4's fail-closed counter guard correctly blocks (no self-creation), but the parent task's "runs on every turn_start" requirement is silently disabled for an existing supported session-start path (web chat persona-selected sessions).

**Fix: define the seeding helper in `agents.py`, call it from `flow.py` at the flow level.** Define `_seed_memory_recall_helper_vars(handler, session_id)` as a module-level function in `agents.py`. Call it from `flow.py` in BOTH session-start paths — `handle_session_start` (before the `skip_default_agent_activation` guard) and `handle_pre_created_session` (before `_activate_default_agent`) — so that every session with a valid `session_id` and `session_manager` gets seeded regardless of whether agent activation runs. Remove the call from inside `activate_default_agent` — the flow owns seeding exclusively.

New module-level function in `agents.py`, placed before `activate_default_agent`:

```python
def _seed_memory_recall_helper_vars(handler: Any, session_id: str) -> None:
    """Seed memory-recall-helper variables at the flow level.

    Called from flow.py's handle_session_start and handle_pre_created_session
    BEFORE any activation guard or early exit. This ensures every session
    with a valid session_id and session_manager gets the seed, including
    sessions where agent activation is skipped entirely (e.g., web chat
    persona-selected sessions set skip_default_agent_activation=True in
    flow.py:208, bypassing _activate_default_agent).

    Preservation semantics:
    - `memory_recall_helper_enabled` is ALWAYS re-applied (config may have
      changed on restart).
    - `parent_turn_seq` is seeded to 0 ONLY if absent (preserve the
      runtime-incremented counter across compact/restart).
    """
    from gobby.workflows.state_manager import SessionVariableManager

    sv_mgr = SessionVariableManager(handler._session_manager.db)
    helper_cfg = handler._memory_recall_helper_config
    enabled_val = bool(helper_cfg.enabled) if helper_cfg is not None else True

    existing = sv_mgr.get_variables(session_id)
    seed: dict[str, Any] = {"memory_recall_helper_enabled": enabled_val}  # `Any` already imported at module top
    if "parent_turn_seq" not in (existing or {}):
        seed["parent_turn_seq"] = 0
    sv_mgr.merge_variables(session_id, seed)
```

In `flow.py`, import the helper at the top of the module:

```python
from gobby.hooks.event_handlers._session_start.agents import _seed_memory_recall_helper_vars
```

In `handle_session_start` (flow.py), insert the call BEFORE the `skip_default_agent_activation` guard (around line 205, after the `workflow_name` check and before `_t_activate`). The call is conditioned only on `session_id` and `handler._session_manager` — NOT on `skip_default_agent_activation`:

```python
    # Seed memory-recall-helper variables at the flow level, unconditionally.
    # This MUST run before the skip_default_agent_activation guard below,
    # because web chat persona-selected sessions set that flag (bypassing
    # _activate_default_agent entirely). Without flow-level seeding, those
    # sessions never get memory_recall_helper_enabled or parent_turn_seq,
    # and the helper feature is silently disabled for an existing supported
    # session-start path.
    if session_id and handler._session_manager is not None:
        try:
            _seed_memory_recall_helper_vars(handler, session_id)
        except Exception as e:
            handler.logger.warning(f"Failed to seed memory-recall-helper vars: {e}")

    _t_activate = time.monotonic()
    agent_result: AgentActivationResult | None = None
    if session_id and not input_data.get("skip_default_agent_activation"):
        try:
            agent_result = handler._activate_default_agent(...)
        except Exception as e:
            ...
```

In `handle_pre_created_session` (flow.py), insert the call before the `_activate_default_agent` call (around line 394, after `handler._setup_code_index`):

```python
    # Seed memory-recall-helper variables at the flow level.
    if handler._session_manager is not None:
        try:
            _seed_memory_recall_helper_vars(handler, session_id)
        except Exception as e:
            handler.logger.warning(f"Failed to seed memory-recall-helper vars: {e}")

    agent_result: AgentActivationResult | None = None
    input_data = event.data if event else {}
    try:
        agent_result = handler._activate_default_agent(...)
    except Exception as e:
        ...
```

`activate_default_agent` in `agents.py` does NOT call `_seed_memory_recall_helper_vars` — the flow owns seeding exclusively. The `changes` dict path (later in the function, inside the `if existing: always_reapply` filter) does NOT include `memory_recall_helper_enabled` or `parent_turn_seq`. The `internal_keys` set still includes `"memory_recall_helper_enabled"` so `variables_count` excludes it.

Also add `"memory_recall_helper_enabled"` to the `internal_keys` set (~line 137 on HEAD; defined inside `activate_default_agent` before `variables_count` is computed) so `variables_count` continues to report only user-facing variables, not this internal flag.

Validation criteria: `EventHandlersBase._memory_recall_helper_config` exists with the `MemoryRecallHelperConfig | None` type. `EventHandlers(memory_recall_helper_config=...)` round-trips the value to `self._memory_recall_helper_config`. `factory.py` (line 245–260 on HEAD) passes `config.memory_recall_helper` when `config` is non-None and `None` otherwise. After daemon start with default config, every new session has `variables.get("memory_recall_helper_enabled") == True` AND `variables.get("parent_turn_seq") == 0`. Setting `memory_recall_helper.enabled: false` in the config YAML and restarting the daemon causes new sessions to have `variables.get("memory_recall_helper_enabled") == False`. Existing sessions on compact/restart re-apply `memory_recall_helper_enabled` (because `_seed_memory_recall_helper_vars` always writes it) but DO NOT reset `parent_turn_seq` (because the helper only seeds it when absent from `existing`). **Preservation test (required, not optional)**: a test that exercises the actual `activate_default_agent` flow end-to-end — set `parent_turn_seq=42` on a session via `SessionVariableManager.merge_variables`, then trigger another `activate_default_agent` call for that session, then read back via `SessionVariableManager.get_variables` and assert `parent_turn_seq == 42`. Add the test alongside the existing variable-merge coverage under `tests/hooks/event_handlers/` (e.g. `test_session_variable_preservation.py`, which already exercises the merge filter via the same fixtures) so it actually goes through the filter logic, not a stub. **Fresh-session test**: simulate a first activation where `existing` may already contain definition defaults but does NOT contain `parent_turn_seq`. Trigger `activate_default_agent` and assert `parent_turn_seq == 0` after the call (the seed write reaches `merge_variables` because `"parent_turn_seq" not in existing`, regardless of whether `existing` is otherwise empty or contains defaults). The condition that matters is "key absent from `existing`", NOT "`existing` is empty" — the latter is rarely true at HEAD because `get_variables()` merges definition defaults. Both new tests must fail if `parent_turn_seq` is incorrectly seeded unconditionally (which would clobber preservation), and must fail if the seed helper is removed (which would leave the variables unseeded on activation-skipped sessions). **Skipped-activation test (required, not optional — round-12 F1 guard)**: fire `handle_session_start` with `input_data["skip_default_agent_activation"] = True` (the flag web chat sets for persona-selected sessions — see Constraints). This causes `flow.py:208` to skip `_activate_default_agent` entirely. Assert: (a) `variables.get("memory_recall_helper_enabled")` is `True` (or the configured value), (b) `variables.get("parent_turn_seq")` is `0`, (c) `_activate_default_agent` was NOT called (activation was skipped at the flow level). This proves the flow-level `_seed_memory_recall_helper_vars` call runs before the `skip_default_agent_activation` guard. Without this test, a regression that moves seeding back into `activate_default_agent` would silently leave persona-selected web chat sessions unseeded. Additionally, test the `handle_pre_created_session` path: fire it and assert both variables are seeded before `_activate_default_agent` runs (this path always calls activation, but seeding must happen at the flow level, not inside the activation function).

**Acceptance:**

- 1.3.1 — `EventHandlersBase._memory_recall_helper_config` typed slot exists. symbol: `gobby.hooks.event_handlers._base.EventHandlersBase`.
- 1.3.2 — `EventHandlers.__init__` accepts `memory_recall_helper_config` and round-trips it to the instance. symbol: `gobby.hooks.event_handlers.EventHandlers.__init__`.
- 1.3.3 — `factory.py` passes `config.memory_recall_helper` to `EventHandlers` on construction. file: `src/gobby/hooks/factory.py`.
- 1.3.4 — `_seed_memory_recall_helper_vars` module-level helper exists in `agents.py` and is called from `flow.py` at the flow level in both `handle_session_start` (before the `skip_default_agent_activation` guard) and `handle_pre_created_session` (before `_activate_default_agent`). NOT called from inside `activate_default_agent`. symbol: `gobby.hooks.event_handlers._session_start.agents._seed_memory_recall_helper_vars`.
- 1.3.5 — `_seed_memory_recall_helper_vars` always re-applies `memory_recall_helper_enabled` and seeds `parent_turn_seq` only when absent from `existing`. file: `src/gobby/hooks/event_handlers/_session_start/agents.py`.
- 1.3.6 — `internal_keys` filter contains `memory_recall_helper_enabled` so `variables_count` excludes the internal flag. test: `tests/hooks/event_handlers/test_session_variable_preservation.py::test_internal_keys_excludes_memory_recall_helper_enabled_from_variables_count`.
- 1.3.7 — Preservation test asserts `parent_turn_seq=42` survives a second `activate_default_agent` call end-to-end. test: `tests/hooks/event_handlers/test_session_variable_preservation.py::test_parent_turn_seq_preserved_across_activation`.
- 1.3.8 — Fresh-session test asserts `parent_turn_seq=0` is seeded on first activation when the key is absent from `existing` (regardless of whether `existing` is otherwise empty). test: `tests/hooks/event_handlers/test_session_variable_preservation.py::test_parent_turn_seq_seeded_on_first_activation`.
- 1.3.9 — Flow-level skipped-activation test: when `handle_session_start` fires with `skip_default_agent_activation=True` (the web chat persona-selected path), `memory_recall_helper_enabled` and `parent_turn_seq` are still seeded by the flow-level call before the guard. test: `tests/hooks/event_handlers/test_session_variable_preservation.py::test_variables_seeded_when_activation_skipped_at_flow_level`.
- 1.3.10 — Pre-created session seeding test: `handle_pre_created_session` seeds both variables at the flow level before calling `_activate_default_agent`. test: `tests/hooks/event_handlers/test_session_variable_preservation.py::test_variables_seeded_in_pre_created_session_flow`.

### 1.4 Create `memory-recall-helper` agent definition [category: config]

`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/memory-recall-helper.yaml` (new file — primary deliverable).
- `tests/agents/test_sync.py` (add bundled-agent sync test case for the new template).
- `tests/workflows/test_agent_resolver.py` (add resolver test case for the new agent name).

This deliverable validates the helper YAML contract only. Runtime enforcement of `blocked_tools` (the `mcp__gobby__set_variable` denial that makes the helper's read-only `injected_memory_ids` contract effective) is owned by §2.2 and tested there — see §2.2.4. Splitting the test ownership this way avoids a cross-phase dependency: §1.4 can be expanded and validated independently of §2.2 because it asserts only the static YAML shape, not runtime behavior that §2.2 enables.

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
         target    = "session"
         target_id = <parent_session_id>
         content   = JSON-encoded string with this exact shape:
                     {"type": "memory_recall",
                      "origin_turn_seq": <integer from your prompt>,
                      "memories": [<full memory records>],
                      "rationale": "<one short sentence>"}
     OMIT the `from_session` argument — the proxy auto-fills it from your
     session context (this is the runtime change made in 2.1). Note that
     HEAD's `send_message` signature is `(from_session, target, content,
     target_id=None, *, priority, include_wakeup, message_type, metadata)`,
     so a session-scoped delivery requires `target="session"` AND
     `target_id=<parent_session_id>`. Do NOT use `to_session` — there is
     no such parameter on this tool. Each memory record MUST include `id`
     so the parent's delivery-side dedup can track it. The literal string
     "memory_recall" in the `type` field is what 2.4's normalization
     pipeline keys off; do not use a different value. The `origin_turn_seq`
     field MUST be the integer you were given in the spawn prompt — copy
     it verbatim. The parent's delivery formatter uses it to drop payloads
     that miss their delivery window (see Constraints / freshness contract
     guard B).

  7. If nothing is clearly relevant, do NOT call send_message. Skip
     straight to step 8.

  8. Call gobby-agents.end_agent_run with no arguments to exit cleanly.
     Without this call you will idle until max_turns (3) is reached,
     burning two extra Haiku round-trips per spawn for no value. Call
     it whether or not you sent a message in step 6 — end_agent_run is
     the canonical exit primitive for backgrounded agents. The spawn
     rule sets `notify_parent_on_completion: false`, so this clean exit
     does NOT create a parent completion notification; only your explicit
     `memory_recall` send_message can reach the parent. Skipping this
     step is the most common source of helper-budget waste; do not skip it.

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
  - ALWAYS finish by calling end_agent_run (step 8 above). The agent
    has no exit_condition in YAML; without an explicit end_agent_run
    you will hit max_turns and waste up to two unnecessary turns.

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

The helper instructions tell it to read the parent's session digest via `gobby-sessions.get_session(session_id=<parent_session_id>)` and consume the `digest_markdown` field. This is verified on HEAD: `Session.to_dict()` at `src/gobby/storage/session_models.py:216-247` includes `digest_markdown` (line 247), and `get_session` (`src/gobby/mcp_proxy/tools/sessions/_crud.py:38-74`) returns `**session.to_dict()` so the field reaches the helper unchanged. If a future refactor strips `digest_markdown` from `Session.to_dict()`, the helper's read returns None and gracefully falls back to query-only semantic search per its instructions (step 1: "It may be empty for fresh sessions — that's fine.").

Definition-load verification: after daemon restart, run `gobby agents list` and confirm `memory-recall-helper` appears in the output (per the post-cleanup CLI: `gobby agents list/show` inspect agent **definitions**, `gobby agents runs list/show` inspect runs). Then run `gobby agents show memory-recall-helper --json` and assert the returned JSON's `model` field equals `"claude-haiku-4-5"`, `max_turns` equals `3`, `timeout` equals `60`, and `blocked_tools` includes `"mcp__gobby__set_variable"`. Equivalent direct-DB check (use either, prefer the CLI for human verification): query `workflow_definitions` for `name='memory-recall-helper' AND workflow_type='agent'` and confirm exactly one row with `enabled=1` and matching definition_json fields. Add a test case to `tests/agents/test_sync.py` asserting that after `sync_bundled_agents` runs against this template, the DB row exists with the documented fields. Add a test case to `tests/workflows/test_agent_resolver.py` asserting `resolve_agent("memory-recall-helper", db)` returns a non-None body whose `model`, `max_turns`, `timeout`, and `blocked_tools` match the documented values.

The helper's `instructions` block contains explicit "OMIT the `from_session` argument", "Do NOT write to injected_memory_ids", "ALWAYS include `origin_turn_seq`", and the literal strings `"memory_recall"` and `"origin_turn_seq"` in the documented JSON content shape. The runtime blocking behavior — that a spawned helper attempting top-level `mcp__gobby__set_variable` is denied with a `[agent-enforcement:memory-recall-helper]` reason — is owned and tested by §2.2 (specifically §2.2.4, which loads this helper's YAML and asserts the call is blocked). §1.4 validates only the YAML contract (the listing of `mcp__gobby__set_variable` in `blocked_tools`); the runtime contract is §2.2's concern.

**Acceptance:**

- 1.4.1 — `memory-recall-helper.yaml` exists at the canonical bundled-agent path. file: `src/gobby/install/shared/workflows/agents/memory-recall-helper.yaml`.
- 1.4.2 — YAML parses against `AgentDefinitionBody` with `model: claude-haiku-4-5`, `max_turns: 3`, `timeout: 60`, `blocked_tools` containing `mcp__gobby__set_variable`, and empty `blocked_mcp_tools`. symbol: `gobby.workflows.definitions.AgentDefinitionBody`.
- 1.4.3 — Bundled-agent sync test asserts the row appears in `workflow_definitions` with `enabled=1` after `sync_bundled_agents` runs. test: `tests/agents/test_sync.py::test_memory_recall_helper_synced`.
- 1.4.4 — Agent-resolver test asserts `resolve_agent("memory-recall-helper", db)` returns the documented body. test: `tests/workflows/test_agent_resolver.py::test_resolve_memory_recall_helper`.

## P2 Phase 2: Runtime correctness fixes (pre-wiring)

`kind: framing`

**Goal**: Six targeted runtime changes that make Phase 3 wiring correct out of the box: (2.1) auto-fill `from_session` on `send_message` so the helper does not need its own child session id; (2.2) reorder `_check_agent_tool_enforcement` so explicit `blocked_tools` listings override the infrastructure-tool exempt and the helper's read-only contract is actually enforced; (2.3) add `_AgentRunQueryMixin.get_cancelled_session_ids` (composed into `LocalAgentRunManager`) for the delivery filter to reference; (2.4) implement helper-aware delivery + same-turn cross-source dedup on `_apply_effect`'s inline `inject_result` path — applies all freshness guards (cancelled-session, `origin_turn_seq` matches `parent_turn_seq - 1`, and cancel-incomplete), dedupes against `injected_memory_ids` keyed by `_platform_session_id`, includes a no-op formatter case for `cancel_stale_helpers` that returns None (so the cancel rule's `inject_result: true` sync marker injects no context), and renders helper memory payloads exactly once; (2.5) add a `cancel_stale_helpers` MCP tool sharing `stop_agent`'s lifecycle path via an extracted `_stop_run` helper so the priority-5 cancel rule has a correctly-wired cancellation primitive that performs the full process-kill + terminalize + terminal-cleanup chain; (2.6) add a `notify_parent_on_completion` spawn option so the memory helper can keep parent lineage/cancellation without durable completion-notification noise. All six come BEFORE the wiring (Phase 3) so the wiring works correctly the first time the helper actually runs end-to-end.

**Phase 2 entry criteria (operational, not DB-enforced):** Phase 2 has no hard cross-phase dependency on Phase 1's CODE changes — it touches different files (`mcp_proxy/tools/agent_messaging.py`, `workflows/engine/enforcement.py`, `storage/agents.py`, `workflows/engine/effects.py`, `mcp_proxy/tools/agents.py`, `mcp_proxy/tools/spawn_agent/_factory.py`). Phase 2 tasks may be claimed and worked in parallel with Phase 1's later sections. The cross-phase `(depends: ...)` annotations seen in earlier rounds of this plan have been removed — the current task expander does NOT deterministically emit cross-phase `tasks.dependencies` edges from header annotations (round-8 adversary finding), so they were misleading rather than load-bearing. Coordination between Phase 1 and Phase 2 happens at PR-merge time and via the conductor's task ordering, not via DB dependency edges. The implementer is responsible for not merging Phase 3 until both Phase 1 and Phase 2 are complete (see Phase 3 entry criteria).

**Intra-phase dependencies inside Phase 2** (which the expander DOES handle reliably for same-phase deps): 2.4 depends on 2.1 (uses 2.1's `from_session` auto-fill semantics) and on 2.3 (uses the storage helper). 2.5 depends on 2.3. 2.6 has no same-phase dependency; it is a local `spawn_agent` factory contract change. These same-phase deps are encoded in the section headers below.

### 2.1 Default `from_session` on `send_message` from SessionContext when omitted [category: code]

`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/agent_messaging.py` (the `send_message` function at lines 115–220 on HEAD; defined inside the `add_messaging_tools` factory closure — primary edit + its registered MCP schema).
- `tests/mcp_proxy/tools/test_agent_messaging.py` (new test cases for the default-fill path, no-context error path, and positional-caller guard).
- `tests/e2e/test_inter_agent_messages.py` (regression coverage — confirm `test_parent_child_message_exchange` still passes after the parameter reorder).

Today `send_message` requires `from_session: str` as the first positional parameter. The full HEAD signature is:

```python
async def send_message(
    from_session: str,
    target: str,
    content: str,
    target_id: str | None = None,
    *,
    priority: str = "normal",
    include_wakeup: bool = False,
    message_type: str = "message",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

`target` is an enum (`"all"`, `"session"`, …); `target_id` carries the specific session id when `target == "session"`. There is no `to_session` parameter. For a spawned helper, the helper does not know its own child session id at prompt-construction time (the spawn rule cannot capture `child_session_id` from the spawn return value because the spawn is `background: true`). We have two options: (a) ask the helper to look itself up at runtime, (b) make `from_session` optional at the tool boundary and default it from the proxy's `SessionContext` (which the MCP proxy already populates from the calling session's `X-Gobby-Session-Id` header — see the `mcp__gobby__call_tool` docstring: "Propagated to the daemon via X-Gobby-Session-Id header so tools can read it from the SessionContext ContextVar").

Choose (b) — it generalizes to any future caller running through the proxy and matches the existing pattern used by other gobby MCP tools.

Concrete change to `send_message`:

```python
# In src/gobby/mcp_proxy/tools/agent_messaging.py, send_message function:
async def send_message(
    target: str,
    content: str,
    target_id: str | None = None,
    from_session: str | None = None,   # was: from_session: str (required positional)
    *,
    priority: str = "normal",
    include_wakeup: bool = False,
    message_type: str = "message",
    metadata: dict[str, Any] | None = None,
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

    # ... rest of function unchanged: resolve via local `_resolve(ref)` at lines
    #     80–86, validate target/target_id combination, call `mailbox.send(...)`,
    #     auto-write to agent_runs.result when sending to the parent, broadcast
    #     the agent_message event ...
```

Reordering the parameters so `from_session` becomes the fourth positional (after `target`, `content`, `target_id`) is required to keep the tool's existing keyword-only block consistent — making `from_session` keyword-only would force every existing caller to switch from positional to keyword, which is a wider blast radius than the new-default change. The MCP schema is regenerated from the function signature on tool registration, so the schema update is automatic; verify `get_tool_schema("gobby-agents", "send_message")` no longer lists `from_session` in `required` after the change.

Document the auto-fill behavior in the tool's docstring (which becomes its description in the MCP schema): "from_session defaults to the calling session's id from SessionContext when omitted."

The HEAD-correct accessor is `gobby.utils.session_context.get_current_session_id` (`src/gobby/utils/session_context.py:61` — returns the calling session's UUID from a ContextVar populated by the proxy from the `X-Gobby-Session-Id` header, or `None` if no session context is set). Do NOT use `gobby.mcp_proxy.session_context.SessionContext.get_session_id()` — that module/accessor does not exist on HEAD. Other MCP tools in `src/gobby/mcp_proxy/tools/` already use `get_current_session_id` directly; mirror that pattern.

Do NOT relax cross-session validation: the function still goes through `mailbox.send(...)`, which validates project scoping and target shape. The default just resolves the unknown, it does not bypass authorization.

**Caller-update audit (load-bearing).** Because `from_session` shifts from the first positional to the fourth positional, every call site that passes it positionally MUST be updated to keyword form or rearranged to the new positional order. Before merging this change, grep for `send_message(` across the whole tree (production + tests) and convert every positional `from_session` to keyword.

**Audit baseline (2026-05-23 HEAD)**: a grep of `src/` for `send_message(from_session` returned zero matches; no current production caller passes `from_session` positionally. The 2.1.5 guard test is therefore a forward-compat invariant, not addressing a current footgun — the implementer does not need to chase ghosts. The audit should still run because (a) test fixtures and `tests/e2e/test_inter_agent_messages.py` may have positional callers, (b) any in-flight branches could add one, and (c) the rule-engine `mcp_call` effect path passes `arguments:` as a kwargs dict (so rule YAMLs are unaffected by positional ordering — they always go through kwargs).

Validation criteria: calling `send_message(target="session", target_id="<peer>", content="hi")` from within a session context (e.g. through the proxy with `X-Gobby-Session-Id` set) succeeds with `from_session` resolved to the calling session's id, verifiable in `agent_runs.result` / `inter_session_messages` DB row. Calling the same from outside any session context returns `{"success": False, "error": "from_session is required and no SessionContext session_id is available"}` rather than crashing. Existing callers that pass `from_session` explicitly (after the caller-audit conversion) continue to work unchanged. The MCP tool schema fetched via `get_tool_schema("gobby-agents", "send_message")` no longer lists `from_session` in `required`. New test cases in `tests/mcp_proxy/tools/test_agent_messaging.py` cover both the default-fill path and the no-context error path. A grep check in `tests/mcp_proxy/tools/test_agent_messaging.py` (or a new test file) asserts no production call site under `src/` passes `from_session` as the first positional argument, guarding against partial caller updates.

**Acceptance:**

- 2.1.1 — `send_message` accepts `from_session` as optional and resolves it from `SessionContext` when omitted; `from_session` moves to the fourth positional (after `target`, `content`, `target_id`). symbol: `gobby.mcp_proxy.tools.agent_messaging.send_message`.
- 2.1.2 — Out-of-context call without `from_session` returns `{"success": False, "error": ...}` rather than crashing. test: `tests/mcp_proxy/tools/test_agent_messaging.py::test_send_message_no_session_context_returns_error`.
- 2.1.3 — `get_tool_schema("gobby-agents", "send_message")` no longer lists `from_session` as required. behavior: "send_message MCP schema marks from_session optional" in `src/gobby/mcp_proxy/tools/agent_messaging.py`.
- 2.1.4 — Default-fill path test asserts the tool resolves to the calling session's id and writes the inter-session message row with that `from_session`. test: `tests/mcp_proxy/tools/test_agent_messaging.py::test_send_message_defaults_from_session_from_context`.
- 2.1.5 — Caller audit: every call site under `src/` that passes `from_session` is updated to keyword form or to the new positional order; a guard test asserts no production caller passes `from_session` first-positional. test: `tests/mcp_proxy/tools/test_agent_messaging.py::test_no_positional_from_session_in_production_callers`.

### 2.2 Reorder `_check_agent_tool_enforcement` so `blocked_tools` overrides the infrastructure-tool exempt [category: code]

`kind: deliverable`

Targets:
- `src/gobby/workflows/engine/enforcement.py` — `EnforcementMixin._check_agent_tool_enforcement` method (primary edit).
- `tests/workflows/test_step_enforcement.py` — generic explicit-block/infra-exempt regression tests AND helper-equivalent integration test that loads the §1.4 helper YAML and asserts `mcp__gobby__set_variable` is denied (the helper-specific test was relocated from §1.4 per round-5 F1 because the runtime behavior under test is owned by this deliverable, not §1.4's YAML contract).

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

**Helper-equivalent integration test (required, not optional — relocated from §1.4 per round-5 F1).** Construct a test fixture that mirrors the `memory-recall-helper` agent's `blocked_tools` list as a local constant: `HELPER_BLOCKED_TOOLS = ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "mcp__gobby__set_variable"]` (the exact list documented in §1.4's YAML). Build a session whose `_agent_blocked_tools` variable equals this constant and `_agent_type` equals `"memory-recall-helper"`. Assert that a synthetic `before_tool` event for `mcp__gobby__set_variable` is denied at the tool-routing layer with the `[agent-enforcement:memory-recall-helper]` reason, AND that `mcp__gobby__get_variable` (NOT in the fixture's `blocked_tools`) still passes via the infra exempt. This test uses a local fixture rather than resolving the helper YAML via `resolve_agent("memory-recall-helper", db)` because Phase 2 has no encoded cross-phase dependency on Phase 1 (per Phase 2 entry criteria) and the test must be expandable and validatable independently of §1.4's merge status. The fixture mirrors the contract shape — if §1.4's YAML ever changes its `blocked_tools` list, the §1.4 leaf's own sync test (§1.4.3) catches the drift; §2.2.4's test validates the enforcement engine behavior, not the YAML content. A comment in the test body references §1.4 as the canonical source so a future reader can reconcile if the lists diverge.

**Acceptance:**

- 2.2.1 — `_check_agent_tool_enforcement` runs the explicit-block check before the infra exempt. symbol: `gobby.workflows.engine.enforcement.EnforcementMixin._check_agent_tool_enforcement`.
- 2.2.2 — With `_agent_blocked_tools=["mcp__gobby__set_variable"]`, the call is blocked with the documented `[agent-enforcement:<agent>]` reason. test: `tests/workflows/test_step_enforcement.py::test_explicit_block_overrides_infra_exempt`.
- 2.2.3 — With empty `_agent_blocked_tools`, infra tools still pass via the exempt path. test: `tests/workflows/test_step_enforcement.py::test_infra_exempt_default_when_no_explicit_block`.
- 2.2.4 — Helper-equivalent integration test: a local fixture mirroring the §1.4 helper's `blocked_tools` list produces a session that denies `mcp__gobby__set_variable` with the documented `[agent-enforcement:memory-recall-helper]` reason while still allowing `mcp__gobby__get_variable`. test: `tests/workflows/test_step_enforcement.py::test_blocked_tools_overrides_infra_exempt_for_helper`.

### 2.3 Add `get_cancelled_session_ids` to `_AgentRunQueryMixin` [category: code]

`kind: deliverable`

Target: `src/gobby/storage/agents/_queries.py` — `_AgentRunQueryMixin` (lines 34–224 on HEAD), which is composed into `LocalAgentRunManager` (`src/gobby/storage/agents/_manager.py:14-27`). Tests live alongside the manager in `tests/storage/test_storage_agents.py` under the `TestLocalAgentRunManager` class.

Read-only helper used by 2.4's delivery formatter to identify queued P2P messages whose `from_session` belongs to an agent run that has been cancelled (e.g., by 2.5's `cancel_stale_helpers` tool). Recency-bounded so the query stays fast even with many historical cancellations.

Use the codebase's time-window helper `newer_than_now_expr(db, column, placeholder, unit)` (sibling of `older_than_now_expr` already used by `_AgentRunCleanupMixin.cleanup_stale_pending_runs` at `_cleanup.py:112-153`). The hub storage layer is Postgres-only (`StorageDialect = Literal["postgres"]` at `src/gobby/storage/sql_dialect.py:10`); `newer_than_now_expr` generates a Postgres `INTERVAL`-based expression. Application-level placeholders stay as `?` — the hub DSN layer rewrites them to `$N` for Postgres.

```python
# In src/gobby/storage/agents/_queries.py, inside _AgentRunQueryMixin:

def get_cancelled_session_ids(
    self: _AgentRunQueryHost,
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
    from gobby.storage.sql_dialect import newer_than_now_expr

    # `newer_than_now_expr(db, column, placeholder, unit)` is the
    # Postgres "column is within the last N <unit>s" primitive exported
    # from `gobby.storage.sql_dialect` (sibling of `older_than_now_expr`
    # which `_AgentRunCleanupMixin.cleanup_stale_pending_runs` uses).
    # The hub layer is Postgres-only; the helper generates a Postgres
    # INTERVAL expression directly.
    #
    # CRITICAL: filter on `completed_at`, NOT `created_at`. Cancellation
    # writes the terminal timestamp to `completed_at` (and `updated_at`).
    # A helper that was created hours ago but cancelled THIS turn must
    # still appear in the cancelled set — filtering on `created_at` would
    # miss it because the row was created outside the recency window even
    # though the cancellation just happened. `completed_at` is populated
    # by `terminalize_cancelled_agent_run` (the same path `stop_agent_run`
    # in §2.5 delegates to), so every cancelled row has a non-NULL
    # `completed_at` by the time this query reads it.
    recency_sql = newer_than_now_expr(self.db, "completed_at", "?", "hour")
    sql = (
        "SELECT child_session_id FROM agent_runs "
        f"WHERE status = 'cancelled' AND child_session_id IS NOT NULL AND {recency_sql}"
    )  # nosec B608 - recency_sql is selected by storage dialect.
    params: list[int | str] = [since_hours]
    if agent_name is not None:
        sql += " AND agent_name = ?"
        params.append(agent_name)
    rows = self.db.fetchall(sql, params)
    return {row["child_session_id"] for row in rows}
```

This is purely additive on `agent_runs` — no existing call sites filter by cancelled status with this signature, so there's no risk of regression. The host class is `LocalAgentRunManager`, but the method is defined on `_AgentRunQueryMixin` (per the mixin composition pattern HEAD uses for the storage package).

Validation criteria: unit test in `tests/storage/test_storage_agents.py::TestLocalAgentRunManager` creates rows with mixed statuses (`success`, `running`, `cancelled` recent, `cancelled` old) and asserts `get_cancelled_session_ids(since_hours=24)` returns exactly the recent-cancelled set. Test with `since_hours=1` and a row cancelled 2h ago confirms recency window is honored. Test with no rows returns empty set without error. **Agent-name scoping test (required, not optional)**: with three cancelled-recent rows (`agent_name='memory-recall-helper'`, `agent_name='other-agent'`, `agent_name=NULL`), assert `get_cancelled_session_ids(agent_name='memory-recall-helper')` returns only the helper row's child_session_id; assert `get_cancelled_session_ids()` (no `agent_name`) returns all three. Without the scoping, the delivery formatter would silently discard cancelled non-helper children's plain P2P messages. **Recency-by-completed_at test (required, not optional — round-7 F3 guard)**: insert a cancelled row with OLD `created_at` (e.g., 48 hours ago) but RECENT `completed_at` (e.g., 5 minutes ago) — simulating a long-running helper that was just cancelled on the current turn. Assert `get_cancelled_session_ids(since_hours=1)` DOES include this row. Also insert a cancelled row with RECENT `created_at` (e.g., 30 minutes ago) but OLD `completed_at` (e.g., 25 hours ago) — simulating a row from an older completed cancellation that is outside the window. Assert `get_cancelled_session_ids(since_hours=24)` does NOT include this second row. This proves the recency window is keyed off `completed_at` (when the cancellation happened) and not `created_at` (when the run was originally started). Without this test, a regression to `created_at` filtering would silently let a just-cancelled long-running helper's queued `memory_recall` payload through guard A. **Timezone-aware Postgres recency test (required, not optional)**: the hub storage layer is Postgres-only (`StorageDialect = Literal["postgres"]` at `src/gobby/storage/sql_dialect.py:10`; `HubDatabase.dialect: Literal["postgres"]` at `src/gobby/storage/hub/protocol.py:206`; the `temp_db` fixture delegates to `postgres_db`). There is no SQLite hub backend to parameterize against. This test instead validates that the Postgres `INTERVAL`-based recency expression in `newer_than_now_expr` correctly handles timezone-aware `completed_at` values. Insert two cancelled-recent rows with `completed_at` set to timezone-aware timestamps (matching what `utc_now_iso()` produces — ISO-8601 with `T` separator and `+00:00` offset), one completed 30 minutes ago and one completed 90 minutes ago. Assert `get_cancelled_session_ids(since_hours=1)` returns only the 30-minutes-ago row. This guards against naive timestamp comparison regressions and confirms the `INTERVAL` arithmetic resolves correctly against the Postgres `NOW()` function for timezone-aware values stored in the `completed_at` column.

**Acceptance:**

- 2.3.1 — `_AgentRunQueryMixin.get_cancelled_session_ids(since_hours, agent_name)` returns recent-cancelled child_session_ids and composes into `LocalAgentRunManager`. symbol: `gobby.storage.agents._queries._AgentRunQueryMixin.get_cancelled_session_ids`.
- 2.3.2 — Recency-window unit test confirms only rows within the window are returned. test: `tests/storage/test_storage_agents.py::TestLocalAgentRunManager::test_get_cancelled_session_ids_honors_recency_window`.
- 2.3.3 — Agent-name scoping test confirms `agent_name="memory-recall-helper"` returns only helper rows; absent-filter form returns all. test: `tests/storage/test_storage_agents.py::TestLocalAgentRunManager::test_get_cancelled_session_ids_agent_name_scoping`.
- 2.3.4 — Recency-by-completed_at test confirms the query includes rows with old `created_at` but recent `completed_at` (just-cancelled long-running helpers), and excludes rows with recent `created_at` but old `completed_at` (outside the recency window). test: `tests/storage/test_storage_agents.py::TestLocalAgentRunManager::test_get_cancelled_session_ids_recency_uses_completed_at`.
- 2.3.5 — Timezone-aware Postgres recency test confirms the query correctly handles `completed_at` values with explicit UTC offset under the production Postgres backend. test: `tests/storage/test_storage_agents.py::TestLocalAgentRunManager::test_get_cancelled_session_ids_postgres_timezone_handling`.

### 2.4 Helper-aware delivery + same-turn dedup on the inline `inject_result` path [category: code] (depends: 2.1, 2.3)

`kind: deliverable`

Targets:
- `src/gobby/workflows/engine/effects.py` (inside `EffectsMixin._apply_effect` at lines 57–255 on HEAD; the `effect.type == "mcp_call"` branch where `effect.inject_result and not effect.background and self._mcp_dispatcher` is true sits around line 109 — primary edit).
- `src/gobby/hooks/dispatchers/mcp.py` (`format_discovery_result` is imported and reused; add a `search_memories` formatter case here if one does not already exist on HEAD, so the helper-surfaced memories render identically to fast-recall memories).
- `tests/workflows/test_delivery_pipeline.py` (new test file for the formatters).

This is the path that BOTH the existing `memory-recall-on-prompt` rule (priority 10, calling `gobby-memory.search_memories`) AND 3.1's modified `deliver-pending-messages` rule (calling `gobby-agents.deliver_pending_messages`) invoke. Today fast recall renders raw `search_memories` results without consulting `injected_memory_ids`, so on the same `turn_start` where the helper-delivery path also tries to surface a memory id the fast path already rendered, the parent sees the same memory twice. 2.4 places the dedup-against-and-append-to `injected_memory_ids` filter inside this inline path so both writers share one source of truth.

**Critical**: both formatters MUST resolve the session id for `SessionVariableManager` calls via `event.metadata.get('_platform_session_id')`, NOT `event.session_id`. The latter is the CLI external id (Claude `external_id`, Codex `thread_id`); the former is the canonical Gobby session row id under which `injected_memory_ids` is stored. The existing `_dedup_memory_results` path (`src/gobby/hooks/hook_manager.py:741`) reads `_platform_session_id` for exactly this reason — verifiable at `hook_manager.py:478, 570, 632`. Mis-keying would silently break dedup across fast recall and helper delivery without any visible error. Both formatters in 2.4 use the same resolution helper.

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
      2b. Check whether any `memory-recall-helper` run for the parent
          remains `pending`/`running` (via list_by_parent). If so, set
          `cancel_incomplete` and drop all memory_recall payloads
          (guard C — catches the immediate-next-turn failed-cancel case
          that guard B cannot).
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
      6b. **Hard cap (trust-boundary guard)**: truncate the surviving
          list to at most `MAX_HELPER_MEMORIES = 3` entries. Applied
          AFTER freshness filtering and dedup (steps 2–6), BEFORE
          formatting and `injected_memory_ids` append (steps 7–8), so
          only memories that will actually render count against the cap.
          This is the runtime enforcement of the 0–3 contract from §1.4;
          a buggy or over-eager helper cannot flood the parent's context
          beyond 3 memories per delivery regardless of how many it sent.
          The cap applies across ALL helper `memory_recall` messages in
          one delivery (not per-message) because step 5 already merges
          memories from multiple payloads by id.
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
    cancelled_lookup_failed: bool = False
    cancel_incomplete: bool = False
    if messages:
        run_storage = LocalAgentRunManager(self.db)
        try:
            # Scope to memory-recall-helper runs only. The helper accepts
            # an agent_name filter (added in 2.3) so we don't sweep up
            # cancelled non-helper children — see 2.3 for the signature.
            helper_cancelled_sessions = run_storage.get_cancelled_session_ids(
                agent_name="memory-recall-helper",
            )
        except Exception as e:  # noqa: BLE001 — fail CLOSED for memory_recall
            logger.warning(
                f"Failed to load cancelled helper session ids: {e}; "
                "dropping all memory_recall payloads (fail-closed)"
            )
            cancelled_lookup_failed = True

        # Guard C: cancel-incomplete fail-closed. If any
        # memory-recall-helper run remains pending/running for this parent
        # after the priority-5 cancel attempt, the cancel MCP tool failed
        # or only partially succeeded. Guard B alone does NOT catch the
        # immediate-next-turn case: a helper spawned at turn N has
        # origin_turn_seq=N, and after the priority-1 increment
        # current_parent_turn_seq - 1 = N, so guard B accepts it. Drop all
        # memory_recall payloads when a stale helper is still running.
        if not cancelled_lookup_failed and platform_session_id:
            try:
                still_running = [
                    r for r in run_storage.list_by_parent(platform_session_id)
                    if r.agent_name == "memory-recall-helper"
                    and r.status in ("pending", "running")
                ]
                cancel_incomplete = len(still_running) > 0
            except Exception as e:  # noqa: BLE001 — fail CLOSED
                logger.warning(
                    f"Failed to check for still-running helpers: {e}; "
                    "dropping all memory_recall payloads (fail-closed)"
                )
                cancel_incomplete = True

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
            # Fail-closed on cancelled-session lookup failure: if we
            # cannot determine whether the sending helper was cancelled,
            # we cannot prove the payload is from a still-valid run.
            # Drop all memory_recall payloads (NOT plain P2P messages)
            # to preserve the freshness contract's invariant that no
            # unverifiable helper output ever injects. Guard B alone is
            # insufficient because a legitimately-fresh origin_turn_seq
            # from a just-cancelled helper would pass it.
            if cancelled_lookup_failed:
                logger.warning(
                    "Dropping memory_recall: cancelled-session lookup failed "
                    f"(from_session={msg.get('from_session')!r})"
                )
                continue
            # Guard C: cancel-incomplete fail-closed. A stale helper
            # that survived the priority-5 cancel attempt may queue a
            # memory_recall whose origin_turn_seq matches current-1.
            if cancel_incomplete:
                logger.warning(
                    "Dropping memory_recall: stale helper still running "
                    f"after cancel attempt (from_session={msg.get('from_session')!r})"
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
        # Hard cap: inject at most MAX_HELPER_MEMORIES per delivery,
        # regardless of how many the helper sent. The cap is a trust-boundary
        # guard — even a buggy or over-eager helper cannot flood the parent's
        # context beyond this limit. The helper's own instructions say 0–3
        # (§1.4), but instructions are soft; this is the runtime enforcement.
        # Applied AFTER dedup and freshness filtering, BEFORE formatting and
        # injected_memory_ids append, so only the memories that will actually
        # render count against the cap. Excess memories are silently dropped
        # (no error, no injection, no id tracking — they never happened).
        MAX_HELPER_MEMORIES = 3
        if len(new_memories) > MAX_HELPER_MEMORIES:
            logger.debug(
                f"Capping helper memories from {len(new_memories)} to "
                f"{MAX_HELPER_MEMORIES}"
            )
            new_memories = new_memories[:MAX_HELPER_MEMORIES]
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
- `SessionVariableManager.append_to_set_variable` is the same atomic primitive `_dedup_memory_results` (`src/gobby/hooks/hook_manager.py:741`) uses for the same variable. Race-free across concurrent writers because it goes through the same DB transaction path.
- Empty short-circuit comes first so the empty-queue noise case ALSO covers any future tool with `inject_result: true`. Helper-aware and search_memories-aware paths fire only on their specific `(server, tool)` match.
- Errors in dedup-state read or write are fail-open (log debug, proceed) — same posture as `_dedup_memory_results`. **However**, errors in the cancelled-session lookup (`get_cancelled_session_ids`) are fail-CLOSED for `memory_recall` payloads: the `cancelled_lookup_failed` flag causes all `memory_recall` payloads to be dropped with a warning log, while non-memory_recall messages still flow through to `other_messages`. The rationale: dedup-state failure at worst causes a duplicate injection (annoying, not dangerous), but cancelled-session lookup failure means guard A cannot verify whether a payload is from a cancelled helper — and guard B alone is insufficient because a just-cancelled helper whose `origin_turn_seq` matches `parent_turn_seq - 1` would pass it (the payload was legitimately fresh at the moment of send_message, but the run was cancelled before delivery). Fail-closed preserves the freshness contract invariant: no unverifiable helper output ever injects.
- **Guard C (cancel-incomplete) is also fail-CLOSED.** After the cancelled-session lookup succeeds, the formatter checks whether any `memory-recall-helper` run for the parent remains `pending`/`running` via `run_storage.list_by_parent(platform_session_id)`. If any do, a stale helper that the priority-5 cancel failed to stop may queue a `memory_recall` whose `origin_turn_seq` matches `current - 1` (guard B would accept it) and whose `from_session` is not in the cancelled set (guard A would also accept it). Guard C catches this case by detecting the still-running state and dropping all `memory_recall` payloads. If the `list_by_parent` call itself raises, the `cancel_incomplete` flag is also set — same fail-closed posture as `cancelled_lookup_failed`. The check is skipped when `cancelled_lookup_failed` is already True (all payloads are already going to be dropped).
- `HookManager._evaluate_workflow_rules`'s dedup loop is left untouched. We do NOT need it to fire on `deliver_pending_messages` results; 2.4 handles the full pipeline inline. The existing `_dedup_memory_results` continues to fire for `dispatch_result` items from non-`inject_result` paths (no functional change).

**Test fixture convention (load-bearing — round 6 F2 guard).** Every memory record used in the §2.4 and §3.5 test fixtures MUST carry both a stable `id` AND a unique `content` sentinel string (e.g., `{"id": "m1", "content": "content-sentinel-m1"}`). The existing `search_memories` formatter at `src/gobby/hooks/dispatchers/mcp.py:148-167` renders ONLY `m.get("content")` plus the optional score/via metadata — it does NOT render the memory `id`. So tests that assert the injected context "contains `m1`" would either silently pass (when content happens to embed the id) or silently fail (when content does not). Per round 6 F2, all rendered-context assertions in this plan use the unique content sentinel, and all state assertions on `injected_memory_ids` use the id. Concretely: "context_parts contains the rendered memory" → assert the content sentinel string is in the formatted output; "`injected_memory_ids` after the call contains `["m1"]`" → assert the id is in the set variable. The two assertions cover different layers (rendered UX vs internal state) and must not be conflated. If a future test really needs to assert ID presence in rendered output, it must first add a formatter change that explicitly renders IDs, with its own UX acceptance item.

Validation criteria: unit tests in a new `tests/workflows/test_delivery_pipeline.py` cover both formatters. For `_format_delivery_result`: (1) empty result `{"messages": [], "count": 0}` → returns `None`, no `injected_memory_ids` mutation; (2) result with one `memory_recall` message containing memory `{"id": "m1", "content": "content-sentinel-m1"}` and `injected_memory_ids` initially empty → returns formatted string containing the content sentinel `"content-sentinel-m1"` rendered through the search_memories formatter (the formatter at `src/gobby/hooks/dispatchers/mcp.py:148-167` emits `m.get("content")` not `m.get("id")`), and `injected_memory_ids` after the call contains `["m1"]` (the state variable stores ids, so the state assertion uses the id); (3) result with `memory_recall` containing the same `m1` record when `injected_memory_ids` already contains `m1` → returns `None` (or empty) and `injected_memory_ids` unchanged; (4) result with `memory_recall` (`{"id": "m1", "content": "content-sentinel-m1"}`) AND a non-memory_recall plain text message containing a distinct sentinel like `"plain-msg-sentinel"` → returned formatted string contains the content sentinel `"content-sentinel-m1"` once AND the plain message's `"plain-msg-sentinel"` substring; (5) result with malformed message content (not JSON) → message falls through to "other_messages" and renders via generic formatter; (6) two concurrent calls to `append_to_set_variable` from different rule evaluations do not lose either's IDs (race test); (7) **freshness guard A scoped to helper memory_recall payloads:** result with one `memory_recall` message whose `from_session` belongs to a `cancelled` `memory-recall-helper` run, carrying a memory `{"id": "m-cancelled", "content": "content-sentinel-cancelled"}` → the message is dropped (formatter return value does NOT contain `"content-sentinel-cancelled"`), `injected_memory_ids` is NOT mutated to include `"m-cancelled"`. (7-aux) **scope test:** result with one `plain` (non-memory_recall) text message whose `from_session` belongs to a `cancelled` non-helper child run and whose body contains sentinel `"plain-cancelled-sentinel"` → the message is NOT dropped; it falls through to `other_messages` and the generic formatter output contains the sentinel. This is the F1 round-5 guard — without it, dropping the `is_spawned_agent` gate on `deliver-pending-messages` would cause data loss for cancelled non-helper children. (7b) **session-key correctness:** with a `HookEvent` whose `event.session_id` (external id) is `"ext-X"` and `event.metadata['_platform_session_id']` is `"plat-Y"`, both formatters MUST read/write `injected_memory_ids` under session id `"plat-Y"` and NEVER under `"ext-X"`. Concrete assertion: after the formatter runs, `SessionVariableManager(...).get_variables("plat-Y")['injected_memory_ids']` includes the new ids and `SessionVariableManager(...).get_variables("ext-X")['injected_memory_ids']` is unchanged (or absent). (7c) **freshness guard B:** with `variables['parent_turn_seq'] == 5`, a `memory_recall` payload with `origin_turn_seq=4` and content sentinel `"content-sentinel-fresh"` is accepted (matches current-1, formatter output contains the sentinel; the test also asserts `injected_memory_ids` mutates to include the memory's id), `origin_turn_seq=3` (sentinel `"content-sentinel-too-old"`) is dropped (sentinel not in output, no id mutation), `origin_turn_seq=5` is dropped (impossible — a helper from this very turn cannot have replied yet), `origin_turn_seq=6` is dropped (impossible / future), and a payload missing `origin_turn_seq` entirely is dropped. (7d) **fail-CLOSED behavior:** with `variables['parent_turn_seq']` missing or non-int, ALL `memory_recall` payloads are dropped — the formatter's rendered output does NOT contain ANY of the payload's content sentinels, no `injected_memory_ids` mutation, and a warning is logged. Non-memory_recall messages from the same delivery are unaffected (they fall through to `other_messages` and the rendered output contains their plain-message sentinel). This is the F2 round-5 guard — without it, a misconfiguration could let stale helper memory inject indefinitely. (7e) **kill-switch catch-all (round 9 guard):** with `variables['memory_recall_helper_enabled'] == False`, ALL `memory_recall` payloads are dropped regardless of `origin_turn_seq` match (rendered output does NOT contain the payload's content sentinel, `injected_memory_ids` unchanged). Non-memory_recall messages still flow through (their plain-message sentinel renders). Concrete test: prior turn enabled → helper spawned → user sets `memory_recall_helper_enabled=False` → helper completes and queues a `memory_recall` payload carrying sentinel `"content-sentinel-disabled"` → next parent turn_start fires deliver → rendered output does NOT contain `"content-sentinel-disabled"`, `injected_memory_ids` unchanged. This catches the across-disable/re-enable race the round-9 adversary identified: even if 3.4 still advances `parent_turn_seq` while the toggle is off (which it does — see 3.4's "Intentionally NOT gated" rationale), a queued payload's `origin_turn_seq` could happen to match the new `current - 1` value if disable-then-re-enable timing aligns; the catch-all drops it unambiguously. (11) **hard-cap trust-boundary guard (round-7 F1 guard):** result with a single `memory_recall` message containing 5 memories `[{"id":"m1","content":"content-sentinel-m1"}, {"id":"m2","content":"content-sentinel-m2"}, {"id":"m3","content":"content-sentinel-m3"}, {"id":"m4","content":"content-sentinel-m4"}, {"id":"m5","content":"content-sentinel-m5"}]` with `injected_memory_ids` initially empty → returns formatted string containing ONLY the first 3 content sentinels (`"content-sentinel-m1"`, `"content-sentinel-m2"`, `"content-sentinel-m3"`) and NOT the last 2 (`"content-sentinel-m4"`, `"content-sentinel-m5"`). `injected_memory_ids` after the call contains exactly `["m1","m2","m3"]` — the capped ids are appended, the excess ids are NOT tracked. A debug log entry mentions "Capping helper memories from 5 to 3". This proves the runtime enforces the 0–3 contract at the trust boundary regardless of what the helper sends. The cap is applied PER DELIVERY (across all memory_recall messages in one `deliver_pending_messages` result) after freshness and dedup filtering, not per-message. (11b) **cap after dedup interaction:** result with two `memory_recall` messages containing 3 fresh+new memories each (6 total, no id overlap, all pass freshness/dedup) → only the first 3 (by insertion order into `helper_memories`) render and are id-tracked; the other 3 are silently dropped. (12) **fail-closed on cancelled-session lookup failure (round-8 F2 guard):** force `LocalAgentRunManager.get_cancelled_session_ids` to raise (e.g., monkey-patch to raise `RuntimeError("db unavailable")`). Deliver a result with one `memory_recall` payload carrying memory `{"id": "m-lookup-fail", "content": "content-sentinel-lookup-fail"}` whose `origin_turn_seq` matches `parent_turn_seq - 1` (i.e., would pass guard B if guard A were not considered) AND a non-memory_recall plain text message carrying sentinel `"plain-msg-lookup-fail-sentinel"`. Assert: (a) rendered output does NOT contain `"content-sentinel-lookup-fail"` (memory_recall payload dropped), (b) `injected_memory_ids` is NOT mutated to include `"m-lookup-fail"`, (c) rendered output DOES contain `"plain-msg-lookup-fail-sentinel"` (plain P2P messages are unaffected — fail-closed applies only to memory_recall payloads, not to the entire delivery), (d) a warning log mentions "cancelled-session lookup failed" and "dropping all memory_recall payloads". This proves the formatter fails closed on the guard-A lookup and does not silently degrade to guard-B-only for memory_recall when the DB is unreachable, while preserving non-helper P2P delivery. (13) **cancel-incomplete guard C (round-10 F1 guard):** pre-insert into `agent_runs` a `running` `memory-recall-helper` row with `parent_session_id='platform-Y'` and `child_session_id='child-still-running'`. Do NOT cancel it (simulates a failed cancel_stale_helpers call). Deliver a result with one `memory_recall` payload from `from_session='child-still-running'` carrying memory `{"id": "m-cancel-fail", "content": "content-sentinel-cancel-fail"}` whose `origin_turn_seq` matches `parent_turn_seq - 1` (i.e., would pass both guard A and guard B individually) AND a non-memory_recall plain text message carrying sentinel `"plain-msg-cancel-fail-sentinel"`. Assert: (a) rendered output does NOT contain `"content-sentinel-cancel-fail"` (memory_recall payload dropped by guard C), (b) `injected_memory_ids` is NOT mutated to include `"m-cancel-fail"`, (c) rendered output DOES contain `"plain-msg-cancel-fail-sentinel"` (plain P2P messages are unaffected — guard C applies only to memory_recall payloads), (d) a warning log mentions "stale helper still running after cancel attempt". This proves the formatter checks for still-running helpers at delivery time and fails closed when any remain, preventing injection of a payload that would pass guards A and B on their own. (13b) **cancel-incomplete check failure fail-closed:** monkey-patch `LocalAgentRunManager.list_by_parent` to raise `RuntimeError("db unavailable")` (the cancelled-session lookup succeeds but the still-running check fails). Deliver a `memory_recall` payload whose `origin_turn_seq` matches `parent_turn_seq - 1` with content sentinel `"content-sentinel-list-fail"`. Assert: (a) rendered output does NOT contain `"content-sentinel-list-fail"`, (b) warning log mentions "Failed to check for still-running helpers", (c) non-memory_recall messages still flow through. For `_format_search_memories_result`: (8) empty `{"memories": []}` → `None`; (9) `{"memories": [{"id":"m1", "content":"content-sentinel-m1"}]}` with `injected_memory_ids` empty → returns formatted string containing the content sentinel `"content-sentinel-m1"`, `injected_memory_ids` becomes `["m1"]`; (10) `{"memories": [{"id":"m1","content":"content-sentinel-m1"},{"id":"m2","content":"content-sentinel-m2"}]}` with `injected_memory_ids = ["m1"]` → returns formatted string containing `"content-sentinel-m2"` and NOT containing `"content-sentinel-m1"`, `injected_memory_ids` becomes `["m1","m2"]`. End-to-end (manual): submit a real prompt, observe both fast-recall (priority 10) and helper-delivery (priority 10, also turn_start) on the same turn — verify a memory selected by both surfaces (i.e., whose content sentinel both rules would render) appears only ONCE in injected context (the unique content string occurs exactly once in the merged output) and `injected_memory_ids` accumulates both writers' picks (the set contains both ids). On a subsequent turn where the helper or fast recall selects the same id, the content sentinel does NOT re-appear in the rendered output and the id is still present in `injected_memory_ids`. `tests/e2e/test_inter_agent_messages.py` continues to pass (parent ↔ child messaging via `send_message` + `deliver_pending_messages` is unaffected because non-memory_recall messages still flow through `other_messages`).

**Acceptance:**

- 2.4.1 — `EffectsMixin._format_delivery_result` implements the helper-aware delivery pipeline (empty short-circuit, fail-closed on cancelled-session lookup failure, cancel-incomplete guard C, cancelled-session drop via guard A, freshness guard B, kill-switch catch-all, dedup, hard cap of 3, atomic append, format). symbol: `gobby.workflows.engine.effects.EffectsMixin._format_delivery_result`.
- 2.4.2 — `EffectsMixin._format_search_memories_result` dedupes fast-recall results against `injected_memory_ids` keyed by `_platform_session_id`. symbol: `gobby.workflows.engine.effects.EffectsMixin._format_search_memories_result`.
- 2.4.3 — `_apply_effect` switches on `(effect.server, effect.tool)` to dispatch the appropriate formatter, including the `cancel_stale_helpers` no-op-formatter case (returns None so the cancel rule's `inject_result: true` sync marker injects no context). test: `tests/workflows/test_delivery_pipeline.py::test_apply_effect_dispatch_switch_cancel_stale_helpers_no_op`.
- 2.4.4 — `_is_empty_inject_payload` short-circuit helper covers `count=0`, empty-`messages`, and empty-`memories` shapes. symbol: `gobby.workflows.engine.effects._is_empty_inject_payload`.
- 2.4.5 — Empty-result delivery returns None without mutating `injected_memory_ids`. test: `tests/workflows/test_delivery_pipeline.py::test_empty_delivery_no_mutation`.
- 2.4.6 — Single-helper-memory delivery formats and atomic-appends the id. test: `tests/workflows/test_delivery_pipeline.py::test_single_memory_recall_inject`.
- 2.4.7 — Already-injected ids are filtered out and not re-rendered. test: `tests/workflows/test_delivery_pipeline.py::test_dedup_against_injected_ids`.
- 2.4.8 — Mixed memory_recall + plain text messages render once each via the right formatters. test: `tests/workflows/test_delivery_pipeline.py::test_mixed_memory_recall_and_plain_messages`.
- 2.4.9 — Concurrent `append_to_set_variable` calls do not lose ids. test: `tests/workflows/test_delivery_pipeline.py::test_concurrent_append_race_safe`.
- 2.4.10 — Freshness guard A: cancelled helper memory_recall is dropped; cancelled non-helper plain P2P message is preserved. test: `tests/workflows/test_delivery_pipeline.py::test_freshness_guard_a_helper_scoped`.
- 2.4.11 — Session-key correctness: formatters read/write `injected_memory_ids` under `_platform_session_id`, never under external `session_id`. test: `tests/workflows/test_delivery_pipeline.py::test_session_key_uses_platform_session_id`.
- 2.4.12 — Freshness guard B: payload `origin_turn_seq` must equal `parent_turn_seq - 1` to be accepted; mismatches and missing values are dropped. test: `tests/workflows/test_delivery_pipeline.py::test_freshness_guard_b_origin_turn_seq`.
- 2.4.13 — Fail-CLOSED behavior with missing/non-int `parent_turn_seq` drops all memory_recall payloads with a warning. test: `tests/workflows/test_delivery_pipeline.py::test_fail_closed_when_parent_turn_seq_missing`.
- 2.4.14 — Kill-switch catch-all drops memory_recall payloads when `memory_recall_helper_enabled` is False. test: `tests/workflows/test_delivery_pipeline.py::test_kill_switch_drops_memory_recall_payloads`.
- 2.4.15 — Fast-recall formatter dedupes and atomic-appends ids consistently with the delivery formatter. test: `tests/workflows/test_delivery_pipeline.py::test_search_memories_formatter_dedup`.
- 2.4.16 — Hard-cap trust-boundary guard: a delivery carrying more than 3 surviving memories (after freshness/dedup) is truncated to 3; only those 3 render and are id-tracked; excess memories are silently dropped with a debug log. test: `tests/workflows/test_delivery_pipeline.py::test_hard_cap_truncates_excess_helper_memories`.
- 2.4.17 — Fail-closed on cancelled-session lookup failure: when `get_cancelled_session_ids` raises, all `memory_recall` payloads are dropped (even those whose `origin_turn_seq` would pass guard B) while non-memory_recall messages from the same delivery still flow through. test: `tests/workflows/test_delivery_pipeline.py::test_fail_closed_on_cancelled_lookup_failure`.
- 2.4.18 — Cancel-incomplete guard C: when any `memory-recall-helper` run for the parent remains `pending`/`running` after the priority-5 cancel attempt, all `memory_recall` payloads are dropped (even those whose `origin_turn_seq` matches `parent_turn_seq - 1` and whose `from_session` is not in the cancelled set) while non-memory_recall messages still flow through. test: `tests/workflows/test_delivery_pipeline.py::test_cancel_incomplete_drops_memory_recall_payloads`.

### 2.5 Add `cancel_stale_helpers` MCP tool sharing `stop_agent`'s lifecycle path [category: code] (depends: 2.3)

`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/agent_cancellation.py` (existing module at 131 lines on HEAD — primary edit: add new public `stop_agent_run(...)` function that contains the shared cancellation body, keeping its 200-line-ish post-edit footprint well below the 1,000-line monolith limit and avoiding pushing `agents.py` over the limit).
- `src/gobby/mcp_proxy/tools/agents.py` (the `create_agents_registry` factory at lines 215–984 on HEAD that owns `stop_agent` at 393–453, `kill_agent` at 502–672, and `end_agent_run` at 462–492 — secondary edit: shrink `stop_agent` to a one-line delegate to `stop_agent_run(...)`, and add the new `cancel_stale_helpers` tool registration that also delegates to `stop_agent_run(...)`).
- `tests/mcp_proxy/tools/test_agents.py` (existing `TestStopAgent` coverage must continue to pass after the extract).
- `tests/mcp_proxy/tools/test_agent_cancellation.py` (existing coverage of the underlying terminalize helper; extend with direct unit tests for the new `stop_agent_run` function).
- `tests/mcp_proxy/tools/test_cancel_stale_helpers.py` (new file — cancel-tool-specific cases).

Both `stop_agent` and the new `cancel_stale_helpers` will share an extracted public helper in `agent_cancellation.py` so the same lifecycle/process-kill/terminal-cleanup path runs for every cancellation.

**Why extract to `agent_cancellation.py` rather than keep the helper inline in `agents.py`?** `src/gobby/mcp_proxy/tools/agents.py` is 956 lines on HEAD. Inlining `_stop_run` (~65 lines) plus the new `cancel_stale_helpers` registration (~60 lines) net would push the file over the repo-enforced 1,000-line non-test Python source-file monolith limit (Gobby's guiding principle #2 — never create or leave monoliths over 1,000 lines for non-test Python, TypeScript, or CSS source files). `src/gobby/mcp_proxy/tools/agent_cancellation.py` already holds the shared cancellation primitives (`terminalize_cancelled_agent_run`, `terminalize_killed_agent_run`, `recover_cancelled_agent_task_claim`) — adding `stop_agent_run` alongside them keeps the cancellation logic co-located, shrinks `agents.py` by ~55 lines net (delete the existing `stop_agent` body, add only the one-line delegate plus the small `cancel_stale_helpers` body), and brings `agent_cancellation.py` from 131 lines to ~200 lines (still well under the limit). No new module is required; the existing one is the natural home.

Why not put cancellation into `spawn_agent`'s factory? `create_spawn_agent_registry` does NOT receive the lifecycle monitor, hook cleanup, or terminal-cleanup dependencies that `stop_agent` requires. Falling back to `runner.cancel_run(run_id)` alone would mark the DB row cancelled but leave the helper's subprocess alive and able to keep issuing MCP calls — defeating the freshness contract. Putting cancellation alongside `stop_agent` (which already has all the right deps wired) is the simpler, correct shape.

Step 1 — add a new public function `stop_agent_run(...)` to `src/gobby/mcp_proxy/tools/agent_cancellation.py` that takes the cancellation dependencies as explicit keyword arguments (so it does NOT need to capture them via closure — the function is a module-level free function, and both `stop_agent` and `cancel_stale_helpers` pass their registry closure's captured deps as kwargs). The body is the verbatim contents of the current `async def stop_agent(run_id)` at `src/gobby/mcp_proxy/tools/agents.py:393-453` (verified line range against HEAD), lifted into the module-level helper:

```python
# In src/gobby/mcp_proxy/tools/agent_cancellation.py, ADDED below the existing
# terminalize_killed_agent_run definition (after line 131 on HEAD).

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def stop_agent_run(
    *,
    run_id: str,
    runner: Any,
    agent_run_manager: Any,
    db: Any,
    lifecycle_monitor: Any | None,
    completion_registry: Any | None,
    task_manager: Any | None,
    session_manager: Any | None,
    hook_manager_resolver: Any | None,
    kill_agent_process: Any,        # injected `gobby.agents.kill.kill_agent`
    cleanup_terminal_artifacts: Any,  # injected `_cleanup_terminal_artifacts` from agents.py
) -> dict[str, Any]:
    """Shared cancellation: stop a single agent run end-to-end.

    Performs the same work the existing `stop_agent` MCP tool does, in
    the same order, with the same error semantics. Both `stop_agent` and
    `cancel_stale_helpers` delegate here so process-kill, lifecycle
    teardown, completion notification, and terminal cleanup are
    guaranteed to happen for every cancellation.

    `kill_agent_process` and `cleanup_terminal_artifacts` are injected
    rather than imported here to avoid an import cycle: `agents.py`
    imports from `agent_cancellation.py` already (for the existing
    terminalize helpers), and `_cleanup_terminal_artifacts` lives in
    `agents.py`. The registry closure passes them as kwargs.
    """
    # Step 1: Look up the run; bail if missing or not pending/running.
    run = runner.get_run(run_id)
    if not run:
        return {"success": False, "error": f"Agent run {run_id} not found"}
    if run.status not in ("pending", "running"):
        return {"success": False, "error": f"Cannot stop agent in status: {run.status}"}

    # Step 2: Kill the underlying subprocess + close its terminal.
    kill_db = db or agent_run_manager.db
    result = await kill_agent_process(
        run, kill_db, signal_name="TERM", close_terminal=True,
    )
    if not result.get("success") and result.get("error") != "No target PID found":
        return result  # Real kill failure — abort early.

    # Step 3: Transition the DB row to cancelled via the canonical
    # terminalize helper (it owns the lifecycle_monitor vs fallback
    # split and also recovers the task claim on the fallback path).
    transitioned = await terminalize_cancelled_agent_run(
        runner=runner,
        run_id=run_id,
        terminal_reason="user_cancelled",
        lifecycle_monitor=lifecycle_monitor,
        completion_registry=completion_registry,
        task_manager=task_manager,
        message=f"Agent {run_id} cancelled",
    )

    if not transitioned:
        current = runner.get_run(run_id)
        logger.debug(
            "stop_agent_run no-op for run %s; current status=%s",
            run_id, current.status if current else "missing",
        )

    # Step 4: Tear down terminal artifacts (firing synthetic stop hook
    # internally if applicable). MUST run regardless of whether the DB
    # transition succeeded — terminal/tmux state still needs cleanup.
    await cleanup_terminal_artifacts(
        run_id=run.id,
        db=kill_db,
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
- `kill_agent_process(run, kill_db, signal_name="TERM", close_terminal=True)` — process kill + terminal close.
- `terminalize_cancelled_agent_run(...)` — single entry point that internally owns the `lifecycle_monitor.terminalize_cancelled_run` vs `runner.cancel_run` + `completion_registry.notify` + `task_manager` claim-recovery split. Do NOT re-implement that split here; the helper already has its own test surface at `tests/mcp_proxy/tools/test_agent_cancellation.py`.
- `cleanup_terminal_artifacts(run_id=..., db=kill_db, tmux_session_name=..., agent_session_id=..., session_manager=..., hook_manager_resolver=..., result=result)` — fires the synthetic stop hook (`_fire_synthetic_stop`) internally per its body; this is how the SessionStop hook chain stays intact for cancelled runs.
- Return-shape parity: `{"success": True, "message": ..., "run_id": ..., "status": "cancelled", "terminal_reason": "user_cancelled"}`.

Step 2 — shrink `stop_agent` in `agents.py` to a one-line delegate. Inside `create_agents_registry`'s closure (so it still captures `runner`, `agent_run_manager`, `db`, `lifecycle_monitor`, `completion_registry`, `task_manager`, `session_manager`, `hook_manager_resolver`, `_kill_agent_process`, `_cleanup_terminal_artifacts`):

```python
# In src/gobby/mcp_proxy/tools/agents.py, replace the existing stop_agent
# body (lines 393–453) with this delegate. Add the import alongside the
# existing terminalize_* imports at the top of the module.
from gobby.mcp_proxy.tools.agent_cancellation import stop_agent_run

@registry.tool(...)  # existing decoration unchanged
async def stop_agent(run_id: str) -> dict[str, Any]:
    """Stop a running agent. Body delegates to the shared helper."""
    return await stop_agent_run(
        run_id=run_id,
        runner=runner,
        agent_run_manager=agent_run_manager,
        db=db,
        lifecycle_monitor=lifecycle_monitor,
        completion_registry=completion_registry,
        task_manager=task_manager,
        session_manager=session_manager,
        hook_manager_resolver=hook_manager_resolver,
        kill_agent_process=_kill_agent_process,
        cleanup_terminal_artifacts=_cleanup_terminal_artifacts,
    )
```

No external behavior change — this is a pure extract. Verify by running `tests/mcp_proxy/tools/test_agents.py::TestStopAgent` unchanged after the refactor.

Step 3 — add the new public MCP tool in the same factory closure, using the same `@registry.tool(...)` decorator pattern as the surrounding tools (`stop_agent`, `kill_agent`, `end_agent_run`, etc.) — HEAD's `create_agents_registry` constructs `registry = InternalToolRegistry(...)` and registers tools via that decorator. There is no `server` variable in this closure; using `@server.tool()` would be a NameError:

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
            result = await stop_agent_run(
                run_id=run.id,
                runner=runner,
                agent_run_manager=agent_run_manager,
                db=db,
                lifecycle_monitor=lifecycle_monitor,
                completion_registry=completion_registry,
                task_manager=task_manager,
                session_manager=session_manager,
                hook_manager_resolver=hook_manager_resolver,
                kill_agent_process=_kill_agent_process,
                cleanup_terminal_artifacts=_cleanup_terminal_artifacts,
            )
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

**Monolith limit verification (required, not optional)**: after the implementer applies the §2.5 edits, `wc -l src/gobby/mcp_proxy/tools/agents.py` MUST return a value strictly less than 1,000. Pre-edit HEAD is 956 lines; the post-edit expected footprint is ~960 lines (delete the existing `stop_agent` body (~60 lines), add the small `cancel_stale_helpers` registration (~60 lines), add the one-line delegate to `stop_agent_run` — net change ≈ +5 lines). `wc -l src/gobby/mcp_proxy/tools/agent_cancellation.py` MUST also stay under 1,000 — expected post-edit footprint is ~200 lines (current 131 + ~70 for `stop_agent_run`). The hard gate is the project's 1,000-line non-test Python monolith limit (guiding principle #2). If the planned edits would make either file reach 1,000 lines, the implementer must extract further or file a follow-up refactor task rather than inline more code. The ~960-line expected outcome for `agents.py` is within the limit; treat the remaining ~40-line headroom as advisory (future edits to the file should be mindful of the limit, but the §2.5 implementation itself is not expected to escalate).

Validation criteria: tool callable via `mcp__gobby__call_tool(server_name="gobby-agents", tool_name="cancel_stale_helpers", arguments={"parent_session_id": "#X", "agent_name": "memory-recall-helper"})`. With no running helpers for `#X`, returns `{"success": True, "cancelled": [], "errors": [], "count": 0}`. With one running helper for `#X`, returns `{"success": True, "cancelled": ["run-…"], "errors": [], "count": 1}`, the run's `agent_runs.status` becomes `cancelled` (DB-verifiable), AND the helper's tmux pane is dead (per `cleanup_terminal_artifacts` invoked from `stop_agent_run`). With two stale helpers where stopping the first raises an exception, the second is still cancelled and `errors` contains the first's failure — best-effort guarantee. Missing `parent_session_id` or `agent_name` returns `{"success": False, "error": "..."}`. **Cleanup-step parity**: integration test that asserts `stop_agent_run` invokes `kill_agent_process(..., close_terminal=True)`, then `terminalize_cancelled_agent_run(...)` (which internally chooses between `lifecycle_monitor.terminalize_cancelled_run(...)` and the fallback `runner.cancel_run(...)` + `completion_registry.notify(...)` + task-claim recovery), then `cleanup_terminal_artifacts(...)` — in that order — for every successful path. Use mocks/spies on these injected kwargs and assert call order; this protects against accidentally dropping a step during the extract. **db-less registry contract test (round 10 guard)**: instantiate `create_agents_registry(runner=mock_runner, db=None, ...)` where `mock_runner.run_storage.list_by_parent` returns a list with one running helper. Call the registered `cancel_stale_helpers` tool with the corresponding parent and `agent_name="memory-recall-helper"`. Assert (a) no `db is None` failure is raised, (b) the cancellation succeeds via the runner.run_storage fallback. Without reusing the closure's `agent_run_manager`, this test fails. Existing `stop_agent` still works identically (delegates to `stop_agent_run` now); existing tests in `tests/mcp_proxy/tools/test_agents.py` (notably `TestStopAgent`) pass without modification. New unit test `tests/mcp_proxy/tools/test_cancel_stale_helpers.py` covers all the cases above. **Module placement test**: `tests/mcp_proxy/tools/test_agent_cancellation.py` gains a direct unit test for `stop_agent_run(...)` that constructs all eleven injected kwargs as `MagicMock`/`AsyncMock` instances and asserts the cleanup-step order for a happy path (run is pending → returns success). This proves the helper is callable standalone, independent of the registry closure, and locks in its public-function contract so a future re-inlining attempt has to delete this test first. **Line-count test**: a guard test under `tests/mcp_proxy/tools/test_agents.py` (or alongside the existing `TestStopAgent`) reads `Path(src/gobby/mcp_proxy/tools/agents.py).read_text().count("\n")` and asserts the result is strictly less than 1,000. This is a forward-compat guard so a future PR that inlines significant code into `agents.py` fails CI rather than silently breaking the monolith limit.

**Acceptance:**

- 2.5.1 — `stop_agent_run` module-level public function added to `src/gobby/mcp_proxy/tools/agent_cancellation.py` containing the shared cancellation body (kill, terminalize, terminal cleanup) with deps as injected kwargs. symbol: `gobby.mcp_proxy.tools.agent_cancellation.stop_agent_run`.
- 2.5.2 — `stop_agent` MCP tool in `agents.py` delegates to `stop_agent_run(...)` with no behavior change. symbol: `gobby.mcp_proxy.tools.agents.stop_agent`.
- 2.5.3 — `cancel_stale_helpers` MCP tool registered on the `gobby-agents` server and delegates to `stop_agent_run(...)` for each stale run. symbol: `gobby.mcp_proxy.tools.agents.cancel_stale_helpers`.
- 2.5.4 — Best-effort cancellation: per-run failures are reported in `errors[]` while other runs still get cancelled. test: `tests/mcp_proxy/tools/test_cancel_stale_helpers.py::test_best_effort_continues_on_per_run_failure`.
- 2.5.5 — Cleanup-step parity integration test asserts `kill_agent_process` → `terminalize_cancelled_agent_run` → `cleanup_terminal_artifacts` runs in order. test: `tests/mcp_proxy/tools/test_cancel_stale_helpers.py::test_cleanup_step_order_parity_with_stop_agent`.
- 2.5.6 — db-less registry contract test asserts the closure's `agent_run_manager` falls back to `runner.run_storage` when `db` is None. test: `tests/mcp_proxy/tools/test_cancel_stale_helpers.py::test_db_less_registry_uses_runner_run_storage`.
- 2.5.7 — Direct unit test of `stop_agent_run` constructs all injected kwargs as mocks and asserts the cleanup-step call order on the happy path. test: `tests/mcp_proxy/tools/test_agent_cancellation.py::test_stop_agent_run_happy_path_call_order`.
- 2.5.8 — Monolith-limit guard test asserts `agents.py` line count is strictly less than 1,000 after the §2.5 edits. test: `tests/mcp_proxy/tools/test_agents.py::test_agents_py_under_monolith_limit`.

### 2.6 Add `notify_parent_on_completion` to `spawn_agent` [category: code]

`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py` (add the tool parameter and gate the completion subscription).
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py` (add subscription-default and subscription-disabled tests).
- `tests/mcp_proxy/tools/test_agents.py` or `tests/events/test_wake.py` (add an end-to-end/unsubscribed completion regression, whichever fixture already owns the shortest path through `end_agent_run` + completion notifications).

`spawn_agent` currently subscribes the parent session to every child run completion when `parent_session_id` is supplied (`subscribe_agent_completion(...)` near the end of `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`). That default is correct for ordinary delegated agents, but it is wrong for `memory-recall-helper`: the parent task requires the helper to send 0–3 selected memories or finish silently. A generic completion notification is neither a selected memory nor silence, and filtering it later in `deliver-pending-messages` would be too late because the live wake and durable `completion_notification` row have already been produced.

Implementation shape:

```python
async def spawn_agent(
    ...,
    parent_session_id: str | None = None,
    project_path: str | None = None,
    notify_parent_on_completion: bool = True,
) -> dict[str, Any]:
    ...
    if (
        notify_parent_on_completion
        and result.get("success")
        and run_id
        and completion_registry
        and resolved_parent_session_id
    ):
        subscribe_agent_completion(...)
```

Keep the default `True` to preserve existing agent-spawn behavior. The option must NOT remove or change `parent_session_id`: lineage, `agent_runs.parent_session_id`, cancellation by parent, and build/task coordination still need the parent relationship. This option controls only completion subscription/wake behavior.

Validation criteria: the generated `gobby-agents.spawn_agent` schema includes `notify_parent_on_completion` with type boolean and default `true`. A focused factory test calls `spawn_agent(..., parent_session_id=<parent>, notify_parent_on_completion=False)` with a mock `completion_registry` and asserts `subscribe_agent_completion` is not called while the run still records/receives `parent_session_id`. A sibling regression calls with the argument omitted and asserts the existing default subscription still happens. A completion-path regression proves an unsubscribed child run that calls `end_agent_run` does not create a parent `completion_notification` inter-session message/wake: use the shortest existing fixture path, but the assertion must inspect the durable pending-message table or wake dispatcher call, not just a mock return value. This is the load-bearing proof for the parent task's "finish silently" requirement.

**Acceptance:**

- 2.6.1 — `spawn_agent` accepts `notify_parent_on_completion: bool = True` and uses it only to guard `subscribe_agent_completion`; `parent_session_id` lineage remains unchanged. symbol: `gobby.mcp_proxy.tools.spawn_agent._factory.create_spawn_agent_registry`.
- 2.6.2 — Schema/default test proves `notify_parent_on_completion` is exposed as a boolean defaulting to `true`. test: `tests/mcp_proxy/tools/spawn_agent/test_factory.py::test_spawn_agent_schema_includes_notify_parent_on_completion_default_true`.
- 2.6.3 — Disabled-subscription test proves `notify_parent_on_completion=False` skips `subscribe_agent_completion` while preserving parent session linkage. test: `tests/mcp_proxy/tools/spawn_agent/test_factory.py::test_spawn_agent_notify_parent_on_completion_false_skips_subscription`.
- 2.6.4 — Default-behavior regression proves ordinary parented agent spawns still subscribe the parent when the option is omitted. test: `tests/mcp_proxy/tools/spawn_agent/test_factory.py::test_spawn_agent_notify_parent_on_completion_defaults_to_subscribe`.
- 2.6.5 — Silent-completion regression proves an unsubscribed helper that calls `end_agent_run` creates no parent `completion_notification` pending message/wake. test: `tests/mcp_proxy/tools/test_agents.py::test_unsubscribed_memory_helper_end_agent_run_does_not_notify_parent` (or the equivalent `tests/events/test_wake.py` test if that fixture is the existing owner of durable completion notifications).

## P3 Phase 3: Wiring

`kind: framing`

**Goal**: At every parent `turn_start`, in priority order: increment turn counter (3.4 at priority 1) → cancel any stale helper (3.2 at priority 5) → deliver pending P2P messages with dedup, cancelled-session filter, origin_turn_seq freshness filter, and cancel-incomplete filter (3.1 at priority 10) → spawn fresh helper for the new prompt with the current `parent_turn_seq` baked into its prompt and completion notifications disabled (3.3 at priority 12). This rule ordering is what makes the freshness contract correct: by the time delivery runs, the counter has advanced and any stale helper is already DB-marked `cancelled` or detected as still running, and 2.4's delivery formatter applies all three freshness guards before injecting any helper memory_recall payload. 2.6's `notify_parent_on_completion: false` wiring separately ensures a helper that sends no memories produces no generic completion-notification noise.

**Co-tenant rule note (Phase 3 timing context).** The priority slots used by 3.1–3.4 are not exclusive owners. At HEAD the same `turn_start` event also fires `reset-subagent-flag` (priority 5, gated on `is_subagent`), `prepare-clear-handoff` (priority 5, gated on `/clear` or `/exit`), `bootstrap-session-title-on-prompt` (priority 9, no-op for established sessions), `memory-recall-on-prompt` (priority 10, the fast-recall path 2.4 also dedupes), and `handle-plan-mode-entry` (priority 10, gated on plan-mode entry). None of those touch `parent_turn_seq`, `injected_memory_ids`, or the helper-run agent type, so they don't interfere with the contract. Order within the same priority is undefined, but the contract only requires ordering BETWEEN priorities (1 < 5 < 10 < 12), which the rule engine guarantees.

**Phase 3 entry criteria (operational, NOT DB-enforced; verify before claiming any Phase 3 task):**

Phase 3 wires up rules and an agent definition that REFERENCE Phase 1 and Phase 2 outputs. None of Phase 3 will function correctly until those outputs are merged. Per the round-8 adversary finding, the current task expander does NOT deterministically emit cross-phase `tasks.dependencies` edges from header annotations, so we cannot rely on the dependency engine to block Phase 3 on Phase 1/2 outputs. The implementer is operationally responsible for this gating. Before claiming or working any Phase 3 task, verify ALL of:

- Phase 1: 1.2 (`MemoryRecallHelperConfig`) merged. 1.3 (config thread + `parent_turn_seq` seed in `_session_start/agents.py`) merged. 1.4 (helper YAML) present in `src/gobby/install/shared/workflows/agents/memory-recall-helper.yaml` and synced to `workflow_definitions` (verifiable via `gobby agents show memory-recall-helper --json`). The earlier monolith-gate task (formerly 1.1) is dropped from this revision because the underlying `_session_start.py` decomposition already landed; no gate is required.
- Phase 2: 2.1 (`send_message` `from_session` default), 2.2 (`_check_agent_tool_enforcement` reorder), 2.3 (`get_cancelled_session_ids`), 2.4 (delivery + same-turn dedup formatters in `EffectsMixin`), 2.5 (`cancel_stale_helpers` MCP tool), and 2.6 (`spawn_agent.notify_parent_on_completion`) ALL merged. Verify 2.5 by calling `mcp__gobby__call_tool(server_name="gobby-agents", tool_name="cancel_stale_helpers", arguments={"parent_session_id":"#<self>","agent_name":"memory-recall-helper"})` and observing a successful `{"success": true, ...}` response. Verify 2.6 by checking `get_tool_schema(server_name="gobby-agents", tool_name="spawn_agent")` includes `notify_parent_on_completion` as a boolean defaulting to `true`. (Note: the wrapper schema uses `server_name` and `tool_name`, NOT `server`/`tool` — the latter are valid only inside rule `mcp_call` effects, not the top-level wrapper call.)

If any output is missing, escalate the Phase 3 task with a specific reason naming the missing output. Do NOT proceed.

**Intra-phase dependencies inside Phase 3** (which the expander handles reliably for same-phase deps): 3.3 (spawn rule) depends on 3.1 (deliver), 3.2 (cancel), and 3.4 (counter) — all same-phase. 3.4 (counter) depends on 3.2 (cancel) to serialize the shared `tests/workflows/test_memory_recall_helper_ordering.py` and `tests/workflows/test_memory_lifecycle_rules.py` test-file ownership — §3.2 creates both extension points (the new ordering test module and the `MEMORY_RULES` set update with the cancel-rule entry), and §3.4 extends them with the counter-rule test class and `MEMORY_RULES` entry. Without this edge, expanding §3.2 and §3.4 in parallel would race on file creation and on the shared `MEMORY_RULES` literal at merge time (round-5 F2). 3.5 (integration tests) depends on 3.1, 3.2, 3.3, 3.4 — all same-phase; 3.5 owns the cancel-vs-deliver ordering regression test (moved out of §3.2 in round 5) plus the cross-rule sensitivity and freshness end-to-end tests. 3.1 has no Phase 3 deps (touches an existing rule). 3.2 has no Phase 3 deps. These same-phase deps are encoded in the section headers below.

### 3.1 Modify `deliver-pending-messages` rule to fire for parent sessions [category: config]

`kind: deliverable`

**Cross-phase preconditions (operational; verify before editing): 2.4 merged.** This rule's behavior is meaningless without 2.4's `_format_delivery_result` formatter — without it the inline `inject_result: true` path injects raw `messages[*].content` JSON. Without 3.4 merged (the priority-1 counter rule), the `parent_turn_seq` variable is missing, which 2.4 treats as fail-closed (drops all `memory_recall` payloads with a warning). 3.1 itself does not technically depend on 3.4 at expansion time (no same-phase edge), but the e2e behavior is tested only after 3.4 is also wired.

Targets:
- `src/gobby/install/shared/workflows/rules/messaging/deliver-pending-messages.yaml` (existing file — primary edit).
- `tests/workflows/test_messaging_rules.py` (`TestDeliverPendingMessages` updated to assert the new contract).
- `tests/e2e/test_inter_agent_messages.py` (regression coverage — `test_parent_child_message_exchange` must continue to pass).

Current file (HEAD) has:

```yaml
when: "variables.get('is_spawned_agent') and event.metadata.get('_platform_session_id')"
```

and an `arguments:` block already templated from `_platform_session_id` (no `inject_result`). Three changes:

1. Drop ONLY the `variables.get('is_spawned_agent')` conjunct from `when:` so the rule fires for parents too (the underlying tool is session-scoped). KEEP the `event.metadata.get('_platform_session_id')` conjunct — it guards against firing when the platform session id has not yet been resolved (legitimate during the first event of a session before the platform-id propagation completes), which would render `target_session_id` to an empty string and explode the tool call. After this change the `when:` clause is `"event.metadata.get('_platform_session_id')"`.
2. The existing `arguments: { target_session_id: "{{ event.metadata.get('_platform_session_id', '') }}" }` block stays — the templated reference is already correct on HEAD. Confirm the template still resolves `_platform_session_id` (canonical Gobby session row id), NOT `event.session_id` (CLI external id — Claude `external_id` / Codex `thread_id`). If a future edit ever swaps to `event.session_id`, this rule breaks for any session where the external id differs from the platform id.
3. Add `inject_result: true` to the `mcp_call` effect. Phase 2.4's pipeline is the consumer of `inject_result` for this tool.

Replace the entire file contents with:

```yaml
tags: [messaging, p2p, commands, gobby, default]

rules:
  deliver-pending-messages:
    description: "Deliver pending inter-session messages on each agent turn"
    event: turn_start
    enabled: true
    when: "event.metadata.get('_platform_session_id')"
    priority: 10
    effects:
      - type: mcp_call
        server: gobby-agents
        tool: deliver_pending_messages
        arguments:
          target_session_id: "{{ event.metadata.get('_platform_session_id', '') }}"
        inject_result: true
```

Existing tests touching this rule (`tests/e2e/test_inter_agent_messages.py::test_parent_child_message_exchange`) must continue to pass — child → parent and parent → child messaging both still rely on this rule, so gate removal must not regress those flows.

Validation criteria: file at the listed path matches the YAML above exactly. Daemon restart loads the rule; `gobby rules show deliver-pending-messages --json` returns a payload where (a) `when` (string) contains `event.metadata.get('_platform_session_id')` AND does NOT contain `is_spawned_agent` (the agent gate is dropped, the platform-session-id presence guard is kept), (b) `enabled` is `true`, (c) `priority` is `10`, (d) `event` is `turn_start`, (e) `effects[0].type` is `mcp_call`, `effects[0].server` is `gobby-agents`, `effects[0].tool` is `deliver_pending_messages`, `effects[0].inject_result` is `true`, and `effects[0].arguments.target_session_id` is the literal templated string `"{{ event.metadata.get('_platform_session_id', '') }}"` (NOT `"{{ event.session_id }}"` — the external id resolution path is wrong here). (`gobby rules list` only returns summaries — name/event/priority/enabled — and CANNOT verify `when`/`arguments`/`inject_result`. Use `gobby rules show <name> --json` for structural assertions; `gobby rules list` is acceptable only as an existence check.)

**Rule-definition tests (required, not optional)**: update `tests/workflows/test_messaging_rules.py::TestDeliverPendingMessages` (which currently hard-codes the old `is_spawned_agent` gate and no-`inject_result` effect) to assert the new contract:

- `when:` clause does NOT contain `is_spawned_agent` but DOES contain `event.metadata.get('_platform_session_id')` (string match on `rule.condition`).
- Effect's `arguments` field equals `{"target_session_id": "{{ event.metadata.get('_platform_session_id', '') }}"}` (string match on the templated value, exactly as written in the YAML — must NOT contain the external-id form `"{{ event.session_id }}"`).
- Effect's `inject_result` field is `True`.
- Effect's `server` is `"gobby-agents"` and `tool` is `"deliver_pending_messages"`.
- Rule's `event` is `"turn_start"` and `priority` is `10`.

`tests/e2e/test_inter_agent_messages.py` passes after the change.

A manual end-to-end test (must include valid `origin_turn_seq` to match 2.4's fail-closed freshness guard B — without it, the formatter drops the payload; memory content must carry a unique sentinel string because the search_memories formatter at `src/gobby/hooks/dispatchers/mcp.py:148-167` renders `content` not `id`):
1. Read `current_seq = get_variable(name="parent_turn_seq", session_id=#<self>)`.
2. From a parent session, call `send_message(target="session", target_id=#<self>, content='{"type":"memory_recall","origin_turn_seq":<current_seq>,"memories":[{"id":"test1","content":"manual-e2e-content-sentinel-fresh"}],"rationale":"manual"}')` (omitting `from_session` to verify 2.1's auto-fill; note the canonical post-2.1 signature is `target="session"` + `target_id=<session_id>` — there is no `to_session` parameter). The memory's `content` field MUST be a unique sentinel string ("manual-e2e-content-sentinel-fresh" or equivalent) because the search_memories formatter renders `content` not `id` — checking for the id `"test1"` in the rendered context is unreliable. The `origin_turn_seq` value MUST equal the parent's current `parent_turn_seq` because the next `turn_start` will increment it to `current_seq + 1` (via 3.4's priority-1 rule), and the formatter's filter accepts payloads where `origin_turn_seq == new_seq - 1 == current_seq`.
3. Trigger a `turn_start`. Result is (a) the content sentinel `"manual-e2e-content-sentinel-fresh"` appears ONCE in the parent's injected context (rendered via the search_memories formatter as the memory's `content`); the id `"test1"` does NOT appear in the rendered context because the formatter does not emit ids, (b) `get_variable(name="injected_memory_ids", session_id=#<self>)` contains `"test1"` (the id, since the state variable stores ids) after the turn, (c) repeating the same payload with the same (now-stale) `origin_turn_seq` and another `turn_start` results in the content sentinel NOT appearing in the second turn's context (no re-injection) — both because the turn-seq guard now drops it AS stale (origin no longer matches new_seq - 1) AND because `injected_memory_ids` already contains `"test1"`.
4. **Negative case (proves fail-closed guard B)**: send a `memory_recall` payload with NO `origin_turn_seq` field but content sentinel `"manual-e2e-content-sentinel-no-origin"`, then trigger `turn_start`; result is the payload is dropped (the sentinel string does NOT appear in the parent's context, `injected_memory_ids` is not mutated), with a warning logged.
5. **Negative case (proves scoped guard A)**: from a different child session whose corresponding `agent_runs.status` is `cancelled` and `agent_name='other-agent'` (NOT memory-recall-helper), `send_message` a plain text P2P payload containing distinct sentinel `"manual-e2e-plain-cancelled-sentinel"` (NOT a memory_recall envelope) to the parent. Trigger `turn_start`. Result: the plain message renders via the generic formatter — the sentinel string appears in the parent's context, proving the cancelled-session filter does not silently discard non-helper child output. A `memory_recall` payload from a `cancelled` `memory-recall-helper` run, by contrast, IS dropped — its content sentinel does NOT appear in the parent's context.

A turn_start with no pending messages results in NO `inject_result` noise in the parent's context.

**Acceptance:**

- 3.1.1 — `deliver-pending-messages.yaml` drops only the `is_spawned_agent` conjunct from `when:` (keeping the `_platform_session_id` presence guard) and sets `inject_result: true` on the effect. file: `src/gobby/install/shared/workflows/rules/messaging/deliver-pending-messages.yaml`.
- 3.1.2 — Rule-definition test asserts the new contract (`when:` contains `_platform_session_id` and NOT `is_spawned_agent`, exact `arguments` shape, `inject_result: true`). test: `tests/workflows/test_messaging_rules.py::TestDeliverPendingMessages`.
- 3.1.3 — `tests/e2e/test_inter_agent_messages.py` continues to pass after the gate removal. test: `tests/e2e/test_inter_agent_messages.py::test_parent_child_message_exchange`.

### 3.2 Create `cancel-stale-memory-recall-helpers` rule (priority 5, before delivery) [category: config]

`kind: deliverable`

**Cross-phase precondition (operational; verify before claiming): 2.5 merged.** This rule invokes `cancel_stale_helpers` which is added in 2.5. Verify the tool exists by `list_tools(server_name='gobby-agents')` showing `cancel_stale_helpers` in the result before working this task.

Targets:
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/cancel-stale-memory-recall-helpers.yaml` (new file — primary deliverable).
- `tests/workflows/test_memory_lifecycle_rules.py` (add `TestCancelStaleMemoryRecallHelpers` class and extend the `MEMORY_RULES` set).
- `tests/workflows/test_memory_recall_helper_ordering.py` (new file — ordering and session-id resolution regression tests).
- `src/gobby/hooks/events.py` (referenced only — `HookEventType.BEFORE_AGENT` is consumed by the regression test fixture; no edit).

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
      and event.metadata.get('_platform_session_id')
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

Validation criteria: file at the listed path; daemon restart loads the rule; `gobby rules show cancel-stale-memory-recall-helpers --json` returns a payload where `priority` is `5`, `event` is `turn_start`, `enabled` is `true`, `when` (string) contains `is_spawned_agent` AND `_platform_session_id` (presence guard, parallels 3.1) AND does NOT contain `memory_recall_helper_enabled` (the toggle gate must NOT be on this rule — see freshness rationale above), `effects[0].type` is `mcp_call`, `effects[0].server` is `gobby-agents`, `effects[0].tool` is `cancel_stale_helpers`, `effects[0].arguments.parent_session_id` is `"{{ event.metadata.get('_platform_session_id') }}"` (canonical platform id, NOT external `"{{ event.session_id }}"`), `effects[0].arguments.agent_name` is `"memory-recall-helper"`, AND `effects[0].inject_result` is `true` (this is the sync marker — the formatter returns None so it injects nothing).

**Session-id resolution test (required, not optional — cancel-rule scope; sole §3.2 test file owner of `tests/workflows/test_memory_recall_helper_ordering.py`)**: this deliverable creates the `tests/workflows/test_memory_recall_helper_ordering.py` test module. Scope is limited to the cancel rule's own `parent_session_id` resolution — asserting cancel + deliver co-loaded ordering or cancel + deliver + spawn together requires those other rules to be loaded alongside, and §3.3's spawn rule depends on §3.2, so cross-rule assertions cannot live here without creating an in-phase prerequisite/downstream cycle. The cancel + deliver ordering regression test AND the full three-rule sensitivity sweep are both owned by §3.5's integration deliverable (`(depends: 3.1, 3.2, 3.3, 3.4)`) — round-5 F2 moved the cancel-vs-deliver ordering test out of §3.2 for the same prerequisite/downstream reason (the ordering assertion needs `deliver-pending-messages` loaded with `inject_result: true`, which is §3.1's contribution).

The "turn_start" rule event in HEAD is `HookEventType.BEFORE_AGENT` (`src/gobby/hooks/events.py:33` — `BEFORE_AGENT = "before_agent"`); there is no `PROMPT_SUBMIT` member. Construct a `RuleEngine` loaded with ONLY the `cancel-stale-memory-recall-helpers` rule. Build:

```python
event = HookEvent(
    event_type=HookEventType.BEFORE_AGENT,
    session_id="external-X",                     # CLI external id (Claude external_id / Codex thread_id)
    source=SessionSource.CLAUDE_CODE,
    timestamp=datetime.now(timezone.utc),
    data={"prompt": "any prompt"},
    metadata={"_platform_session_id": "platform-Y"},  # canonical Gobby session row id, deliberately != session_id
)
```

Fire it via `RuleEngine.evaluate(event, session_id="platform-Y", variables={"servers_listed": True})` (the `servers_listed=True` short-circuit avoids auto-discovery side-rules polluting the dispatcher transcript). Stub `_mcp_dispatcher` to record `(server, tool, arguments)` for every inline dispatch; the stub MUST return the production envelope `{"success": True, "inject_result": True, "result": {"success": True, "cancelled": [], "errors": [], "count": 0}}` so `_apply_effect` at `effects.py:117` takes the success path rather than aborting remaining effects (round-11 F1).

Assert:

- (a) `cancel_stale_helpers` was dispatched inline with `arguments["parent_session_id"] == "platform-Y"` and NOT `"external-X"`.
- (b) Sensitivity flip: temporarily monkey-patch the cancel rule's argument template `event.metadata.get('_platform_session_id')` → `event.session_id`, re-run `RuleEngine.evaluate` with the same event, and assert the same assertion flips from pass to fail (now `"external-X"`). This proves the test actually exercises the resolution path rather than passing trivially.

This is the explicit partial guard against round-12 F1 at cancel-rule scope: any future PR that switches the cancel rule YAML from `event.metadata.get('_platform_session_id')` back to `event.session_id` fails this test. The cross-rule integration assertions across cancel + deliver + spawn are owned by §3.5's `test_three_rule_session_id_sensitivity_integration`.

**Rule-definition tests (required, not optional)**: add a new test class `TestCancelStaleMemoryRecallHelpers` to `tests/workflows/test_memory_lifecycle_rules.py` paralleling the structural assertions made for the spawn rule below. Add `"cancel-stale-memory-recall-helpers"` to the `MEMORY_RULES` set at the top of that file (line 33) so `TestMemoryLifecycleSync` covers it.

End-to-end (manual): start a session, submit a 6+-word prompt to spawn helper N. Before helper N completes, submit a second prompt. Observe in `agent_runs` that helper N's status transitions to `cancelled` (set by 2.5's tool, fired by this rule at priority 5) BEFORE `deliver-pending-messages` at priority 10 runs. If helper N had time to call `send_message`, observe in the parent's context at the second turn that the cancelled helper's memory payload was NOT injected.

**Acceptance:**

- 3.2.1 — `cancel-stale-memory-recall-helpers.yaml` exists with priority 5, `inject_result: true`, `parent_session_id` resolved from `_platform_session_id`, and `agent_name: memory-recall-helper`. file: `src/gobby/install/shared/workflows/rules/memory-lifecycle/cancel-stale-memory-recall-helpers.yaml`.
- 3.2.2 — Rule-definition test class `TestCancelStaleMemoryRecallHelpers` asserts the structural contract. test: `tests/workflows/test_memory_lifecycle_rules.py::TestCancelStaleMemoryRecallHelpers`.
- 3.2.3 — `MEMORY_RULES` set in the test module includes `cancel-stale-memory-recall-helpers`. file: `tests/workflows/test_memory_lifecycle_rules.py`.
- 3.2.4 — Session-id resolution test (cancel-rule scope) asserts the cancel rule's `parent_session_id` argument resolves to `event.metadata['_platform_session_id']`, never to external `event.session_id`, with a monkey-patched sensitivity flip on the cancel rule alone. Cross-rule sensitivity (cancel + deliver + spawn) and cancel-vs-deliver ordering are owned by §3.5. test: `tests/workflows/test_memory_recall_helper_ordering.py::test_cancel_rule_parent_session_id_resolves_to_platform_session_id`.

### 3.3 Create `spawn-memory-recall-helper` rule [category: config] (depends: 3.1, 3.2, 3.4)

`kind: deliverable`

**Cross-phase preconditions (operational; verify before claiming):** 1.3 merged (`parent_turn_seq` seeded; this rule reads it via `{{ variables.parent_turn_seq }}` in the helper prompt template). 1.4 merged (helper YAML; this rule references `agent: memory-recall-helper`). 2.2 merged (enforcement reorder; without it the helper's `blocked_tools` listing of `mcp__gobby__set_variable` does not actually take effect). 2.6 merged (`spawn_agent` accepts `notify_parent_on_completion`; without it the helper's `end_agent_run` would create generic parent completion notifications and violate the "finish silently" requirement). Verify 1.4 sync via `gobby agents show memory-recall-helper --json` returning a non-error payload before claiming this task, and verify 2.6 via `get_tool_schema(server_name="gobby-agents", tool_name="spawn_agent")`.

Targets:
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/spawn-memory-recall-helper.yaml` (new file — primary deliverable).
- `tests/workflows/test_memory_lifecycle_rules.py` (add `TestSpawnMemoryRecallHelper` class and extend the `MEMORY_RULES` set).

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
      and event.metadata.get('_platform_session_id')
      and variables.get('parent_turn_seq') is not none
    effects:
      - type: mcp_call
        server: gobby-agents
        tool: spawn_agent
        arguments:
          agent: memory-recall-helper
          parent_session_id: "{{ event.metadata.get('_platform_session_id') }}"
          notify_parent_on_completion: false
          prompt: |
            Parent session: {{ event.metadata.get('_platform_session_id') }}
            origin_turn_seq: {{ (variables.parent_turn_seq | default(0)) | int }}
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
- `when: event.metadata.get('_platform_session_id')` — presence guard mirroring 3.1 and 3.2. Without it the rule fires on the rare first-event-of-session race where platform-id resolution is still in flight, passing an empty string into `parent_session_id` and tripping spawn_agent's input validation.
- `when: variables.get('parent_turn_seq') is not none` — presence guard for the seed written by 1.3 at the session-start flow level. If flow-level seeding regresses or is bypassed, `parent_turn_seq` is absent and the prompt template would default `origin_turn_seq` to 0. That defaulted value can accidentally match `current_parent_turn_seq - 1` on the next turn, causing 2.4's guard B to false-accept a stale payload. Refusing to spawn when the seed is missing is the safe posture — the helper feature is unusable without it anyway.
- Behavioral note (accepted design tradeoff): the helper picks memories at prompt N and delivers them at prompt N+1. If N and N+1 are topically unrelated, the helper's selection is irrelevant noise rather than useful context. The plan's "Helper must run backgrounded" constraint rules out the alternative (block turn_start on helper completion), so this latency-for-relevance trade is intentional. The `memory_recall_helper.enabled` kill-switch lets users opt out if the prompt mix is too topic-volatile for the helper to be useful.
- `background: true` — `mcp_call` effect runs without blocking turn_start (per `src/gobby/workflows/engine/effects.py:142–166`).
- `notify_parent_on_completion: false` — preserves `parent_session_id` lineage while disabling the completion subscription added by ordinary parented `spawn_agent` calls. Without this, a helper that finds no memories but calls `end_agent_run` would create a parent `completion_notification` P2P/wake, which violates the parent task's "send 0–3 selected memories or finish silently" requirement. Do not solve this by filtering `completion_notification` in delivery; that would hide the injected context too late while the durable wake row already exists.
- `parent_session_id` and the prompt's `Parent session:` line are composed via `{{ event.metadata.get('_platform_session_id') }}` (NOT `event.session_id`); the user prompt body is composed via `{{ event.data.prompt }}`. The rule template engine exposes `event` directly per `_build_eval_context` (`src/gobby/workflows/engine/templating.py:36–105`), and `HookEvent.metadata` is a `dict[str, Any]` field on `HookEvent` (`src/gobby/hooks/events.py:85–124`), so `event.metadata.get(...)` resolves at template time. `event.session_id` is the CLI external id (Claude `external_id` / Codex `thread_id`); the helper needs the canonical Gobby platform session id for `get_session`, `get_variable`, and `injected_memory_ids` reads.
- The prompt explicitly tells the helper to omit `from_session` on `send_message` calls. 2.1's runtime change auto-fills it from the helper's SessionContext (the proxy populates it from the helper's session header), so the helper does not need to know its own child session id.
- The freshness contract — "at most one running helper per parent, no stale memory_recall payload ever injects regardless of how the prior helper terminated" — is owned by three guards: (A) 3.2 (`cancel-stale-memory-recall-helpers` at priority 5) + 2.4's cancelled-session filter, (B) 3.4 (priority-1 `parent_turn_seq` increment) + 2.4's `origin_turn_seq` freshness check, and (C) 2.4's cancel-incomplete check (detects still-running helpers after a failed cancel and drops `memory_recall` payloads — catches the immediate-next-turn gap where guard B alone would accept a payload from a not-cancelled stale helper). 3.3 itself stays simple: it always spawns. By the time 3.3 fires at priority 12, 3.4 has already incremented `parent_turn_seq`, 3.2 has cancelled any in-flight helper from the prior turn, and 3.1 has delivered the queue with both filters applied. There is no per-spawn `supersede` flag — that approach was rejected because (a) the rule-priority race meant delivery at 10 would inject stale payloads before a spawn-time supersede at 12 could cancel them, and (b) `spawn_agent`'s factory does not have access to the lifecycle/process-kill deps that proper cancellation requires.
- `origin_turn_seq: {{ variables.parent_turn_seq | int }}` is templated into the helper prompt. At priority 12, `parent_turn_seq` has already been incremented by 3.4 (priority 1), so the helper receives the CURRENT turn's number. The helper echoes that integer in its `memory_recall` payload. At the next parent turn_start, 2.4's delivery formatter compares the payload's echoed value against `current_parent_turn_seq - 1` (where `current_parent_turn_seq` is THIS turn's value, also already incremented). Match → fresh, accept. Mismatch (older or future) → stale, drop.

Validation criteria: file exists at the listed path; daemon restart loads the rule; `gobby rules show spawn-memory-recall-helper --json` returns a payload where (a) `enabled` is `true`, (b) `priority` is `12`, (c) `event` is `turn_start`, (d) `when` (string) contains all five guards as substrings: `event.data.get('prompt')`, `is_spawned_agent`, `memory_recall_helper_enabled`, `_platform_session_id` (presence guard parallels 3.1/3.2), and `variables.get('parent_turn_seq') is not none` (presence guard for the seed, so the helper never receives a defaulted `origin_turn_seq=0`), (e) `effects[0].type` is `mcp_call`, `effects[0].server` is `gobby-agents`, `effects[0].tool` is `spawn_agent`, `effects[0].background` is `true`, (f) `effects[0].arguments.agent` is `"memory-recall-helper"`, `effects[0].arguments.parent_session_id` is `"{{ event.metadata.get('_platform_session_id') }}"` (NOT external `"{{ event.session_id }}"`), `effects[0].arguments.notify_parent_on_completion` is `false`, `effects[0].arguments` does NOT contain a `supersede` key, and `effects[0].arguments.prompt` contains all three template references: `"{{ event.metadata.get('_platform_session_id') }}"` (the `Parent session:` line — NOT `"{{ event.session_id }}"`), `"{{ event.data.prompt }}"`, AND `"variables.parent_turn_seq"` somewhere inside a `default(0) | int` chain. **The plan MUST NOT contain any literal `{{ event.session_id }}` reference inside Phase 3 rule YAMLs or their validation criteria.** `gobby rules list` may be used as an existence check (it shows name/event/priority/enabled summary only) but cannot verify `when`/`arguments`/`effects` internals — use `--json` for those.

**Rule-definition tests (required, not optional)**: add a new rule-level test class `TestSpawnMemoryRecallHelper` to `tests/workflows/test_memory_lifecycle_rules.py` paralleling the existing `TestMemoryRecallOnPrompt` class in the same file (which is the closest structural analog — both are `turn_start` rules with a `when:` clause and a single `mcp_call` effect). The new class asserts the rule's contract:

- Rule's `event` is `"turn_start"`, `priority` is `12`, `enabled` is `True`.
- Rule's `condition` (the `when:` clause) contains all five guards as substrings (or parses to an AST including all five): (a) `len((event.data.get('prompt') or '').split()) >= 6`, (b) `not variables.get('is_spawned_agent')`, (c) `variables.get('memory_recall_helper_enabled', True)`, (d) `event.metadata.get('_platform_session_id')` (parallels 3.1/3.2 presence guard), (e) `variables.get('parent_turn_seq') is not none` (presence guard for the seed).
- Rule has exactly one effect of type `mcp_call`.
- Effect's `server` is `"gobby-agents"`, `tool` is `"spawn_agent"`, `background` is `True`.
- Effect's `arguments` includes `agent: "memory-recall-helper"`, `parent_session_id: "{{ event.metadata.get('_platform_session_id') }}"` (string match — NOT external `"{{ event.session_id }}"`), and `notify_parent_on_completion: false`. MUST NOT include `supersede` (the round-2 design that placed cancellation at spawn time was rejected — cancellation now lives in rule 3.2).
- Effect's `arguments.prompt` is a non-empty string containing the literal `"{{ event.metadata.get('_platform_session_id') }}"` (the `Parent session:` line), `"{{ event.data.prompt }}"`, AND `"{{ variables.parent_turn_seq"` (template references the helper needs). It MUST NOT contain `"{{ event.session_id }}"` anywhere.

Additionally, add `"spawn-memory-recall-helper"` to the `MEMORY_RULES` set defined at the top of `tests/workflows/test_memory_lifecycle_rules.py` (line 33). That manifest is consulted by `TestMemoryLifecycleSync` (lines 73, 82, 92) for cross-rule sync checks; omitting the new rule here would leave it outside the file's existing coverage net.

Behavioral validation: submitting a real prompt of ≥ 6 words to a parent (non-spawned-agent) session triggers a spawn — `gobby agents runs list --status running --json` shows a new run shortly after the prompt with `agent_name == "memory-recall-helper"` (use the JSON variant — plain text output may not include the agent name). Equivalent: `gobby agents runs show <run_id_prefix> --json` and assert `agent_name`. Direct-DB equivalent: query `agent_runs` for `agent_name='memory-recall-helper'` ordered by `created_at DESC` and inspect the most recent row. Submitting a 1-word prompt does not spawn. Manually setting `is_spawned_agent: true` on a session via `set_variable` and submitting a prompt does NOT spawn. Setting `memory_recall_helper.enabled: false` in the daemon config and restarting causes new sessions to NOT spawn the helper on prompts. The parent session's `turn_start` is not blocked — Claude Code starts streaming a response within the normal latency window (no Haiku-call wait inserted into the critical path). When the helper completes without `send_message`, no `completion_notification` inter-session message or wake is created for the parent. When the helper completes after `send_message`ing a `memory_recall` payload (omitting `from_session`), the parent's NEXT `turn_start` (a) injects only the helper's selected memories once via the search_memories formatter (NOT as raw JSON dump of the message body and NOT with any generic completion notification), (b) appends the surfaced IDs to the parent's `injected_memory_ids` (verifiable by `get_variable`), and (c) on a subsequent helper turn that re-selects those IDs, dedup filters them out before injection.

**Acceptance:**

- 3.3.1 — `spawn-memory-recall-helper.yaml` exists with priority 12, `event: turn_start`, the five-guard `when:` clause (prompt length, not-spawned-agent, helper-enabled, platform-session-id present, parent_turn_seq seeded), `background: true`, `notify_parent_on_completion: false`, and prompt template referencing `_platform_session_id`, `event.data.prompt`, and `(variables.parent_turn_seq | default(0)) | int`. file: `src/gobby/install/shared/workflows/rules/memory-lifecycle/spawn-memory-recall-helper.yaml`.
- 3.3.2 — `TestSpawnMemoryRecallHelper` rule-definition test asserts the contract, including `notify_parent_on_completion is False`, and that arguments do NOT include `supersede`. test: `tests/workflows/test_memory_lifecycle_rules.py::TestSpawnMemoryRecallHelper`.
- 3.3.3 — `MEMORY_RULES` set in the test module includes `spawn-memory-recall-helper`. file: `tests/workflows/test_memory_lifecycle_rules.py`.
- 3.3.4 — 6+-word prompts on parent (non-spawned-agent) sessions trigger a `memory-recall-helper` run; 1-word prompts and `is_spawned_agent` sessions do not. behavior: "spawn rule fires on prompt length >= 6 for non-spawned-agent parents only" in `src/gobby/install/shared/workflows/rules/memory-lifecycle/spawn-memory-recall-helper.yaml`.

### 3.4 Create `increment-parent-turn-seq` rule (priority 1, before all other turn_start rules) [category: config] (depends: 3.2)

`kind: deliverable`

**Cross-phase precondition (operational; verify before claiming): 1.3 merged.** This rule increments the `parent_turn_seq` session variable seeded at session_start by 1.3's flow-level seeding helper. Without 1.3 merged, the variable does not exist on new sessions and the rule must fail closed rather than self-create it. Verify 1.3 by checking that a fresh session has `variables['parent_turn_seq'] == 0` immediately after session_start (before any turn_start rule fires).

Targets:
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/increment-parent-turn-seq.yaml` (new file — primary deliverable).
- `tests/workflows/test_memory_lifecycle_rules.py` (add `TestIncrementParentTurnSeq` class and extend the `MEMORY_RULES` set).
- `tests/workflows/test_memory_recall_helper_ordering.py` (extend with the four-rule behavioral test).

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
      and variables.get('parent_turn_seq') is not none
    effects:
      - type: set_variable
        variable: parent_turn_seq
        value: "{{ (variables.parent_turn_seq | int) + 1 }}"
```

Why each clause:
- `priority: 1` — strictly lower than 3.2 (5), 3.1 (10), 3.3 (12). The increment must happen before the cancel rule, before the delivery formatter reads `variables['parent_turn_seq']`, and before the spawn rule reads `variables.parent_turn_seq` for the helper prompt. The rule engine evaluates rules in ascending priority order; `set_variable` is a synchronous in-memory effect (no async dispatch) so the result is immediately visible to subsequent rules.
- `when: not is_spawned_agent` — only parent sessions need this counter. Spawned helpers do not spawn other helpers, so they do not need the counter on their own turn_start.
- `when: variables.get('parent_turn_seq') is not none` — **fail-closed seed guard (round-9 F1 fix).** If `parent_turn_seq` was never seeded by §1.3's `_seed_memory_recall_helper_vars` (e.g., a session where `handler._session_manager is None` at activation time, or a pre-existing session created before this feature landed), the counter rule MUST NOT fire. Without this guard, the `default(0)` fallback in the value template would self-create `parent_turn_seq=1` on the first `turn_start`, which defeats §3.3's `parent_turn_seq is not none` spawn guard — the spawn rule would see a counter that was never legitimately seeded and fire in sessions where the runtime config was never applied. With this guard, the counter rule is a no-op when the seed is missing, §3.3's `parent_turn_seq is not none` guard correctly blocks the spawn, and the 2.4 delivery formatter's fail-closed behavior (drops all `memory_recall` payloads when `parent_turn_seq` is missing/non-int) prevents any stale payload from injecting. The corresponding `default(0)` has been removed from the value template because it is dead code when this guard is present — `variables.parent_turn_seq` is guaranteed to be non-None inside the effect.
- **Intentionally NOT gated on `memory_recall_helper_enabled`.** The counter must advance on every parent turn_start regardless of the toggle, otherwise a helper queued while enabled could send a payload during a disabled interval and have its `origin_turn_seq` accidentally match `current_parent_turn_seq - 1` when the feature is later re-enabled, injecting stale memory against an unrelated prompt. The cost of running this rule while the feature is disabled is one `set_variable` call per parent turn — negligible.
- `set_variable` with a Jinja arithmetic expression — `_render_template` produces a numeric string ("1", "2", …), `_coerce_rendered_value` converts to int. Verified path in HEAD at `src/gobby/workflows/engine/effects.py`'s `_apply_set_variable` (templates render before expression eval) and `_coerce_rendered_value` (string → int coercion).

Validation criteria: file at the listed path; daemon restart loads the rule; `gobby rules show increment-parent-turn-seq --json` returns a payload where `priority` is `1`, `event` is `turn_start`, `enabled` is `true`, `when` (string) contains `is_spawned_agent` AND `variables.get('parent_turn_seq') is not none` AND does NOT contain `memory_recall_helper_enabled` (counter must advance regardless of the toggle), `effects[0].type` is `set_variable`, `effects[0].variable` is `"parent_turn_seq"`, `effects[0].value` is the literal Jinja string `"{{ (variables.parent_turn_seq | int) + 1 }}"` (no `default(0)` — the `when:` guard guarantees non-None).

**Rule-definition tests (required, not optional)**: add `TestIncrementParentTurnSeq` to `tests/workflows/test_memory_lifecycle_rules.py` paralleling the structural assertions for the other helper rules. Add `"increment-parent-turn-seq"` to the `MEMORY_RULES` set at the top of that file so `TestMemoryLifecycleSync` covers it.

**Behavioral test (required — counter-rule scope)**: in `tests/workflows/test_memory_recall_helper_ordering.py` (the same file added in 3.2's regression test), construct a `RuleEngine` with ONLY the `increment-parent-turn-seq` rule loaded — scope is limited to the counter rule's own behavior because the cross-rule four-rule test would require §3.3's spawn rule, but §3.3 depends on §3.4, so co-loading here creates a prerequisite/downstream cycle. The full four-rule integration test (counter advance + spawn-prompt resolved value + memory_recall freshness inject/drop) is owned by §3.5 (`(depends: 3.1, 3.2, 3.3, 3.4)`).

Seed a session with `parent_turn_seq=0`. Fire two `turn_start` events. Assert: (a) after first turn_start, `variables['parent_turn_seq'] == 1`; (b) after second turn_start, `variables['parent_turn_seq'] == 2`; (c) firing a `turn_start` with `variables['is_spawned_agent'] == True` does NOT increment the counter (proves the `when: not is_spawned_agent` guard works); **(d) firing a `turn_start` with NO `parent_turn_seq` in `variables` (simulating an unseeded session) does NOT create the variable and does NOT increment anything — the rule's `when:` guard blocks it (round-9 F1 regression guard).** This assertion must fail if the `variables.get('parent_turn_seq') is not none` guard is removed from the rule's `when:` clause or if the `default(0)` fallback is reintroduced in the value template.

End-to-end (manual): submit two prompts. After the first turn_start, `get_variable(name="parent_turn_seq", session_id=#<self>)` returns `1`. After the second, returns `2`. The cross-rule manual check (helper's prompt for the second turn contains `origin_turn_seq: 2`) is owned by §3.5's integration deliverable.

**Acceptance:**

- 3.4.1 — `increment-parent-turn-seq.yaml` exists at priority 1 with `event: turn_start`, `when:` containing both `not is_spawned_agent` and `variables.get('parent_turn_seq') is not none`, and `set_variable` effect computing `(variables.parent_turn_seq | int) + 1` (no `default(0)` fallback). file: `src/gobby/install/shared/workflows/rules/memory-lifecycle/increment-parent-turn-seq.yaml`.
- 3.4.2 — `TestIncrementParentTurnSeq` rule-definition test asserts the contract: `when:` contains `parent_turn_seq) is not none` AND does NOT contain `memory_recall_helper_enabled`; value template does NOT contain `default(0)`. test: `tests/workflows/test_memory_lifecycle_rules.py::TestIncrementParentTurnSeq`.
- 3.4.3 — `MEMORY_RULES` set in the test module includes `increment-parent-turn-seq`. file: `tests/workflows/test_memory_lifecycle_rules.py`.
- 3.4.4 — Behavioral test (counter-rule scope) asserts: (a,b) two consecutive turn_starts with seeded `parent_turn_seq=0` increment 0→1→2; (c) an `is_spawned_agent` session does NOT increment; (d) a session with NO `parent_turn_seq` variable does NOT create or increment the variable (round-9 F1 regression guard). Cross-rule integration (spawn-prompt resolved value, memory_recall freshness inject/drop) is owned by §3.5. test: `tests/workflows/test_memory_recall_helper_ordering.py::test_increment_parent_turn_seq_counter_isolated`.

### 3.5 Phase 3 integration test: four-rule turn-sequence + session-id sensitivity + memory_recall freshness end-to-end [category: test] (depends: 3.1, 3.2, 3.3, 3.4)

`kind: deliverable`

**Cross-phase preconditions (operational; verify before claiming):** §3.1, §3.2, §3.3, §3.4 all merged, AND §2.4's `_format_delivery_result` formatter merged (the freshness inject/drop assertions exercise 2.4 inline). This deliverable owns the cross-rule integration tests that exercise the freshness contract end-to-end through all four turn_start rules. Splitting these out of the per-rule deliverables resolves the round-4 §3.2 session-id test / §3.4 counter test sequencing problems and the round-5 cancel-vs-deliver ordering sequencing problem: per-rule tests cannot assert behavior that requires other Phase 3 rules to be co-loaded (the spawn rule §3.3 already depends on §3.1, §3.2, §3.4, so cross-rule assertions inside any of those earlier deliverables would create a prerequisite/downstream cycle at expansion time; the cancel-vs-deliver ordering test relocated from §3.2 to §3.5.3 needed §3.1's `inject_result: true` change but §3.2 had no encoded dep on §3.1, which is the round-5 F2 reason for the move).

Targets:
- `tests/workflows/test_memory_recall_helper_ordering.py` (extend with the five integration tests — same file as §3.2.4 / §3.4.4, new top-level test functions).

This deliverable is `category: test` because its only artifact is the integration test suite. There is no new source file or YAML rule — all wiring lives in §3.1–§3.4; this deliverable is the proof that those four rules compose correctly when co-loaded.

Five integration tests cover the cross-rule behavior:

**Test 1 — three-rule session-id sensitivity (extension of §3.2.4 / round-12 F1 guard at cross-rule scope)**: construct a `RuleEngine` with the three argument-templating rules loaded: `cancel-stale-memory-recall-helpers` (3.2), `deliver-pending-messages` (3.1), and `spawn-memory-recall-helper` (3.3). Build a `HookEvent` with `session_id="external-X"` and `metadata={"_platform_session_id": "platform-Y"}` (the two ids deliberately differ — `session_id` is the CLI external id (Claude `external_id` / Codex `thread_id`); `_platform_session_id` is the canonical Gobby session row id):

```python
event = HookEvent(
    event_type=HookEventType.BEFORE_AGENT,
    session_id="external-X",
    source=SessionSource.CLAUDE_CODE,
    timestamp=datetime.now(timezone.utc),
    data={"prompt": "six or more words for helper spawn rule"},
    metadata={"_platform_session_id": "platform-Y"},
)
```

Fire via `RuleEngine.evaluate(event, session_id="platform-Y", variables={"memory_recall_helper_enabled": True, "parent_turn_seq": 7, "servers_listed": True})` (the `servers_listed=True` short-circuit avoids auto-discovery side-rules polluting the dispatcher transcript). Stub `_mcp_dispatcher` to record `(server, tool, arguments)` for every inline dispatch; the stub MUST return the production envelope for each tool (e.g. `{"success": True, "inject_result": True, "result": {"success": True, "cancelled": [], "errors": [], "count": 0}}` for `cancel_stale_helpers`, `{"success": True, "inject_result": True, "result": {"success": True, "messages": [], "count": 0}}` for `deliver_pending_messages`) so `_apply_effect` at `effects.py:117` takes the success path and invokes `format_discovery_result` rather than aborting remaining effects (round-11 F1).

Assert:

- (a) `cancel_stale_helpers` was dispatched inline with `arguments["parent_session_id"] == "platform-Y"` and NOT `"external-X"`.
- (b) `deliver_pending_messages` was dispatched inline with `arguments["target_session_id"] == "platform-Y"` and NOT `"external-X"`.
- (c) The `spawn_agent` call appears NOT in the inline dispatcher transcript (the spawn rule has `background: true`) but in `response.metadata["mcp_calls"]` (the deferred list). That deferred entry has `arguments["agent"] == "memory-recall-helper"`, `arguments["parent_session_id"] == "platform-Y"`, and `arguments["prompt"]` is a rendered string containing the literal substring `"Parent session: platform-Y"` (NOT `"Parent session: external-X"`) AND `"origin_turn_seq: 7"` (proving `parent_turn_seq` resolved through `_render_template`).
- (d) Sensitivity check: temporarily monkey-patch the three rule definitions in-test to substitute `event.metadata.get('_platform_session_id')` → `event.session_id` in the rendered argument templates, re-run `RuleEngine.evaluate` with the same event, and assert the same assertions flip from pass to fail (now resolving to `"external-X"`). This proves the test actually exercises the resolution path rather than passing trivially.

This is the explicit cross-rule guard against round-12 F1: any future PR that switches one or more of the rule YAMLs from `event.metadata.get('_platform_session_id')` back to `event.session_id` fails this test. §3.2.4 catches the cancel-rule-only regression in isolation; this test catches the cross-rule regression with all three argument-templating rules co-loaded.

**Test 2 — four-rule turn-sequence + memory_recall freshness end-to-end (extension of §3.4.4 parts c/d/e at cross-rule scope)**: construct a `RuleEngine` with all four Phase 3 rules loaded (3.4 priority 1, 3.2 priority 5, 3.1 priority 10, 3.3 priority 12). Stub `_mcp_dispatcher` for the cancel and deliver calls. **Critical: all stubs MUST return the production dispatcher envelope** `{"success": True, "inject_result": True, "result": <tool_payload>}`, NOT the raw tool payload directly. The production inline dispatcher at `src/gobby/hooks/factory.py:382-386` wraps every tool result in this envelope, and `_apply_effect` at `src/gobby/workflows/engine/effects.py:117` gates the formatter path on `dr.get("result")` — a raw payload without the `result` wrapper key causes the entire `format_discovery_result` branch to be skipped, making sub-scenario B's positive assertions fail (formatter never runs) and sub-scenario C's negative assertions pass trivially (nothing rendered = nothing to find). Default stub return values: `{"success": True, "inject_result": True, "result": {"success": True, "cancelled": [], "errors": [], "count": 0}}` for `cancel_stale_helpers`, `{"success": True, "inject_result": True, "result": {"success": True, "messages": [], "count": 0}}` for `deliver_pending_messages`; record every deferred `spawn_agent` call's arguments. The test is partitioned into three independent sub-scenarios that EACH reset `parent_turn_seq` and `injected_memory_ids` to a known pre-turn state before firing `turn_start`. State must NOT carry over between sub-scenarios — re-firing `turn_start` without a reset advances `parent_turn_seq` (the priority-1 increment rule fires every turn) and would invalidate any `origin_turn_seq` value chosen for a later sub-scenario. The round-5 F3 finding flagged this exact pitfall in the prior phrasing, where the implementer was told to "re-fire turn 2" with `origin_turn_seq=1` after `parent_turn_seq` had already advanced to 2, making the previously-fresh value stale.

**Sub-scenario A — baseline counter advance and spawn-prompt resolution.** Seed: `parent_turn_seq=0`, `memory_recall_helper_enabled=True`, `injected_memory_ids=[]`, deliver stub returns the empty payload wrapped in the production envelope: `{"success": True, "inject_result": True, "result": {"success": True, "messages": [], "count": 0}}`. Fire two consecutive `turn_start` events with `event.data["prompt"]` containing a `>=6`-word prompt so the spawn rule fires both times. Assert:

- (A.1) after first `turn_start`, `variables['parent_turn_seq'] == 1`.
- (A.2) after second `turn_start`, `variables['parent_turn_seq'] == 2`.
- (A.3) the deferred `spawn_agent` call's resolved `arguments["prompt"]` for turn 2 contains the literal substring `"origin_turn_seq: 2"` (verifies the spawn rule's `{{ (variables.parent_turn_seq | default(0)) | int }}` template reads the post-increment value because 3.4 at priority 1 fires before 3.3 at priority 12).

**Sub-scenario B — fresh memory_recall payload is accepted.** Reset state to the pre-turn-2 baseline: `parent_turn_seq=1`, `injected_memory_ids=[]`, `memory_recall_helper_enabled=True` (use a fresh `SessionVariableManager` instance keyed off `platform_session_id="platform-Y"` to clear residue from sub-scenario A; do NOT share state across sub-scenarios). Reconfigure the `deliver_pending_messages` stub to return the production envelope wrapping the tool payload: `{"success": True, "inject_result": True, "result": {"success": True, "messages": [{"from_session": "child-A", "content": json.dumps({"type": "memory_recall", "origin_turn_seq": 1, "memories": [{"id": "mem-fresh", "content": "content-sentinel-mem-fresh"}], "rationale": "fresh"})}], "count": 1}}`. The memory's `content` carries a unique sentinel because the existing `search_memories` formatter at `src/gobby/hooks/dispatchers/mcp.py:148-167` renders `m.get("content")` (not `m.get("id")`) — so rendered-context assertions check the content sentinel, and state assertions on `injected_memory_ids` check the id (round-6 F2). Fire ONE `turn_start` event with a `>=6`-word prompt. The priority-1 counter rule increments `parent_turn_seq` from 1 to 2 BEFORE the priority-10 deliver rule fires, so 2.4's expected `origin_turn_seq == current_parent_turn_seq - 1 == 1` matches the payload exactly. Assert:

- (B.1) §3.1's effect runs through §2.4's `_format_delivery_result` (verified by patching the formatter at `gobby.workflows.engine.effects.EffectsMixin._format_delivery_result` to record its invocation count and arguments).
- (B.2) the resulting injected `context_parts` contains the content sentinel `"content-sentinel-mem-fresh"` (rendered by the search_memories formatter as the memory's `content` field). It MUST NOT assert the id `"mem-fresh"` appears in the rendered output — the formatter does not emit ids.
- (B.3) `SessionVariableManager.get_variables(session_id="platform-Y")["injected_memory_ids"]` contains `"mem-fresh"` (the id) after the turn. The state variable stores ids, so this assertion uses the id rather than the content sentinel.

**Sub-scenario C — stale memory_recall payload is dropped.** Reset state again to the pre-turn-2 baseline: `parent_turn_seq=1`, `injected_memory_ids=[]`, `memory_recall_helper_enabled=True` (same fresh-`SessionVariableManager` posture as sub-scenario B; the two sub-scenarios are independent test setups, not a continuation). Reconfigure the `deliver_pending_messages` stub for `origin_turn_seq=0` (stale: 2.4's expected `current_parent_turn_seq - 1 == 1` does not match 0), wrapped in the production envelope: `{"success": True, "inject_result": True, "result": {"success": True, "messages": [{"from_session": "child-A", "content": json.dumps({"type": "memory_recall", "origin_turn_seq": 0, "memories": [{"id": "mem-stale", "content": "content-sentinel-mem-stale"}], "rationale": "stale"})}], "count": 1}}`. The unique content sentinel string lets the test assert the stale memory was NOT rendered (since the formatter emits `content`, absence-of-sentinel proves absence-of-rendering). Fire ONE `turn_start` event with a `>=6`-word prompt. The priority-1 counter rule increments `parent_turn_seq` to 2; 2.4's guard-B check rejects `origin_turn_seq=0` as stale. Assert:

- (C.1) injected `context_parts` does NOT contain the content sentinel `"content-sentinel-mem-stale"`. (Asserting absence of the id `"mem-stale"` is unreliable — the id is never rendered, so absence-of-id is true even when content was injected; the content-sentinel check is the load-bearing assertion.)
- (C.2) `SessionVariableManager.get_variables(session_id="platform-Y")["injected_memory_ids"]` is empty (NOT mutated by the dropped payload; this state assertion uses the id since the variable stores ids).
- (C.3) a debug log entry under the `gobby.workflows.engine.effects` logger mentions "Dropping stale memory_recall" with the rejected `origin_turn_seq=0` value visible in the log message.

This integration test proves that the four turn_start rules compose correctly to enforce the freshness contract: turn-seq increment (priority 1) → cancel stale (priority 5) → deliver with both filters (priority 10) → spawn fresh with current-turn `origin_turn_seq` (priority 12), and 2.4's `origin_turn_seq == current - 1` guard accepts fresh payloads and drops stale ones at delivery time. Splitting the original Test 2 into three independent sub-scenarios with explicit pre-turn-2 state resets resolves round-5 F3 — the prior single-flow phrasing implicitly required `parent_turn_seq` to stay at 2 across sub-scenarios while the priority-1 rule guarantees it advances on every `turn_start`.

**Test 3 — cancel-vs-deliver inline ordering (relocated from §3.2.4 per round-5 F2)**: construct a `RuleEngine` with both `cancel-stale-memory-recall-helpers` (priority 5, with `inject_result: true`) and `deliver-pending-messages` (priority 10, with `inject_result: true`) loaded. Stub `_mcp_dispatcher` to record `(server, tool, timestamp, arguments)` for each call (use `time.monotonic_ns()` for the timestamp so successive inline-await dispatches get strictly-ordered values). The recording stub MUST also return the production envelope `{"success": True, "inject_result": True, "result": <tool_payload>}` (e.g. `{"success": True, "inject_result": True, "result": {"success": True, "cancelled": [], "errors": [], "count": 0}}` for cancel, `{"success": True, "inject_result": True, "result": {"success": True, "messages": [], "count": 0}}` for deliver) — without it, `_apply_effect` sees a missing `result` key and the `format_discovery_result` branch is skipped, which changes the success/abort control flow (round-11 F1). Fire a `turn_start` event with `event.metadata["_platform_session_id"]="platform-Y"`. Assert:

- (3.a) the `cancel_stale_helpers` dispatch's `timestamp` strictly precedes the `deliver_pending_messages` dispatch's `timestamp`.
- (3.b) BOTH dispatches appear in the inline transcript (i.e., were inline-awaited); NEITHER appears in `response.metadata["mcp_calls"]` (the deferred list returned by `_evaluate_workflow_rules`).

This protects against a future regression where someone removes `inject_result: true` from the cancel rule (which would silently make it deferred and break the freshness contract). The original §3.2.4 narrative also included this test packaged in §3.2, but the prerequisite/downstream cycle — §3.2.4 needed §3.1's `inject_result: true` change without an encoded dep — is what triggered the round-5 F2 move to this deliverable. Both rules are loaded here as ordinary preconditions, valid because §3.5 already depends on §3.1 and §3.2.

**Test 4 — real-DB cancellation completes before delivery formatter runs (extension of Test 3, end-to-end)**: construct a `RuleEngine` with both `cancel-stale-memory-recall-helpers` (3.2) and `deliver-pending-messages` (3.1) loaded against a real test hub DB (not a stub). Pre-insert into `agent_runs` a row with `status='running'`, `agent_name='memory-recall-helper'`, `parent_session_id='platform-Y'`, and a unique `child_session_id` (e.g. `'child-stale'`). Pre-insert into `inter_session_messages` a queued row using the storage API: `InterSessionMessageManager(db).create_message(from_session='child-stale', to_session='platform-Y', content=json.dumps({"type": "memory_recall", "origin_turn_seq": <current-1>, "memories": [{"id": "mem-test4", "content": "content-sentinel-test4"}], "rationale": "test4"}))` (the storage model and table column is `to_session`, NOT `target_session_id` — the latter is the MCP tool parameter name at `deliver_pending_messages`'s FastMCP schema, not the DB column). The content sentinel is required because the search_memories formatter renders `content` (not `id`); asserting on the id would be unreliable. Construct the `RuleEngine` with an inline `mcp_dispatcher` wired to route `('gobby-agents', 'cancel_stale_helpers')` through the registered test `gobby-agents` tool (so `stop_agent_run` executes the full lifecycle chain against the real DB and transitions the run to `cancelled`) and route `('gobby-agents', 'deliver_pending_messages')` through the real `deliver_pending_messages` tool (so `InterSessionMessageManager.get_undelivered_messages` reads the pre-inserted row and `_format_delivery_result` runs inline with the real DB state). **The inline dispatcher MUST wrap each registered tool's raw result in the production envelope** `{"success": True, "inject_result": True, "result": <raw_tool_result>}` before returning it to `RuleEngine`, matching the contract at `src/gobby/hooks/factory.py:382-386`. Without this wrapping, `_apply_effect` at `effects.py:117` sees no `result` key and skips the `format_discovery_result` branch entirely — the formatter never runs and assertions pass trivially (round-11 F1). Without the inline dispatcher wiring itself, `inject_result: true` effects are deferred into `response.metadata["mcp_calls"]` and the cancellation/delivery chain under test never executes inline — the assertions would pass trivially because no formatter ever runs. Fire one `turn_start` event with `metadata={"_platform_session_id": "platform-Y"}`. Spy/patch `_format_delivery_result` to record `LocalAgentRunManager(self.db).get_run('<stale-run-id>').status` at the moment the formatter is first invoked. Assert:

- (4.a) at the moment `_format_delivery_result` runs, the stale run's `agent_runs.status` has already transitioned to `'cancelled'` (proving §2.5's `cancel_stale_helpers` MCP tool ran inline before delivery, and the DB write happened atomically before the formatter read it).
- (4.b) the queued `memory_recall` message from the cancelled `child-stale` session is dropped by 2.4's helper-cancellation guard A. Concretely: the rendered `context_parts` does NOT contain the content sentinel `"content-sentinel-test4"`, AND `SessionVariableManager(...).get_variables("platform-Y")["injected_memory_ids"]` does NOT contain `"mem-test4"`. Both assertions are required — the rendered-context check covers UX (was the memory injected?), the state check covers internal accounting (was the id marked seen?). Confirms that the in-DB status flip is visible to and consumed by 2.4's `get_cancelled_session_ids` lookup within the same inline-await sequence.

This test exercises the full priority-5 cancel → priority-10 deliver chain at the storage layer; together with Test 3 (which proves dispatch ordering at the engine layer), it covers both halves of the freshness-guard-A contract.

**Test 5 — helper completion is silent unless it sends `memory_recall`**: use the §2.6 runtime option and the Phase 3 spawn rule together. Sub-scenario A fires a 6+-word parent turn, captures the inline/background `spawn_agent` arguments, and asserts `notify_parent_on_completion is False` while `parent_session_id == "platform-Y"` is still present. Then complete the child run through the same completion path used by `end_agent_run` without inserting any `memory_recall` message; assert the parent's pending inter-session-message queue has no `message_type="completion_notification"` row and the wake dispatcher is not called for the parent. Sub-scenario B inserts exactly one explicit helper `memory_recall` message before completion, completes the run, fires the next parent `turn_start`, and asserts the rendered output contains only the selected memory content sentinel and no generic completion notification text. This proves the "0–3 selected memories or finish silently" requirement at the source; do not satisfy this test by filtering completion notifications during delivery, because that would leave the durable wake row behind.

Validation criteria: all five tests exist in `tests/workflows/test_memory_recall_helper_ordering.py` as top-level `def test_...` functions (NOT inside any test class), are runnable independently of each other (each owns its own `RuleEngine`, event fixture, and — for Tests 2 sub-scenarios B/C, Test 4, and Test 5 — its own fresh `SessionVariableManager` / hub DB state), and pass when run against the four Phase 3 rule YAMLs synced from the bundled templates. Test 1's sensitivity-flip assertion uses pytest's `monkeypatch` fixture to substitute the rule YAML template strings in the loaded `RuleEngine` instance and confirms the resolution path is actually exercised. Test 2's freshness-filter assertions use a real `SessionVariableManager` keyed off `platform_session_id="platform-Y"` (the same id used in `event.metadata['_platform_session_id']`), NOT off the external session_id — this transitively asserts §2.4's session-key correctness across the integration. Test 2 explicitly resets `parent_turn_seq` and `injected_memory_ids` between sub-scenarios A, B, and C; the test body MUST contain explicit reset code (`sv_mgr.set_variable(...)` or fixture-level setup) between the sub-scenarios so a future maintainer cannot accidentally let state bleed across (round-5 F3 guard). Test 4 uses a real test hub DB (pytest fixture `db`) and pre-inserts the `agent_runs` row directly via the storage manager API and the `inter_session_messages` row via `InterSessionMessageManager(db).create_message(from_session=..., to_session=..., content=...)` (the column is `to_session`, NOT `target_session_id` — the MCP tool parameter name differs from the storage column), to keep the test setup self-contained. Test 4's `RuleEngine` MUST be constructed with an inline `mcp_dispatcher` that routes `cancel_stale_helpers` and `deliver_pending_messages` through the real registered tools against the test DB; without it, `inject_result: true` effects are deferred and the cancellation/delivery chain never executes inline. Test 5 must assert both absence of durable `completion_notification` rows/wake calls and absence of rendered completion text; rendered-output-only assertions are insufficient.

**Acceptance:**

- 3.5.1 — Three-rule session-id sensitivity integration test asserts cancel + deliver + spawn rules all resolve their session-id-referencing arguments to `_platform_session_id`, with a monkey-patched sensitivity flip that proves the assertion is exercised. test: `tests/workflows/test_memory_recall_helper_ordering.py::test_three_rule_session_id_sensitivity_integration`.
- 3.5.2 — Four-rule turn-sequence + freshness integration test partitions into three independent sub-scenarios with explicit `parent_turn_seq` and `injected_memory_ids` resets between them: (A) baseline counter advance 0→1→2 and spawn-prompt `origin_turn_seq: 2`; (B) reset to pre-turn-2 (`parent_turn_seq=1`, empty `injected_memory_ids`) then fire one turn with a `memory_recall` payload at `origin_turn_seq=1` carrying memory `{"id":"mem-fresh","content":"content-sentinel-mem-fresh"}`, assert rendered output contains the content sentinel AND `injected_memory_ids` mutates to include the id `"mem-fresh"`; (C) same reset, fire one turn with `origin_turn_seq=0` and memory `{"id":"mem-stale","content":"content-sentinel-mem-stale"}`, assert rendered output does NOT contain the content sentinel, `injected_memory_ids` unchanged, and "Dropping stale memory_recall" debug log emitted. The two assertion layers (rendered content via sentinel, internal state via id) reflect that `src/gobby/hooks/dispatchers/mcp.py::format_discovery_result` renders `m.get("content")` not `m.get("id")`, so id-based rendered-output assertions are unreliable (round-6 F2). test: `tests/workflows/test_memory_recall_helper_ordering.py::test_four_rule_turn_seq_and_freshness_integration`.
- 3.5.3 — Cancel-vs-deliver inline ordering test asserts `cancel_stale_helpers` dispatches strictly before `deliver_pending_messages` (by `time.monotonic_ns()` timestamp) and that BOTH dispatches run inline (neither appears in `response.metadata["mcp_calls"]`). Relocated from the original §3.2.4 per round-5 F2 because the test requires §3.1's `inject_result: true` change on `deliver-pending-messages`, which §3.2 did not encode as a dep. test: `tests/workflows/test_memory_recall_helper_ordering.py::test_cancel_dispatches_before_deliver_inline`.
- 3.5.4 — Real-DB cancellation-before-formatter test pre-inserts a `running` `memory-recall-helper` `agent_runs` row plus a queued `memory_recall` `inter_session_messages` row carrying memory `{"id":"mem-test4","content":"content-sentinel-test4"}`, fires `turn_start`, and asserts (a) the stale run's `agent_runs.status == 'cancelled'` at the moment `_format_delivery_result` first runs, (b) rendered output does NOT contain `"content-sentinel-test4"` (UX: payload dropped), and (c) `injected_memory_ids` does NOT contain `"mem-test4"` (state: id not marked seen). Both rendered and state assertions are required per the round-6 F2 content-sentinel convention. test: `tests/workflows/test_memory_recall_helper_ordering.py::test_cancel_status_transitions_before_delivery_formatter_runs`.
- 3.5.5 — Silent-completion integration test proves `spawn-memory-recall-helper` passes `notify_parent_on_completion: false`, a helper that calls `end_agent_run` without `memory_recall` leaves no parent `completion_notification` pending message/wake, and a helper that sends `memory_recall` delivers only the selected memory content sentinel. test: `tests/workflows/test_memory_recall_helper_ordering.py::test_memory_helper_completion_is_silent_without_memory_recall`.

## V1 Plan Changelog

`kind: verification`

**Round 1** (reviewer_run: run-5231d2f026de — workflow-blocked on `send_message` per #15100; findings re-derived in-session by the parent coordinator. reviewer_session: 01cce3e3-9a81-47af-a4b8-af3f2b563b99. verdict: needs_review → resolved in this pass.)

- F1 (blocking, traceability): stale `hook_manager.py` line refs in Constraints. Actual HEAD: `_dedup_memory_results` at line 490; `_platform_session_id` reads at 312, 400, 490. **Resolved**: Constraints updated to cite HEAD line numbers.
- F2 (blocking, unhandled-edge): freshness contract did not document fail-open posture when `cancel_stale_helpers` returns `success: False`. Guard B (origin_turn_seq) is the correctness backstop in that case. **Resolved**: Constraints freshness section adds an explicit "Fail-open posture on cancel error" paragraph.
- F3 (blocking, weak-testability): 2.4.3 lacked a concrete behavioral test for the `_apply_effect` formatter dispatch switch including the cancel_stale_helpers no-op case. **Resolved**: 2.4.3 now names `test_apply_effect_dispatch_switch_cancel_stale_helpers_no_op` in `tests/workflows/test_delivery_pipeline.py`.
- F4 (nit, traceability): 2.1 caller-audit guard was a forward-compat invariant — HEAD grep returned zero positional `send_message(from_session` callers in `src/`. **Resolved**: 2.1 body now records the audit-baseline grep result so the implementer doesn't chase ghosts.
- F5 (nit, gobby-format): 1.4 never cited the proof point that `Session.to_dict()` exposes `digest_markdown` (verified at `storage/session_models.py:247`). **Resolved**: 1.4 validation criteria cite the verification chain.

Spawn-surface bug (#15100): `plan-adversary-taskless`'s `review` step whitelists only `gobby-agents:end_agent_run` and `gobby-skills:get_skill`, blocking `gobby-agents:send_message`. The adversary ran 57 turns / 90 tool calls and completed `success`, but its structured findings never reached the parent. Tracked separately so the next round can use the spawned adversary surface once #15100 lands.

**Round 2** (reviewer_run: in-session — adversary spawn still blocked by #15100. verdict: needs_review → resolved in this pass.)

- F6 (blocking, unhandled-edge): rules 3.2 and 3.3 lacked the `_platform_session_id` presence guard 3.1 carries. Early-lifecycle race could fire the rules with an empty `parent_session_id`, tripping `cancel_stale_helpers` / `spawn_agent` input validation and aborting sibling effects of those rules. **Resolved**: both rules' `when:` clauses now require `event.metadata.get('_platform_session_id')`.
- F7 (blocking, unhandled-edge): 3.3's spawn prompt template `{{ variables.parent_turn_seq | int }}` defaulted to 0 when the seed was missing, which could match 2.4's expected `current - 1 = 0` on the first turn after a missed-seed activation and FALSE-ACCEPT a stale payload. **Resolved**: spawn rule's `when:` now gates on `variables.get('parent_turn_seq') is not none` and the template uses `(variables.parent_turn_seq | default(0)) | int` belt-and-suspenders.
- F8 (nit, unhandled-edge): helper picks memories at prompt N, delivers at N+1; topical prompt switches make selections irrelevant. **Resolved**: documented in 3.3's clause-by-clause section as an accepted design tradeoff (the alternative — block turn_start on helper completion — is ruled out by the "Helper must run backgrounded" constraint).
- F9 (blocking, weak-testability): 1.3.6 (`internal_keys` includes `memory_recall_helper_enabled`) was named as a `file:` artifact only. **Resolved**: 1.3.6 now names `test_internal_keys_excludes_memory_recall_helper_enabled_from_variables_count` in `tests/hooks/event_handlers/test_session_variable_preservation.py`.

**Round 3** (reviewer_run: in-session — #15100 still open. verdict: needs_review → resolved in this pass.)

- F10 (nit, traceability): Phase 3's prose understated rule co-tenancy at the priority slots (`reset-subagent-flag` and `prepare-clear-handoff` at 5, `memory-recall-on-prompt` and `handle-plan-mode-entry` at 10). **Resolved**: Phase 3 framing now lists co-tenants and confirms none interfere with the helper contract.
- F11 (blocking, weak-testability): 1.4's helper instructions never told the helper to call `end_agent_run`, leaving it to burn the full `max_turns: 3` budget per spawn. **Resolved**: helper instructions now have an explicit step 8 (`end_agent_run`) plus a hard-constraint reminder.

**Round 4** (verdict: rejected → resolved in this pass.)

- F12 (blocking, bad-sequencing): §3.2.5's regression test was assigned to the cancel-rule leaf but required §3.3's spawn rule loaded as setup; §3.3 depends on §3.2, creating a prerequisite/downstream cycle at expansion time. **Resolved**: §3.2.5 narrowed to assert ONLY the cancel rule's `parent_session_id` resolution (with a monkey-patched sensitivity flip on the cancel rule alone). The full three-rule sensitivity sweep (cancel + deliver + spawn co-loaded) moved to the new §3.5 integration deliverable, which depends on §3.1, §3.2, §3.3, §3.4.
- F13 (blocking, bad-sequencing): §3.4.4 behavioral test required all four rules and asserted §3.3's spawn-prompt resolved value; §3.3 depends on §3.4, creating the same cycle. **Resolved**: §3.4.4 narrowed to assert ONLY the counter rule's increment-on-turn-start and not-incrementing-for-spawned-agent behavior in isolation. The four-rule turn-seq + spawn-prompt + memory_recall freshness end-to-end test moved to §3.5's `test_four_rule_turn_seq_and_freshness_integration`.
- F14 (blocking, weak-testability): §3.1 manual end-to-end documented `send_message(to_session=#<self>, ...)`, but `send_message` has no `to_session` parameter — the post-2.1 signature requires `target="session"` + `target_id=<session_id>`. **Resolved**: §3.1's step 2 now uses the canonical signature `send_message(target="session", target_id=#<self>, ...)` with an explicit "there is no `to_session` parameter" caveat inline so future readers don't reintroduce the typo.
- Whole-plan sweep (per the plan-draft "Whole-Plan Sweep After Findings" rule): swept Phase 3 for any other per-section test whose setup requires downstream sections to be co-loaded. None found beyond the two cited. §3.2.4's ordering test co-loads §3.1's deliver rule, which is a sibling (3.1 has no Phase 3 deps), so it does not create a cycle. §3.5 (new) is the canonical owner for any future cross-rule integration test.
- Plan structural change: added §3.5 (`[category: test] (depends: 3.1, 3.2, 3.3, 3.4)`) as the canonical integration deliverable. Phase 3 framing's "Intra-phase dependencies" paragraph updated to list 3.5's deps.

**Round 5** (verdict: rejected → resolved in this pass.)

- F15 / round-5 F1 (blocking, bad-sequencing): §1.4.5 owned `tests/workflows/test_step_enforcement.py::test_blocked_tools_overrides_infra_exempt_for_helper`, but the runtime behavior under test is implemented in §2.2 (`_check_agent_tool_enforcement` reorder). Phase 2 carries no encoded cross-phase deps on Phase 1, so §1.4 could be expanded and validated before §2.2 landed, making the test fail for a prerequisite §1.4 does not own. **Resolved**: removed §1.4.5 from acceptance, removed `tests/workflows/test_step_enforcement.py` from §1.4's Targets, rewrote §1.4's runtime-blocking sentence to point at §2.2 instead, and added §2.2.4 as the helper-equivalent integration test owned by §2.2 (which is the deliverable that actually implements the runtime behavior). §2.2's narrative documents the operational prereq on §1.4 inline so an expansion worker knows to sequence §1.4 first.
- F16 / round-5 F2 (blocking, bad-sequencing): §3.2.4's ordering regression test required `deliver-pending-messages` loaded with `inject_result: true`, but `inject_result: true` is §3.1's contribution and §3.2 had no encoded dep on §3.1. Independent expansion of §3.2 could run before §3.1 (or in parallel), making the test fail for a missing prerequisite. Separately, §3.2 and §3.4 both target `tests/workflows/test_memory_lifecycle_rules.py` (extending the `MEMORY_RULES` set literal) and `tests/workflows/test_memory_recall_helper_ordering.py` (the new ordering test module), with no encoded dep between them, creating a file-ownership race at merge time. **Resolved**: moved the cancel-vs-deliver ordering test from §3.2.4 to §3.5 as Test 3 / §3.5.3 (and a new Test 4 / §3.5.4 covers the real-DB cancellation-before-formatter assertion that the original §3.2.4 narrative also described but never broke out as a named acceptance item). Renumbered the remaining §3.2.5 to §3.2.4. Added `(depends: 3.2)` to §3.4's section header to serialize the shared test-file ownership. Updated Phase 3 framing's "Intra-phase dependencies" paragraph to document the new §3.4 → §3.2 edge and the §3.5 ownership of the relocated ordering tests.
- F17 / round-5 F3 (blocking, weak-testability): §3.5 Test 2's narrative told the implementer to "fire two turn_start events" to advance `parent_turn_seq` to 2, then "re-fire turn 2" with `memory_recall` payloads whose `origin_turn_seq` values were chosen against the `parent_turn_seq=2` baseline. But re-firing `turn_start` triggers §3.4's priority-1 increment rule again, advancing `parent_turn_seq` to 3 (and 4) — so the chosen `origin_turn_seq=1` payload was actually stale, not fresh, and the assertion contradicted the stated freshness filter. **Resolved**: partitioned Test 2 into three independent sub-scenarios (A: baseline counter advance + spawn-prompt resolution; B: fresh payload accepted; C: stale payload dropped) with explicit pre-turn-2 state resets (`parent_turn_seq=1`, `injected_memory_ids=[]`, fresh `SessionVariableManager` instance) between each sub-scenario. Acceptance §3.5.2 now spells out the three-sub-scenario structure and the reset requirement; the test body must contain explicit reset code so a future maintainer cannot accidentally let state bleed across.
- Whole-plan sweep (per plan-draft's "Whole-Plan Sweep After Findings" rule): swept all leaves for the three Round 5 finding classes.
  - F1 class (test needs cross-leaf runtime without encoded dep): only §1.4.5 / §2.2.4 fit. §2.2.4 documents its §1.4 prereq operationally because §2.2 is Phase 2 and the Phase 2 entry criteria already establish that cross-phase deps are not DB-enforced. All other test acceptance items name tests whose runtime behavior is owned by their own leaf.
  - F2 class (file/test ownership conflict between parallel leaves without encoded dep): only §3.2 / §3.4 fit (both add to `test_memory_lifecycle_rules.py` and `test_memory_recall_helper_ordering.py`). §3.3 also extends `test_memory_lifecycle_rules.py` but already depends on §3.1, §3.2, §3.4, so it serializes last. §3.5 extends `test_memory_recall_helper_ordering.py` but depends on all of §3.1–§3.4. No other parallel-leaves-share-file pattern exists in the plan.
  - F3 class (state contamination across multi-step test assertions): only §3.5 Test 2 fit. §2.4.12 (freshness guard B) makes multiple assertions in one test function but each calls `_format_delivery_result` independently with the same read-only `variables['parent_turn_seq']` — no state mutation across assertions. §2.4.14 (kill-switch catch-all) describes a production flow inline but the test setup is a single formatter call. §3.5 Test 1 (sensitivity flip) re-runs `RuleEngine.evaluate` with monkey-patched templates, but the counter rule is not loaded so no `set_variable` mutates `variables` across runs; the recording stub returns nothing that would mutate the formatter's `injected_memory_ids` read path. §3.5 Test 4 (real-DB cancellation) is a single `turn_start` fire against pre-inserted DB rows — no within-test multi-firing.

**Round 6** (verdict: rejected → resolved in this pass.)

- F18 / round-6 F1 (blocking, gobby-format): §2.5 inlined a private `_stop_run` helper (~65 lines) and the new `cancel_stale_helpers` tool registration (~60 lines) directly into `src/gobby/mcp_proxy/tools/agents.py`, which is already 956 lines on HEAD. The net +70 lines would push the file past the project's 1,000-line non-test Python monolith limit (guiding principle #2). No open refactor task exists for that file (verified via `gobby-tasks:search_tasks query="refactor mcp_proxy/tools/agents.py monolith"`). **Resolved**: extracted the shared cancellation body into a new public function `stop_agent_run(...)` in the existing `src/gobby/mcp_proxy/tools/agent_cancellation.py` module (which already holds `terminalize_cancelled_agent_run` / `terminalize_killed_agent_run` / `recover_cancelled_agent_task_claim` and is 131 lines on HEAD). The function takes its dependencies (`runner`, `agent_run_manager`, `db`, `lifecycle_monitor`, `completion_registry`, `task_manager`, `session_manager`, `hook_manager_resolver`, `kill_agent_process`, `cleanup_terminal_artifacts`) as explicit keyword arguments — both `stop_agent` and the new `cancel_stale_helpers` delegate to it from inside `create_agents_registry`. Post-edit footprints: `agents.py` ≈ 960 lines (net +5: delete the existing `stop_agent` body, add the one-line delegate and the small `cancel_stale_helpers` registration), `agent_cancellation.py` ≈ 200 lines (net +70: add `stop_agent_run`). Both are comfortably below the 1,000-line limit. Added §2.5.7 (direct unit test of `stop_agent_run`) and §2.5.8 (forward-compat guard test that `agents.py` line count is strictly less than 1,000) as new acceptance items. Updated §2.5 Targets to list `agent_cancellation.py` as a primary edit (was "consumed unchanged").
- F19 / round-6 F2 (blocking, weak-testability): §2.4 and §3.5 test acceptance items asserted that injected `context_parts` contains memory IDs such as `m1`, `mem-fresh`, and `mem-stale`. The existing `search_memories` formatter at `src/gobby/hooks/dispatchers/mcp.py:148-167` renders ONLY `m.get("content")` plus optional `similarity`/`search_via` metadata — it never renders `m.get("id")`. So a correct implementation that preserves the existing formatter could fail the "contains id" assertions for accepted memories, and the stale-drop "does NOT contain id" assertions could pass even when stale memory CONTENT was injected. **Resolved**: introduced a load-bearing "Test fixture convention" paragraph at the top of §2.4's validation criteria establishing that every memory record carries a stable `id` AND a unique `content` sentinel; rendered-context assertions use the content sentinel (which the formatter renders); state assertions on `injected_memory_ids` use the id (which the set variable stores). Rewrote §2.4 validation cases (2), (4), (7), (7-aux), (7b), (7c), (7d), (7e), (9), (10), and the end-to-end paragraph to use content sentinels for rendered-context assertions and ids for state assertions. Rewrote §3.5 Sub-scenarios B and C to use sentinels `"content-sentinel-mem-fresh"` / `"content-sentinel-mem-stale"` and explicitly call out that absence-of-id checks are unreliable. Rewrote §3.5 Test 4 to use sentinel `"content-sentinel-test4"` and require both a rendered-context check (sentinel absent) and a state check (id absent from `injected_memory_ids`). Updated acceptance items §3.5.2 and §3.5.4 to reference the content-sentinel convention. Rewrote §3.1's manual end-to-end to use sentinel `"manual-e2e-content-sentinel-fresh"` and the negative cases to use `"manual-e2e-content-sentinel-no-origin"` and `"manual-e2e-plain-cancelled-sentinel"`. The convention paragraph documents the exit hatch: if a future test really needs ID-presence in rendered output, it must first add an explicit formatter change with its own UX acceptance item.
- Whole-plan sweep (per plan-draft's "Whole-Plan Sweep After Findings" rule): swept all leaves for the two Round 6 finding classes.
  - F1 class (monolith risk after edits push a target file past 1,000 lines): only §2.5 / `agents.py` fit. Verified all other Target: files: `src/gobby/config/sessions.py` (233 lines, +12), `src/gobby/config/app.py` (891 lines, +5), `src/gobby/hooks/event_handlers/_base.py` (62 lines, +2), `src/gobby/hooks/event_handlers/__init__.py` (179 lines, +5), `src/gobby/hooks/factory.py` (545 lines, +2), `src/gobby/hooks/event_handlers/_session_start/agents.py` (199 lines, +20), `src/gobby/mcp_proxy/tools/agent_messaging.py` (300 lines, +15), `src/gobby/workflows/engine/enforcement.py` (965 lines, net ~0 from §2.2's single-block reorder), `src/gobby/storage/agents/_queries.py` (224 lines, +40), `src/gobby/workflows/engine/effects.py` (380 lines, +200 from §2.4's two new methods → ~580 lines), `src/gobby/hooks/dispatchers/mcp.py` (417 lines, only imported in §2.4 with no edit required because the existing search_memories formatter at lines 148-167 already handles `{"memories": [...]}`). `enforcement.py` is the closest to the limit at 965 lines but §2.2's edit is a single-block reorder (move existing 6-line block above existing 4-line block) with no net line growth. None of the other targets approach the limit.
  - F2 class (id-based assertions on rendered output where the formatter renders content not id): only §2.4 and §3.5 fit. Grepped the entire plan for assertions of memory id literals appearing in rendered output — all instances were inside §2.4's validation criteria paragraph and §3.5's sub-scenarios / Test 4. Every match was converted to the content-sentinel convention. §3.1's manual e2e (sentinel `"manual-e2e-content-sentinel-fresh"`) was the only other id-in-rendered-output language in the plan. Acceptance items §2.4.6-§2.4.10 ("formats and atomic-appends the id", "already-injected ids are filtered", etc.) use "id" in a state-management sense (the variable stores ids), not a rendered-output sense — they are correct as-is. §2.4.15 ("Fast-recall formatter dedupes and atomic-appends ids") same rationale. No other content-vs-id confusion exists in the plan.

**Round 7** (verdict: rejected → resolved in this pass.)

- F20 / round-7 F1 (blocking, missing-requirement): §1.4's helper instructions say "select 0–3 memories" but that is soft LLM guidance only — §2.4's delivery pipeline accepted every memory record surviving freshness and dedup without a runtime cap. A buggy or over-eager helper could send 4+ memories and all would inject, violating the 0–3 contract at the trust boundary. **Resolved**: added a `MAX_HELPER_MEMORIES = 3` hard cap in `_format_delivery_result` at step 6b — after freshness filtering and dedup (steps 2–6), before formatting and `injected_memory_ids` append (steps 7–8). The cap applies across all `memory_recall` messages in one delivery (not per-message). Excess memories are silently dropped with a debug log. Added validation case (11) + (11b) and acceptance item §2.4.16 covering the trust-boundary guard.
- F21 / round-7 F2 (blocking, bad-sequencing): §2.2.4's helper-equivalent integration test resolved the real §1.4 helper YAML via `resolve_agent("memory-recall-helper", db)`, but Phase 2 framing explicitly permits parallel work with Phase 1's later sections. If §2.2 expanded before §1.4 merged and synced, the test would fail for an artifact it does not own. The inline "operational prereq" note is not enough when the phase entry criteria explicitly permits parallel work and the DAG does not enforce the cross-phase edge. **Resolved**: replaced the `resolve_agent` approach with a local test-file constant `HELPER_BLOCKED_TOOLS` that mirrors §1.4's `blocked_tools` list. The test now exercises the enforcement engine behavior (the reordered `_check_agent_tool_enforcement`) independent of the YAML file's existence. §1.4's own sync test (§1.4.3) catches any drift between the real YAML and this fixture. Updated §2.2.4 acceptance item description.
- F22 / round-7 F3 (blocking, unhandled-edge): §2.3's `get_cancelled_session_ids` SQL filtered recency on `created_at`, but cancellation writes the terminal timestamp to `completed_at`. An old pending/running helper cancelled on the current turn would have `created_at` outside the recency window even though `completed_at` is NOW — the row would be omitted from the cancelled-session set, and guard A would fail to drop its queued `memory_recall` payload. **Resolved**: changed the `newer_than_now_expr` column from `"created_at"` to `"completed_at"` in the SQL. Added a code comment explaining why `completed_at` is correct (terminalize writes it on cancellation, so it's always populated for cancelled rows by the time the query runs). Added validation case "Recency-by-completed_at test" and acceptance item §2.3.4 covering the edge case (old `created_at` + recent `completed_at` included; recent `created_at` + old `completed_at` excluded). Renumbered the existing §2.3.4 (dialect parity) to §2.3.5.
- Whole-plan sweep (per plan-draft's "Whole-Plan Sweep After Findings" rule): swept all leaves for the three Round 7 finding classes.
  - F1 class (missing runtime cap / trust-boundary guard): only §2.4 fits. The fast-recall path (`_format_search_memories_result`) does not need a cap because the `search_memories` tool itself returns at most `limit` results (8 per the helper's instructions) and the fast-recall rule's own search is bounded. The delivery path is the only one accepting memory counts from an LLM-driven agent.
  - F2 class (test resolves real YAML from a cross-phase leaf without an encoded dep): only §2.2.4 fit. All other Phase 2 tests reference Phase 2's own artifacts or use inline test constants. §3.5's integration tests require all four Phase 3 rules loaded, but §3.5 explicitly depends on §3.1–§3.4 (same phase, encoded in DAG). No other cross-phase-YAML dependency exists.
  - F3 class (SQL filters on `created_at` where `completed_at` is the correct temporal anchor): only §2.3 fits. Grepped the plan for all SQL expressions using `newer_than_now_expr` or `older_than_now_expr` — the only instance is §2.3's `get_cancelled_session_ids`. The `_AgentRunCleanupMixin.cleanup_stale_pending_runs` reference uses `older_than_now_expr` on `created_at` which is correct for that query (finding runs that have been pending too long, where creation time is the relevant anchor). No other plan section introduces time-window SQL.

**Round 8** (verdict: rejected → resolved in this pass.)

- F23 / round-8 F1 (blocking, weak-testability): §2.3.5 required a "dialect-parity test" running against both SQLite and PostgreSQL hub backends, but the hub storage layer is Postgres-only (`StorageDialect = Literal["postgres"]` at `src/gobby/storage/sql_dialect.py:10`; `HubDatabase.dialect: Literal["postgres"]` at `src/gobby/storage/hub/protocol.py:206`; the `temp_db` test fixture delegates to `postgres_db`). There is no SQLite hub backend to parameterize against — the test as specified is unimplementable. **Resolved**: revised §2.3.5 to a "Timezone-aware Postgres recency test" that validates `newer_than_now_expr`'s `INTERVAL` arithmetic against timezone-aware `completed_at` values under the production Postgres backend. Corrected §2.3's narrative from claiming the dialect layer "translates to the underlying SQL flavor (Postgres uses `INTERVAL`; SQLite uses `datetime('now', ?)`)" to accurately state the storage layer is Postgres-only. Updated the code comment inside the `get_cancelled_session_ids` implementation to remove the Postgres-vs-SQLite framing.
- F24 / round-8 F2 (blocking, unhandled-edge): §2.4's `_format_delivery_result` failed open when `LocalAgentRunManager.get_cancelled_session_ids` raised — `helper_cancelled_sessions` stayed empty and guard A became a silent no-op. A queued `memory_recall` payload from a just-cancelled helper whose `origin_turn_seq` legitimately matched `parent_turn_seq - 1` would then pass guard B (the payload was fresh at send time; the run was cancelled AFTER the send but BEFORE delivery). Guard B alone is insufficient to catch this scenario because the payload's turn-seq is correct — only the run's cancelled status (which guard A checks) reveals it should be dropped. **Resolved**: changed the exception handler from fail-open to fail-closed for `memory_recall` payloads. A new `cancelled_lookup_failed: bool` flag is set on exception; the per-message loop drops ALL `memory_recall` payloads (with a warning log) when this flag is true, while non-memory_recall messages from the same delivery still flow through to `other_messages` unchanged. Added Constraints section "Fail-closed posture on cancelled-session lookup failure (formatter level)" documenting the distinction between rule-level fail-open (cancel_stale_helpers MCP call failure) and formatter-level fail-closed (get_cancelled_session_ids DB query failure). Added validation case (12) and acceptance item §2.4.17. Updated §2.4.1 description and the implementer-notes "Errors in dedup-state" bullet to document the fail-open vs fail-closed split.
- Whole-plan sweep (per plan-draft's "Whole-Plan Sweep After Findings" rule): swept all leaves for the two Round 8 finding classes.
  - F1 class (test requires a non-existent backend or infrastructure): only §2.3.5 fit. All other test acceptance items target existing infrastructure. §2.4's test fixtures use the `temp_db` fixture (which delegates to `postgres_db`), §3.5's Test 4 uses the real test hub DB — both are Postgres-only by design. No other test in the plan references SQLite or assumes multi-backend parameterization.
  - F2 class (exception handler that fails open where fail-closed is needed to preserve a safety invariant): only §2.4's `get_cancelled_session_ids` lookup handler fit. The other `except Exception` handlers in §2.4 (dedup-state read at line 961, dedup-state write at line 985) and in `_format_search_memories_result` (lines 1047, 1059) correctly fail open because their failure mode is at worst a duplicate injection (annoying, not a safety violation — duplicate content renders twice, but no stale/cancelled payload injects). §2.5's best-effort per-run cancellation loop (line 1318) correctly fails open because other runs still get cancelled and the tool returns `errors[]` for transparency. The distinction: the cancelled-session lookup is the ONLY handler whose failure means the formatter cannot verify a safety-critical property (run cancelled status) that guard B alone cannot recover.

**Round 9** (verdict: rejected → resolved in this pass.)

- F25 / round-9 F1 (blocking, unhandled-edge): §1.3 seeded `memory_recall_helper_enabled` and `parent_turn_seq` inside the `changes` dict path of `activate_default_agent`, which runs AFTER agent resolution. Sessions where activation is skipped (`default_agent_name == "none"`, agent not found in DB) never reach that path, so the variables are never seeded. But §3.4's counter rule (priority 1) used `variables.get('parent_turn_seq', 0)` with a `default(0)` fallback, self-creating `parent_turn_seq=1` on the first `turn_start` — defeating §3.3's `parent_turn_seq is not none` spawn guard. Combined with §3.3's `memory_recall_helper_enabled` defaulting to True, the helper could spawn in sessions where the runtime config was never applied. **Resolved** (two-part fix): (1) extracted seeding into a new `_seed_memory_recall_helper_vars(handler, session_id)` function called at the top of `activate_default_agent` — AFTER the `session_manager is None` check (cannot seed without a session manager) and BEFORE `_resolve_agent_name` (so seeding runs before exit (2) `default_agent_name == "none"` and exit (3) `not agent_body`). The helper uses `SessionVariableManager` directly: always re-applies `memory_recall_helper_enabled`, seeds `parent_turn_seq=0` only when absent. The `changes` dict no longer carries these two variables. (2) Added `variables.get('parent_turn_seq') is not none` to §3.4's `when:` clause and removed the `default(0)` fallback from the value template (now `{{ (variables.parent_turn_seq | int) + 1 }}`). This makes the counter rule fail closed when the seed is missing — it neither creates nor increments the variable, so §3.3's existing `parent_turn_seq is not none` guard remains effective. Added §1.3.9 (skipped-activation test) and §3.4.4(d) (unseeded-session regression guard).
- F26 / round-9 F2 (blocking, weak-testability): §2.3's verbatim `get_cancelled_session_ids` implementation used `with self.db.connection() as conn: rows = conn.execute(sql, params).fetchall()`, but `HubDatabase` (the protocol `_AgentRunQueryMixin.db` conforms to) does not expose a `connection()` method — it exposes `fetchall(sql, params)` directly. The existing `_queries.py` mixin methods use `self.db.fetchall(...)`. Following the plan as written would raise an `AttributeError` under the real Postgres hub backend before any §2.3 acceptance tests could pass. The snippet also used `list[Any]` without adding `Any` to `_queries.py`'s imports (which only has `from typing import Protocol`). **Resolved**: rewrote the implementation to use `rows = self.db.fetchall(sql, params)` (matching the existing mixin pattern) and changed the type annotation from `list[Any]` to `list[int | str]` (since params are `since_hours: int` and optionally `agent_name: str`), avoiding the need for an `Any` import.
- Whole-plan sweep (per plan-draft's "Whole-Plan Sweep After Findings" rule): swept all leaves for the two Round 9 finding classes.
  - F1 class (variables seeded only on the happy-path agent-activation flow, missed by early exits): only §1.3's `activate_default_agent` seeding was affected. No other plan section seeds session variables through a code path with early exits. §3.4 was the only rule using `default(0)` in a `set_variable` value template — its value expression now requires the variable to be pre-seeded (guarded by the `when:` clause). §3.3's spawn rule already had its own `parent_turn_seq is not none` guard and uses `default(0)` only as belt-and-suspenders in the prompt template (dead code behind the guard, per round-7 F7). **Note (corrected in round 12):** this sweep addressed within-function early exits in `activate_default_agent` but did not cover the flow-level `skip_default_agent_activation` guard in `flow.py:208` that bypasses the entire function — see round-12 F31 for the flow-level fix.
  - F2 class (code snippet uses a storage API method that does not exist on the `HubDatabase` protocol): only §2.3's `get_cancelled_session_ids` fit. Grepped the plan for `self.db.connection()` and `conn.execute` — no remaining instances. All other code snippets in the plan use the correct `self.db.fetchall(...)`, `self.db.execute(...)`, or `self.db.fetchone(...)` patterns. The existing `_queries.py` methods at HEAD all use `self.db.fetchall(...)` or `self.db.fetchone(...)`, confirming the fix matches the codebase convention.

**Round 10** (verdict: rejected → resolved in this pass.)

- F27 / round-10 F1 (blocking, unhandled-edge): `cancel_stale_helpers` is best-effort (`success: True` even with per-run `errors[]`), so a failed cancellation leaves a stale helper running. On the immediate next turn, a helper spawned at turn N has `origin_turn_seq=N` and `current_parent_turn_seq - 1 = N` (after the priority-1 increment), so guard B accepts its payload. The run was never cancelled, so guard A also accepts it. Neither guard catches a still-running stale helper on the immediate next turn. **Resolved**: added guard C (cancel-incomplete) to `_format_delivery_result`. After the cancelled-session lookup, the formatter queries `LocalAgentRunManager.list_by_parent(platform_session_id)` for any `pending`/`running` `memory-recall-helper` runs. If any remain, the `cancel_incomplete` flag is set and all `memory_recall` payloads are dropped (fail-closed, same posture as `cancelled_lookup_failed`). Non-memory_recall messages are unaffected. Updated Constraints freshness paragraph from "two guards" to "three guards" (A, B, C), updated fail-open paragraph to reference guard C as the single-turn backstop, updated §2.4 docstring steps, code block, per-message loop, validation cases (13)+(13b), acceptance item §2.4.18, implementer notes, §2.4.1 description, and Phase 3 framing freshness-contract sentence.
- F28 / round-10 F2 (blocking, weak-testability): §3.5 Test 4 pre-inserted an `inter_session_messages` row using `target_session_id='platform-Y'`, but the storage model and `InterSessionMessageManager.create_message` use `to_session` (the table column), not `target_session_id` (the MCP tool parameter name). Test 4 also constructed a `RuleEngine` without specifying inline `mcp_dispatcher` wiring; without an inline dispatcher, `inject_result: true` effects are deferred into `response.metadata["mcp_calls"]` and the cancel/delivery chain never executes inline — assertions would pass trivially. **Resolved**: rewrote the pre-insert to use `InterSessionMessageManager(db).create_message(from_session='child-stale', to_session='platform-Y', content=...)` with an explicit note that the column is `to_session` not `target_session_id`. Added inline dispatcher wiring specification: the `RuleEngine` MUST route `cancel_stale_helpers` through the real registered tool (for the full lifecycle chain) and `deliver_pending_messages` through the real delivery tool (so the formatter runs inline). Updated the validation criteria to require the inline dispatcher and reference the correct storage API.
- Whole-plan sweep (per plan-draft's "Whole-Plan Sweep After Findings" rule): swept all leaves for the two Round 10 finding classes.
  - F1 class (guard B alone insufficient for immediate-next-turn cancel failure): only `_format_delivery_result` in §2.4 is the delivery formatter. No other section performs the cancelled-session check or claims guard B catches cancel-failure payloads. The existing fast-recall formatter (`_format_search_memories_result`) does not consume `memory_recall` payloads and has no stale-helper concern. The Phase 3 framing freshness-contract sentence at line 1636 was the only other location referencing the two-guard model — updated to three guards. The Constraints fail-closed paragraph (line 20) referenced guard B as the reason rule-level fail-open is safe — updated to reference guard C.
  - F2 class (storage column name mismatch in test setup): only §3.5 Test 4. No other test in the plan pre-inserts `inter_session_messages` rows directly. §2.4 validation cases use stubbed `_format_delivery_result` input (already-parsed `result` dicts), not raw DB inserts. §3.5 Tests 1–3 use stubbed `_mcp_dispatcher` returns. Test 4 is the only real-DB test that inserts into `inter_session_messages`. F2 sub-class (missing inline dispatcher wiring): only §3.5 Test 4 constructs a `RuleEngine` against a real DB with `inject_result: true` effects. Tests 1–3 use stubbed dispatchers and explicitly verify which calls appear inline vs deferred. Test 4 is the only test that needs real inline execution.

**Round 11** (verdict: rejected → resolved in this pass.)

- F29 / round-11 F1 (blocking, weak-testability): §3.5 Test 2 `_mcp_dispatcher` stubs returned raw tool payloads (e.g. `{"success": True, "messages": [], "count": 0}`) instead of the production dispatcher envelope (`{"success": True, "inject_result": True, "result": <payload>}`). The production inline dispatcher at `src/gobby/hooks/factory.py:382-386` wraps every result in this envelope, and `_apply_effect` at `src/gobby/workflows/engine/effects.py:117` gates the `format_discovery_result` formatter path on `dr.get("result")` — without the wrapper key, the formatter branch is skipped entirely. Sub-scenario B's positive assertions (content sentinel in rendered output) would fail because the formatter never runs; sub-scenario C's negative assertions (sentinel absent) would pass trivially because nothing is rendered. **Resolved**: rewrote all `_mcp_dispatcher` stub return values in §3.5 Tests 2, 3, and 4 plus §3.2.4 and §3.5 Test 1 to return the production envelope. Test 2 default stubs, sub-scenario A deliver stub, sub-scenario B deliver stub, and sub-scenario C deliver stub all wrapped. Test 3 recording stub adds explicit envelope return. Test 4 inline dispatcher adds explicit envelope-wrapping requirement for real-tool results. §3.2.4 recording stub adds explicit envelope return. §3.5 Test 1 recording stub adds per-tool envelope returns.
- F30 / round-11 F2 (blocking, gobby-format): §2.5 monolith limit verification said implementers MUST escalate if either file "approaches the limit (>950 lines)," but the plan's own expected post-edit footprint for `agents.py` is ~960 lines — crossing the stated escalation threshold while remaining well within the actual 1,000-line hard gate. A compliant implementer would be forced to escalate a plan-conforming implementation. **Resolved**: removed the >950 mandatory escalation threshold. The instruction now states the hard gate is 1,000 lines, requires extraction or follow-up if the edits would reach that limit, and explicitly notes the ~960-line expected outcome is within the limit with ~40-line advisory headroom.
- Whole-plan sweep (per plan-draft's "Whole-Plan Sweep After Findings" rule): swept all leaves for both Round 11 finding classes.
  - F1 class (dispatcher stub returns raw payload or no payload without production envelope): §3.5 Tests 2, 3, 4 were the originally cited instances. Swept the entire plan for other `_mcp_dispatcher` stub specifications: §3.2.4 (cancel-rule session-id test) and §3.5 Test 1 (three-rule sensitivity) both had recording stubs with no return value specified — fixed to return the production envelope. §2.4 validation criteria describe stubbed `_format_delivery_result` inputs (already-parsed `result` dicts passed directly to the formatter function, not through `_apply_effect`), so the envelope contract does not apply. No other dispatcher stubs exist in the plan.
  - F2 class (self-contradictory monolith escalation threshold): only §2.5's monolith limit verification paragraph (line 1382) contained the >950 threshold. §2.5.8 (line-count guard test) correctly asserts "strictly less than 1,000" with no reference to 950. §1.1 (dropped), §2.5 rationale paragraph (line 1157), and round-6 changelog entry all reference the 1,000-line limit consistently. No other contradictory threshold exists.

**Round 12** (verdict: rejected → resolved in this pass.)

- F31 / round-12 F1 (blocking, unhandled-edge): §1.3's `_seed_memory_recall_helper_vars` was called from inside `activate_default_agent`, but `flow.py:208` (`handle_session_start`) guards the entire `_activate_default_agent` call with `not input_data.get("skip_default_agent_activation")`. Web chat sets this flag for persona-selected sessions (`src/gobby/servers/websocket/chat/_session.py:812`), so those sessions bypass `activate_default_agent` entirely and never get seeded. The Round 9 fix addressed within-function early exits (default_agent == "none", agent not found) but missed this flow-level bypass. **Resolved**: moved the seeding call from inside `activate_default_agent` to the flow level in `flow.py`. `_seed_memory_recall_helper_vars` is still defined in `agents.py` but called from `flow.py` in both `handle_session_start` (before the `skip_default_agent_activation` guard at line 208) and `handle_pre_created_session` (before the `_activate_default_agent` call at line 399). `activate_default_agent` no longer calls the seeding function — the flow owns seeding exclusively. Added §1.3.10 (pre-created session seeding test). Updated §1.3.4, §1.3.9, Targets, and validation criteria. Corrected the Round 9 sweep language to note the flow-level gap.
- Whole-plan sweep (per plan-draft's "Whole-Plan Sweep After Findings" rule): swept all leaves for the Round 12 finding class.
  - F1 class (seeding located inside a function that can be bypassed by a flow-level guard): only §1.3's seeding of `memory_recall_helper_enabled` and `parent_turn_seq` was affected. No other plan section introduces session variable seeding through `activate_default_agent` or any function gated by `skip_default_agent_activation`. `flow.py` has two session-start entry points (`handle_session_start` and `handle_pre_created_session`) — both now call `_seed_memory_recall_helper_vars` at the flow level. `handle_pre_created_session` (line 399) does not have a `skip_default_agent_activation` guard (it always calls `_activate_default_agent`), but moving seeding to the flow level there is necessary for consistency since `activate_default_agent` no longer calls the seeder. No other plan section references `skip_default_agent_activation` or depends on seeding happening inside `activate_default_agent`.

**Round 13** (verdict: rejected → resolved in this pass.)

- F32 / round-13 F1 (blocking, missing-requirement): the parent task requires the helper to either send 0–3 selected memories or finish silently. The plan instructed the helper to always call `end_agent_run`, while `spawn_agent` auto-subscribes parent sessions to child completion when `parent_session_id` is set. That would create durable `completion_notification` inter-session messages/wakes for no-memory helpers and extra completion noise for memory-sending helpers. **Resolved**: added §2.6, a runtime `spawn_agent.notify_parent_on_completion: bool = True` option that preserves `parent_session_id` lineage/cancellation while skipping `subscribe_agent_completion` when false. Updated §3.3's spawn rule to set `notify_parent_on_completion: false`, updated helper instructions so `end_agent_run` remains required but silent, and updated §3.5 with a silent-completion integration test. Filtering `completion_notification` during delivery was explicitly rejected as too late because the durable wake row would already exist.
- Whole-plan sweep (per plan-draft's "Whole-Plan Sweep After Findings" rule): swept all completion-notification paths. Only `spawn_agent` subscription plus the helper's required `end_agent_run` produced the missing-requirement violation. §2.4 still forwards non-`memory_recall` P2P messages intentionally for ordinary messaging; it is not the right layer for helper completion silence. §3.1's expanded `deliver-pending-messages` rule remains unchanged because the source-level subscription opt-out prevents helper completion rows from entering the queue. §3.3 is the only rule that spawns `memory-recall-helper`, so it is the only YAML that needs `notify_parent_on_completion: false`.

## Task Mapping

`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add `MemoryRecallHelperConfig` (single field) to `DaemonConfig`
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: gobby.config.sessions.MemoryRecallHelperConfig
  labels:
  - covers:12898:1.2:1.2.1
  - covers:12898:1.2:1.2.2
  - covers:12898:1.2:1.2.3
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Thread `memory_recall_helper` config to `EventHandlers` and seed `memory_recall_helper_enabled`
    on session_start
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: gobby.hooks.event_handlers._base.EventHandlersBase
  labels:
  - covers:12898:1.3:1.3.1
  - covers:12898:1.3:1.3.2
  - covers:12898:1.3:1.3.3
  - covers:12898:1.3:1.3.4
  - covers:12898:1.3:1.3.5
  - covers:12898:1.3:1.3.6
  - covers:12898:1.3:1.3.7
  - covers:12898:1.3:1.3.8
  - covers:12898:1.3:1.3.9
  - covers:12898:1.3:1.3.10
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Create `memory-recall-helper` agent definition
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/install/shared/workflows/agents/memory-recall-helper.yaml
  labels:
  - covers:12898:1.4:1.4.1
  - covers:12898:1.4:1.4.2
  - covers:12898:1.4:1.4.3
  - covers:12898:1.4:1.4.4
  tdd: true
  source_section: '1.4'
  assigned_agent: backend-developer
- title: Default `from_session` on `send_message` from SessionContext when omitted
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: gobby.mcp_proxy.tools.agent_messaging.send_message
  labels:
  - covers:12898:2.1:2.1.1
  - covers:12898:2.1:2.1.2
  - covers:12898:2.1:2.1.3
  - covers:12898:2.1:2.1.4
  - covers:12898:2.1:2.1.5
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Reorder `_check_agent_tool_enforcement` so `blocked_tools` overrides the
    infrastructure-tool exempt
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: gobby.workflows.engine.enforcement.EnforcementMixin._check_agent_tool_enforcement
  labels:
  - covers:12898:2.2:2.2.1
  - covers:12898:2.2:2.2.2
  - covers:12898:2.2:2.2.3
  - covers:12898:2.2:2.2.4
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Add `get_cancelled_session_ids` to `_AgentRunQueryMixin`
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: gobby.storage.agents._queries._AgentRunQueryMixin.get_cancelled_session_ids
  labels:
  - covers:12898:2.3:2.3.1
  - covers:12898:2.3:2.3.2
  - covers:12898:2.3:2.3.3
  - covers:12898:2.3:2.3.4
  - covers:12898:2.3:2.3.5
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Helper-aware delivery + same-turn dedup on the inline `inject_result` path
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.3'
  validation_criteria: gobby.workflows.engine.effects.EffectsMixin._format_delivery_result
  labels:
  - covers:12898:2.4:2.4.1
  - covers:12898:2.4:2.4.2
  - covers:12898:2.4:2.4.3
  - covers:12898:2.4:2.4.4
  - covers:12898:2.4:2.4.5
  - covers:12898:2.4:2.4.6
  - covers:12898:2.4:2.4.7
  - covers:12898:2.4:2.4.8
  - covers:12898:2.4:2.4.9
  - covers:12898:2.4:2.4.10
  - covers:12898:2.4:2.4.11
  - covers:12898:2.4:2.4.12
  - covers:12898:2.4:2.4.13
  - covers:12898:2.4:2.4.14
  - covers:12898:2.4:2.4.15
  - covers:12898:2.4:2.4.16
  - covers:12898:2.4:2.4.17
  - covers:12898:2.4:2.4.18
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Add `cancel_stale_helpers` MCP tool sharing `stop_agent`'s lifecycle path
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: gobby.mcp_proxy.tools.agent_cancellation.stop_agent_run
  labels:
  - covers:12898:2.5:2.5.1
  - covers:12898:2.5:2.5.2
  - covers:12898:2.5:2.5.3
  - covers:12898:2.5:2.5.4
  - covers:12898:2.5:2.5.5
  - covers:12898:2.5:2.5.6
  - covers:12898:2.5:2.5.7
  - covers:12898:2.5:2.5.8
  tdd: true
  source_section: '2.5'
  implementation_domain: backend
- title: Add `notify_parent_on_completion` to `spawn_agent`
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: gobby.mcp_proxy.tools.spawn_agent._factory.create_spawn_agent_registry
  labels:
  - covers:12898:2.6:2.6.1
  - covers:12898:2.6:2.6.2
  - covers:12898:2.6:2.6.3
  - covers:12898:2.6:2.6.4
  - covers:12898:2.6:2.6.5
  tdd: true
  source_section: '2.6'
  implementation_domain: backend
- title: Modify `deliver-pending-messages` rule to fire for parent sessions
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/install/shared/workflows/rules/messaging/deliver-pending-messages.yaml
  labels:
  - covers:12898:3.1:3.1.1
  - covers:12898:3.1:3.1.2
  - covers:12898:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  assigned_agent: backend-developer
- title: Create `cancel-stale-memory-recall-helpers` rule (priority 5, before delivery)
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: src/gobby/install/shared/workflows/rules/memory-lifecycle/cancel-stale-memory-recall-helpers.yaml
  labels:
  - covers:12898:3.2:3.2.1
  - covers:12898:3.2:3.2.2
  - covers:12898:3.2:3.2.3
  - covers:12898:3.2:3.2.4
  tdd: true
  source_section: '3.2'
  assigned_agent: backend-developer
- title: Create `spawn-memory-recall-helper` rule
  category: config
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.4'
  validation_criteria: src/gobby/install/shared/workflows/rules/memory-lifecycle/spawn-memory-recall-helper.yaml
  labels:
  - covers:12898:3.3:3.3.1
  - covers:12898:3.3:3.3.2
  - covers:12898:3.3:3.3.3
  - covers:12898:3.3:3.3.4
  tdd: true
  source_section: '3.3'
  assigned_agent: backend-developer
- title: Create `increment-parent-turn-seq` rule (priority 1, before all other turn_start
    rules)
  category: config
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: src/gobby/install/shared/workflows/rules/memory-lifecycle/increment-parent-turn-seq.yaml
  labels:
  - covers:12898:3.4:3.4.1
  - covers:12898:3.4:3.4.2
  - covers:12898:3.4:3.4.3
  - covers:12898:3.4:3.4.4
  tdd: true
  source_section: '3.4'
  assigned_agent: backend-developer
- title: 'Phase 3 integration test: four-rule turn-sequence + session-id sensitivity
    + memory_recall freshness end-to-end'
  category: test
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  validation_criteria: tests/workflows/test_memory_recall_helper_ordering.py::test_three_rule_session_id_sensitivity_integration
  labels:
  - covers:12898:3.5:3.5.1
  - covers:12898:3.5:3.5.2
  - covers:12898:3.5:3.5.3
  - covers:12898:3.5:3.5.4
  - covers:12898:3.5:3.5.5
  tdd: false
  source_section: '3.5'
  assigned_agent: backend-developer
```
