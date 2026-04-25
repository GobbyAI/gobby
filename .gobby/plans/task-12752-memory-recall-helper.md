# Smarter memory recall via backgrounded Haiku helper agent

## Overview

Replace score-only synchronous memory recall with an LLM-judgment-driven backgrounded Haiku helper agent. The helper runs in parallel with the existing fast vector recall on every `turn_start`, takes a holistic view of the parent's session digest + prompt, runs iterative `search_memories` calls, and either `send_message`s 0–3 selected memories back to the parent or finishes silently. Existing fast vector recall stays in place as the immediate baseline; the helper supplements it with smarter selections delivered at the parent's next `turn_start`.

## Constraints

- The synchronous fast-recall path (`memory-recall-on-prompt`) and the rolling digest pipeline (`digest-on-response`) are out of scope — they continue unchanged. The helper consumes the digest produced at `turn_end` of the previous turn; it never produces it.
- Helper must run backgrounded. Adding LLM latency to `turn_start` is unacceptable.
- `PreToolUse` (`before_tool`) does not fire on text-only assistant turns in Claude Code, so delivery happens at the next `turn_start` only — never at `before_tool`.
- **Dedup tracking lives on the delivery side, not the helper side.** The helper *reads* `injected_memory_ids` on the parent before selecting (to avoid re-surfacing already-seen memories) but does NOT write to it. Writing happens when the parent actually delivers and injects the helper's payload. This eliminates two bugs from the earlier draft: (1) the race where IDs get marked injected before the parent ever sees them (because `send_message` only queues, it does not deliver), and (2) the read-modify-write loss when concurrent writers update the variable. The mechanism: `_dedup_memory_results` (`src/gobby/hooks/hook_manager.py:737`) keys off result shape `{"memories": [{"id": …}]}`. `deliver_pending_messages` returns `{"messages": [...], "count": N}`. Phase 2.2 makes `inject_result` recognize helper `memory_recall` payloads in `messages` and merge their memories into the top-level result so existing dedup runs as-is.
- **Empty pending-message queues must be no-op injections.** Without explicit handling, `inject_result: true` would inject `{"success": true, "messages": [], "count": 0}` as visible JSON on every routine turn where no helper has anything to surface. Phase 2.1 makes `inject_result` skip injection when an mcp_call returns an empty/no-op payload.
- The existing `deliver-pending-messages` rule is gated on `variables.get('is_spawned_agent')`, which excludes user-facing parents. The gate must be removed; the underlying tool is session-scoped, so removing it does not cross-contaminate sessions.
- Helper must be hard-bounded: `max_turns: 3`, `timeout: 60s`. `AgentLifecycleMonitor` enforces both. These values live in the helper YAML, not in user-tunable config — the only runtime configurable for this feature is the `enabled` master kill-switch.
- Helper's `prompt` must contain dynamic per-turn content (parent_session_id, the parent's user prompt). `spawn_agent` has no separate `inputs:` parameter — everything dynamic is composed into the `prompt` string. Static instructions live on the agent definition.
- No new MCP tools, no new prompt-template files. The helper uses existing `gobby-memory.search_memories`, `gobby-agents.send_message`, `gobby-sessions.get_session`, top-level `get_variable`.
- The runtime master kill-switch is `DaemonConfig.memory_recall_helper.enabled`. It must be readable from the spawn rule's `when:` clause via a session variable seeded at `session_start` from the daemon's loaded config (rules cannot read `DaemonConfig` directly — `_build_eval_context` at `src/gobby/workflows/engine/templating.py:36–105` exposes only `event`, `variables`, `tool_input`, `source`, `project`).

## Phase 1: Foundation

**Goal**: Add the helper agent's master-toggle config, thread it through `EventHandlers` so its `enabled` flag is seeded into every new session as a variable, and create the helper's YAML definition.

### 1.1 Add `MemoryRecallHelperConfig` (single field) to `DaemonConfig` [category: code]

Target: `src/gobby/config/sessions.py` (config class) and `src/gobby/config/app.py` (`DaemonConfig` field).

Add a minimal `MemoryRecallHelperConfig` (Pydantic `BaseModel`, NOT extending `FeatureDefaultConfig`) with a single `enabled: bool` field, and attach it to `DaemonConfig` as a sibling of the existing `digest: DigestConfig` field at `src/gobby/config/app.py:288+`. The helper's model, timeouts, and search-tuning values are intentionally hardcoded in the helper agent YAML (1.3) — they are not user-tunable and adding orphan config fields would just be dead surface.

In `src/gobby/config/sessions.py`, add the class right after `DigestConfig` (which ends at line 151):

```python
class MemoryRecallHelperConfig(BaseModel):
    """Backgrounded Haiku memory-recall helper agent runtime toggle."""

    enabled: bool = Field(
        default=True,
        description="Enable the backgrounded LLM-driven memory recall helper agent.",
    )
```

`BaseModel` is the right base here; we are not exposing provider/model/tier overrides because the helper's runtime values are pinned in its YAML definition (1.3). If a future requirement exposes any of those for tuning, it can extend this class then.

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

### 1.2 Thread `memory_recall_helper` config to `EventHandlers` and seed `memory_recall_helper_enabled` on session_start [category: code] (depends: 1.1)

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

In `src/gobby/hooks/event_handlers/_session_start.py`, in `_activate_default_agent` (lines 841–965), before `sv_mgr.merge_variables(session_id, changes)` (~line 925), add the helper-enabled flag to the `changes` dict and to the `_ALWAYS_REAPPLY` set:

```python
# Seed runtime toggle for memory-recall-helper from DaemonConfig.
# Re-applied on every session_start so a config change at restart
# propagates to existing sessions on next session_start.
helper_cfg = self._memory_recall_helper_config
changes["memory_recall_helper_enabled"] = (
    bool(helper_cfg.enabled) if helper_cfg is not None else True
)
```

Add `"memory_recall_helper_enabled"` to the `_ALWAYS_REAPPLY` literal set defined inside `_activate_default_agent` (around line 911) so the value re-applies on compact/restart rather than being preserved as a stale truthy value:

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
}
```

Also add `"memory_recall_helper_enabled"` to the `internal_keys` set (~line 907) so `variables_count` continues to report only user-facing variables, not this internal flag.

Validation criteria: `EventHandlersBase._memory_recall_helper_config` exists with the `MemoryRecallHelperConfig | None` type. `EventHandlers(memory_recall_helper_config=...)` round-trips the value to `self._memory_recall_helper_config`. `factory.py` line 232+ passes `config.memory_recall_helper` when `config` is non-None and `None` otherwise. After daemon start with default config, every new session has `variables.get("memory_recall_helper_enabled") == True`. Setting `memory_recall_helper.enabled: false` in the config YAML and restarting the daemon causes new sessions to have `variables.get("memory_recall_helper_enabled") == False`. Existing sessions on compact/restart re-apply the flag (the `_ALWAYS_REAPPLY` set handles this). `tests/hooks/test_event_handlers.py` (or the equivalent test file for `_activate_default_agent`) gains a test asserting the variable is seeded.

### 1.3 Create `memory-recall-helper` agent definition [category: config]

Target: `src/gobby/install/shared/workflows/agents/memory-recall-helper.yaml` (new file).

This agent definition is consumed by the spawn rule in 3.2. It conforms to the `AgentDefinitionBody` schema (`src/gobby/workflows/definitions.py:303–408`) — specifically `name`, `description`, `enabled`, `surfaces`, `provider`, `model`, `timeout`, `max_turns`, `role`, `goal`, `instructions`, `blocked_tools`, `blocked_mcp_tools`. There is **no** `inputs:`, `system_prompt:`, or `allowed_tools:` block at the agent level — the schema does not accept them.

The helper does NOT mutate the parent's `injected_memory_ids` itself. It only reads it (to avoid re-surfacing already-seen memories). Tracking newly-surfaced IDs as injected is the parent-side delivery rule's job (Phase 2.2 + Phase 3.1). This avoids the early-mark race and the read-modify-write loss flagged by the round-1 review.

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
  You will be given: the parent session ID and the parent's user prompt for
  the current turn. Follow this process within your 3-turn budget.

  1. Fetch the parent's session digest:
       gobby-sessions.get_session(<parent_session_id>)
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
         content    = JSON: {"type": "memory_recall",
                             "memories": [<full memory records>],
                             "rationale": "<one short sentence>"}
     Each memory record MUST include `id` so the parent's delivery-side
     dedup can track it. The `type: "memory_recall"` marker is what
     Phase 2.2's payload-unwrapping recognizes. Do NOT call set_variable
     on the parent's injected_memory_ids — that is the parent's job.

  7. If nothing is clearly relevant, finish your turn without calling
     send_message. Do not pad.

  Hard constraints:
  - Cap: 3 surfaced memories per turn.
  - Never surface generic memories ("user prefers X" with no tie to current
    topic).
  - Never re-surface an ID already in injected_memory_ids.
  - Never write to injected_memory_ids yourself. Read-only.
  - You have at most 3 turns. Spend them on judgment, not exhaustive search.

# Belt-and-suspenders: keep the helper out of file edits and tool zoos.
# Top-level blocked_tools is a denylist (no `allowed_tools` at agent level
# in AgentDefinitionBody — only `blocked_tools` and `blocked_mcp_tools`).
blocked_tools:
  - Read
  - Write
  - Edit
  - Bash
  - Grep
  - Glob

# Block set_variable to enforce the read-only-on-parent contract; the helper
# legitimately needs get_variable but never set_variable.
blocked_mcp_tools:
  - "gobby:set_variable"
```

This file is synced to `workflow_definitions` in the DB on the next daemon startup with `enabled: true` (per the bundled-templates sync at `src/gobby/workflows/loader.py`).

Validation criteria: file exists at the listed path; `gobby agents list` after a daemon restart shows `memory-recall-helper`; the YAML parses against `AgentDefinitionBody` (`src/gobby/workflows/definitions.py:303–408`) without validation errors; `model` field is `claude-haiku-4-5`; `max_turns` is `3`; `timeout` is `60`; `blocked_mcp_tools` contains `"gobby:set_variable"`. The helper's `instructions` block contains explicit "Do NOT call set_variable" and "Read-only" language. A spawned helper attempting to call `set_variable` is blocked at the tool-routing layer (per `EnforcementMixin` and `_agent_blocked_mcp_tools` plumbing — verify by reading `src/gobby/workflows/engine/enforcement.py` and confirming the blocked-tool check covers MCP tool references in the documented form).

## Phase 2: Runtime correctness fixes (pre-wiring)

**Goal**: Two small targeted fixes to the rule effect engine that make Phase 3 wiring correct out of the box. These come BEFORE the wiring (Phase 3) so the wiring works correctly the first time the helper actually runs end-to-end.

### 2.1 Skip `inject_result` injection when an mcp_call returns an empty/no-op payload [category: code]

Target: `src/gobby/workflows/engine/effects.py` (`_apply_effect` method on `EffectsMixin`, around lines 142–166).

Today, `inject_result: true` injects whatever the mcp_call returns, formatted into `context_parts`. When `deliver_pending_messages` returns `{"success": true, "messages": [], "count": 0}` on an empty queue, the formatter would inject that as visible JSON in the parent's context every turn the queue is empty. That is unacceptable noise on the routine path and was identified by round-1 review as a blocking concrete bug (the previous draft handwaved it as "verify during implementation").

Add an explicit no-op short-circuit before formatting/injection, *only* in the `inject_result` path (not in the regular result handling, which other code may rely on):

```python
# Inside _apply_effect, in the inject_result=True branch, BEFORE formatting/appending:
if _is_empty_inject_payload(result):
    # Skip injection entirely — no context_parts append, no system message change.
    # Tool still ran (its side effects, like marking messages delivered, persist),
    # we just don't render an empty payload into the parent's context.
    return
```

Add the helper function in the same module (or a sibling util):

```python
def _is_empty_inject_payload(result: Any) -> bool:
    """Decide whether an mcp_call result represents 'nothing worth injecting.'

    Heuristic — covers the cases that actually arise in this codebase, in this order:

      1. result is None or not a dict: empty.
      2. result has explicit count == 0: empty (e.g., deliver_pending_messages).
      3. result has 'messages': [] AND no other non-bookkeeping keys: empty.
      4. result has 'memories': [] AND no other non-bookkeeping keys: empty.
      5. otherwise: not empty (inject as normal).

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

This affects all `inject_result: true` consumers, not just `deliver_pending_messages`. Audit existing consumers (`memory-recall-on-prompt.yaml` is one; grep for `inject_result: true` across `src/gobby/install/shared/workflows/rules/` to find the full list) and confirm: (a) every existing consumer is fine with empty results being skipped (they should be — empty memories injection is also noise), and (b) no test currently asserts an empty payload IS injected.

Validation criteria: `_is_empty_inject_payload({"success": True, "messages": [], "count": 0})` returns `True`. `_is_empty_inject_payload({"success": True, "messages": [], "count": 0, "response_time_ms": 12})` returns `True`. `_is_empty_inject_payload({"success": True, "messages": [{"id": "m1"}], "count": 1})` returns `False`. `_is_empty_inject_payload({"success": True, "memories": []})` returns `True`. `_is_empty_inject_payload({"success": True, "memories": [{"id": "m1"}]})` returns `False`. `_is_empty_inject_payload(None)` returns `True`. `_is_empty_inject_payload({})` returns `True`. End-to-end: with the changed `deliver-pending-messages` rule (Phase 3.1) firing on a parent's `turn_start` while the queue is empty, no inter-session-message context is appended to the parent's prompt — verifiable by inspecting the prompt or `system_message` in the hook response. All existing tests under `tests/workflows/` and `tests/hooks/` that touch `inject_result` continue to pass; if any test relied on empty-payload injection, it must be explicitly updated and the change documented.

### 2.2 Unwrap helper `memory_recall` payloads in `inject_result` so `_dedup_memory_results` runs at delivery time [category: code]

Target: `src/gobby/workflows/engine/effects.py` (in or adjacent to `_apply_effect`'s `inject_result` path) and `src/gobby/hooks/hook_manager.py` (the dedup call site at line 634).

The helper sends inter-session messages with content shaped:

```json
{"type": "memory_recall", "memories": [{"id": "...", ...}, ...], "rationale": "..."}
```

`deliver_pending_messages` returns `{"success": true, "messages": [<msg>...], "count": N}` where each `<msg>` carries that JSON content. To make `_dedup_memory_results` (`src/gobby/hooks/hook_manager.py:737`) run on the helper's memories the same way it runs on `search_memories` results — which is what makes "fast recall and helper agree on a memory ID, render once" work — the result needs to expose those memories at the top level under a `memories` key.

Add a normalization step in `_apply_effect` that runs in the `inject_result: true` path (after the empty-check from 2.1, before any formatting/append) when the calling tool is `deliver_pending_messages`:

```python
# Inside _apply_effect, inject_result=True branch, after empty-check:
tool_name = effect.tool  # already in scope as the effect's tool name
server_name = effect.server
if (server_name, tool_name) == ("gobby-agents", "deliver_pending_messages"):
    result = _normalize_helper_memory_payloads(result)
```

Add the normalization helper:

```python
def _normalize_helper_memory_payloads(result: dict[str, Any]) -> dict[str, Any]:
    """If deliver_pending_messages returned helper memory_recall payloads,
    merge their memories into a top-level 'memories' key so _dedup_memory_results
    fires at delivery time.

    Non-memory-recall messages are left in 'messages' untouched. If any
    memory_recall messages were present, the returned dict has BOTH the
    original 'messages' and a top-level 'memories' list (de-duplicated by id,
    last-write-wins on metadata).
    """
    if not isinstance(result, dict):
        return result
    messages = result.get("messages")
    if not messages or not isinstance(messages, list):
        return result

    merged: dict[str, dict[str, Any]] = {}  # id -> memory dict
    for msg in messages:
        content = _coerce_message_content(msg)
        if not isinstance(content, dict):
            continue
        if content.get("type") != "memory_recall":
            continue
        for mem in content.get("memories") or []:
            mid = mem.get("id") if isinstance(mem, dict) else None
            if not mid:
                continue
            merged[mid] = mem
    if not merged:
        return result
    return {**result, "memories": list(merged.values())}


def _coerce_message_content(msg: Any) -> Any:
    """Inter-session messages may carry content as JSON string or dict.
    Returns a dict if parseable, else the raw value."""
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return content
    return content
```

The dedup integration point is `_evaluate_workflow_rules` at `src/gobby/hooks/hook_manager.py:634`:

```python
dr["result"] = self._dedup_memory_results(dr["result"], session_id)
```

Today this fires when the tool's name is `search_memories` (or whichever logic gates dedup — verify by reading the surrounding code). After 2.2 the `deliver_pending_messages` result has a top-level `memories` key when helper payloads arrived, so the dedup path needs to run on it too. Either:

- Loosen the dedup gate so it runs whenever `result` has a `memories` key, regardless of tool name, OR
- Add an explicit branch in the dedup call site for `("gobby-agents", "deliver_pending_messages")`.

Pick whichever is closer to the existing gating style. Document the choice in the validation criteria below.

`_dedup_memory_results` already does the right thing once it runs: it filters `result["memories"]` by `injected_memory_ids` and appends the surviving IDs back via `sv_mgr.append_to_set_variable(..., "injected_memory_ids", new_ids)`. That `append_to_set_variable` is the atomic primitive — there is no read-modify-write race on the helper-side variable update because it happens on the parent's delivery rule's evaluation thread, after the helper has already finished its turn and sent the message.

Validation criteria: `_normalize_helper_memory_payloads({"messages": [{"content": '{"type":"memory_recall","memories":[{"id":"m1"}]}'}], "count": 1})` returns a dict with a `memories: [{"id": "m1"}]` key alongside the original `messages`. `_normalize_helper_memory_payloads({"messages": [{"content": "plain text"}], "count": 1})` returns the original dict unchanged (no `memories` key added). `_normalize_helper_memory_payloads({"messages": [], "count": 0})` returns the original dict unchanged. End-to-end: a helper run that surfaces memory `m_xyz` via `send_message` results, on the parent's next `turn_start`, in `m_xyz` being injected once and `m_xyz` being appended to the parent's `injected_memory_ids` session variable — verifiable by `get_variable(name="injected_memory_ids")` after the turn. A second helper run on a later turn that selects `m_xyz` again (because it's still semantically relevant) results in `_dedup_memory_results` filtering it out before injection — verifiable by inspecting hook responses across two turns. Whichever dedup-gate-loosening choice is made (tool-name-agnostic vs explicit branch), there is a unit test in `tests/hooks/test_hook_manager.py` covering the new path.

## Phase 3: Wiring (depends: Phase 1, Phase 2)

**Goal**: Spawn the helper at parent `turn_start` and deliver its output (correctly, with dedup and no empty-noise) at the parent's next `turn_start`.

### 3.1 Modify `deliver-pending-messages` rule to fire for parent sessions [category: config] (depends: 2.1, 2.2)

Target: `src/gobby/install/shared/workflows/rules/messaging/deliver-pending-messages.yaml` (existing file).

The current rule is gated on `when: "variables.get('is_spawned_agent')"`. This excludes user-facing parent sessions. Drop the gate and add `inject_result: true` so the helper's payload actually surfaces in the parent's context. The underlying `gobby-agents.deliver_pending_messages` MCP tool (`src/gobby/mcp_proxy/tools/agent_messaging.py:318`) is session-scoped — it returns only the calling session's queue — so removing the gate does not cross-contaminate sessions. Phase 2.1 ensures the empty-queue case is a no-op injection; Phase 2.2 ensures helper memory payloads route through `_dedup_memory_results` at delivery time.

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
        inject_result: true
```

Two changes vs the existing file:
1. Drop the `when: "variables.get('is_spawned_agent')"` line.
2. Add `inject_result: true` to the `mcp_call` effect.

Existing tests touching this rule (`tests/e2e/test_inter_agent_messages.py::test_parent_child_message_exchange`) must continue to pass — child → parent and parent → child messaging both still rely on this rule, so gate removal must not regress those flows.

Validation criteria: file at the listed path matches the YAML above exactly. Daemon restart loads the rule; `gobby rules list` shows `deliver-pending-messages` enabled with no `when:` clause and `inject_result: true` on the mcp_call effect. `tests/e2e/test_inter_agent_messages.py` passes after the change. A manual end-to-end test with a parent session sending itself a structured `memory_recall` message via `gobby-agents.send_message(to_session=#<self>, content='{"type":"memory_recall","memories":[{"id":"test1"}],"rationale":"manual"}')` followed by a turn_start results in (a) the message rendering in the parent's context, (b) `injected_memory_ids` containing `"test1"` after the turn, and (c) a second turn with the same content message resulting in `test1` being filtered out by `_dedup_memory_results` so it is NOT re-injected. A turn with no pending messages results in NO `inject_result` noise in the parent's context (verifying Phase 2.1 hand-off).

### 3.2 Create `spawn-memory-recall-helper` rule [category: config] (depends: 1.2, 1.3, 3.1)

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
          parent_session_id: "{{ event.session_id }}"
          prompt: |
            Parent session: {{ event.session_id }}
            Parent's user prompt for this turn:

            {{ event.data.prompt }}

            Follow your standing instructions: fetch the parent's digest via
            get_session, read injected_memory_ids on the parent, run focused
            search_memories, select 0–3 clearly-relevant memories that
            haven't already been surfaced, and either send_message them to
            the parent or finish silently. Do NOT write to
            injected_memory_ids yourself — the parent's delivery flow
            handles that.
        background: true
```

Why each clause:

- `event: turn_start` — fires once per user prompt, in parallel with the existing `memory-recall-on-prompt` (priority 10).
- `priority: 12` — runs *after* the fast vector recall so the immediate baseline ships first.
- `when: len(prompt.split()) >= 6` — mirrors the existing recall rule's skip-trivial-prompts check.
- `when: not variables.get('is_spawned_agent')` — prevents the helper from spawning *another* helper if a spawned agent ever issues a prompt (the helper itself is a spawned agent, so without this it would self-fork).
- `when: variables.get('memory_recall_helper_enabled', True)` — runtime master kill-switch. Seeded from `DaemonConfig.memory_recall_helper.enabled` at every `session_start` by 1.2. Default-True means a fresh session with a misconfigured daemon (no daemon_config available to `EventHandlers`) still spawns the helper rather than silently disabling it.
- `background: true` — `mcp_call` effect runs without blocking turn_start (per `src/gobby/workflows/engine/effects.py:142–166`).
- `parent_session_id` and `prompt` are composed via `{{ event.session_id }}` and `{{ event.data.prompt }}` — the rule template engine exposes `event` directly per `_build_eval_context` (`src/gobby/workflows/engine/templating.py:36–105`).

Validation criteria: file exists at the listed path; daemon restart loads the rule; `gobby rules list` shows `spawn-memory-recall-helper` enabled at priority 12 with the documented `when:` clause. Submitting a real prompt of ≥ 6 words to a parent (non-spawned-agent) session triggers a spawn — `gobby agents runs list` shows a new `memory-recall-helper` run with status `running` shortly after the prompt. Submitting a 1-word prompt does not spawn. Manually setting `is_spawned_agent: true` on a session via `set_variable` and submitting a prompt does NOT spawn. Setting `memory_recall_helper.enabled: false` in the daemon config and restarting causes new sessions to NOT spawn the helper on prompts. The parent session's `turn_start` is not blocked — Claude Code starts streaming a response within the normal latency window (no Haiku-call wait inserted into the critical path). When the helper completes and `send_message`s a `memory_recall` payload, the parent's NEXT `turn_start` (a) injects the helper's selected memories once, (b) updates `injected_memory_ids` with the surfaced IDs (verifiable by `get_variable`), and (c) on a subsequent helper turn, those IDs are filtered out by `_dedup_memory_results` before injection.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
