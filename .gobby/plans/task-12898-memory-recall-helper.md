# Smarter memory recall via backgrounded Haiku helper agent

## Overview

Replace score-only synchronous memory recall with an LLM-judgment-driven backgrounded Haiku helper agent. The helper runs in parallel with the existing fast vector recall on every `turn_start`, takes a holistic view of the parent's session digest + prompt, runs iterative `search_memories` calls, and either `send_message`s 0–3 selected memories back to the parent or finishes silently. Existing fast vector recall stays in place as the immediate baseline; the helper supplements it with smarter selections delivered at the parent's next `turn_start` via the existing inter-session messaging rule (with its `is_spawned_agent` gate dropped so parents receive too).

## Constraints

- The synchronous fast-recall path (`memory-recall-on-prompt`) and the rolling digest pipeline (`digest-on-response`) are out of scope — they continue unchanged. The helper consumes the digest produced at `turn_end` of the previous turn; it never produces it.
- Helper must run backgrounded. Adding LLM latency to `turn_start` is unacceptable.
- `PreToolUse` (`before_tool`) does not fire on text-only assistant turns in Claude Code, so delivery happens at the next `turn_start` only.
- **Dedup tracking is on the parent's delivery side, on the inline `inject_result` path inside `_apply_effect` (`src/gobby/workflows/engine/effects.py:57+`), NOT in `HookManager._evaluate_workflow_rules` (`src/gobby/hooks/hook_manager.py:597–698`).** The hook-manager dedup loop only runs on deferred `dispatch_result` items; the `inject_result: true` path is inline-dispatched directly inside `_apply_effect` and never produces a dispatch_result. Phase 2.3 implements the full delivery-time pipeline (normalize → dedup → strip handled messages → format) on the inline path so it actually fires for `deliver_pending_messages` results.
- **Helper is read-only on `injected_memory_ids`.** Helper reads it before selecting (to avoid re-surfacing already-seen memories) but never writes. Writing happens in 2.3 via `SessionVariableManager.append_to_set_variable` — the existing atomic primitive `_dedup_memory_results` uses. This eliminates the round-1 race where IDs got marked injected before the parent ever saw them and the read-modify-write loss between concurrent writers. The read-only contract is enforced at the runtime layer by 2.2, which makes agent `blocked_tools` override the default infrastructure-tool exempt so the helper's `mcp__gobby__set_variable` calls are actually blocked (the `is_infrastructure_tool` exempt path in `_check_agent_tool_enforcement` (`src/gobby/workflows/engine/enforcement.py`) currently returns before any block-list is consulted, which makes `blocked_mcp_tools` and a naive `blocked_tools` placement non-functional for proxy infra tools).
- **Empty pending-message queues must be no-op injections.** Without explicit handling, `inject_result: true` would inject `{"success": true, "messages": [], "count": 0}` as visible JSON on every routine turn where no helper has anything to surface. Phase 2.3's pipeline includes an early empty-payload short-circuit so the inline path skips injection cleanly.
- **First-time deliveries must render each helper memory exactly once.** Without explicit handling, `inject_result` would dump the raw `messages[*].content` AND the normalized top-level `memories`, so a fresh memory would render twice on the first delivery. Phase 2.3's pipeline strips handled `memory_recall` messages out of the `messages` array before formatting, so rendered output contains the deduped helper memories once and any non-`memory_recall` messages still passing through.
- **The existing `deliver-pending-messages` rule needs an explicit `arguments: { target_session_id: "{{ event.session_id }}" }` block.** The dispatcher does not auto-inject `target_session_id` (only `session_id`, which `deliver_pending_messages` does not accept). The current rule, with no `arguments`, would not actually invoke the tool successfully. Phase 3.1 fixes this in the rule body.
- **The helper sends with `from_session=<helper's own child session id>`.** `send_message`'s schema requires `from_session`. The helper does not know its child session id at prompt-construction time (the spawn rule cannot capture the spawn return value because `background: true`). Phase 2.1 makes `from_session` optional in `send_message` and defaults it from `SessionContext` (the proxy's session-context header) when omitted, so callers running through the proxy do not need to know their own session id explicitly. The helper's instructions then say "omit from_session — it auto-fills from your session context."
- The existing `deliver-pending-messages` rule is gated on `variables.get('is_spawned_agent')`, which excludes user-facing parents. The gate must be removed; the underlying tool is session-scoped, so removing it does not cross-contaminate sessions.
- Helper must be hard-bounded: `max_turns: 3`, `timeout: 60s`. `AgentLifecycleMonitor` enforces both. These values live in the helper YAML, not in user-tunable config — the only runtime configurable for this feature is the `enabled` master kill-switch.
- Helper's `prompt` must contain dynamic per-turn content (parent_session_id, the parent's user prompt). `spawn_agent` has no separate `inputs:` parameter — everything dynamic is composed into the `prompt` string. Static instructions live on the agent definition.
- No new MCP tools (we modify existing ones), no new prompt-template files. The helper uses existing `gobby-memory.search_memories`, `gobby-agents.send_message`, `gobby-sessions.get_session`, top-level `get_variable`.
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

The helper does NOT mutate the parent's `injected_memory_ids` itself. It only reads it (to avoid re-surfacing already-seen memories). Tracking newly-surfaced IDs as injected is the parent-side delivery pipeline's job (2.3). The helper's read-only contract is enforced at the runtime layer by 2.2 (which makes the `blocked_tools` listing of `mcp__gobby__set_variable` actually take effect for the helper's session).

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
  You will be given: the parent session ID and the parent's user prompt for
  the current turn. Follow this process within your 3-turn budget.

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
                       "memories": [<full memory records>],
                       "rationale": "<one short sentence>"}
     OMIT the `from_session` argument — the proxy auto-fills it from your
     session context (this is the runtime change made in 2.1). Each memory
     record MUST include `id` so the parent's delivery-side dedup can track
     it. The literal string "memory_recall" in the `type` field is what
     2.3's normalization pipeline keys off; do not use a different value.

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

The helper's `instructions` block contains explicit "OMIT the `from_session` argument", "Do NOT write to injected_memory_ids", and the literal string `"memory_recall"` in the documented JSON content shape. A spawned helper that attempts to call top-level `mcp__gobby__set_variable` is blocked at the tool-routing layer with a `[agent-enforcement:memory-recall-helper]` reason (made functional by 2.2's enforcement reorder — verify by integration test in `tests/workflows/test_step_enforcement.py` that spawns a helper-equivalent agent definition with `mcp__gobby__set_variable` in `blocked_tools` and asserts the call is blocked).

## Phase 2: Runtime correctness fixes (pre-wiring)

**Goal**: Three targeted runtime changes that make Phase 3 wiring correct out of the box: (2.1) auto-fill `from_session` on `send_message` so the helper does not need its own child session id; (2.2) reorder `_check_agent_tool_enforcement` so explicit `blocked_tools` listings override the infrastructure-tool exempt and the helper's read-only contract is actually enforced; (2.3) implement helper-aware delivery on `_apply_effect`'s inline `inject_result` path so it correctly normalizes, dedupes, and renders helper memory payloads exactly once. All three come BEFORE the wiring (Phase 3) so the wiring works correctly the first time the helper actually runs end-to-end.

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
        from gobby.mcp_proxy.session_context import SessionContext  # adjust import path to match existing usage

        ctx_session_id = SessionContext.get_session_id()  # use whichever accessor the existing module exposes
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

If the existing `agent_messaging.py` does not import a `SessionContext` accessor, look for the existing pattern other tools in the same file or `src/gobby/mcp_proxy/tools/` use to read session_id from the proxy's request context. The `mcp__gobby__call_tool` description states this propagation is already in place; the accessor exists somewhere — verify its module path during implementation by searching for `SessionContext` in `src/gobby/mcp_proxy/`.

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

### 2.3 Helper-aware delivery on the inline `inject_result` path [category: code] (depends: 2.1)

Target: `src/gobby/workflows/engine/effects.py`, inside `EffectsMixin._apply_effect` (around line 100–119 — the `effect.type == "mcp_call"` branch where `effect.inject_result and not effect.background and self._mcp_dispatcher` is true).

This is the path that 3.1's modified `deliver-pending-messages` rule actually invokes. The current inline path:

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

    if (effect.server, effect.tool) == ("gobby-agents", "deliver_pending_messages"):
        # Helper-aware delivery: normalize, dedup, strip handled messages, format remaining.
        formatted = self._format_delivery_result(raw_result, ctx.get("event"), variables)
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
    event: Any | None,
    variables: dict[str, Any],
) -> str | None:
    """Inline delivery-time pipeline for deliver_pending_messages results.

    Steps:
      1. Empty short-circuit (count=0 or empty messages) → no injection.
      2. Partition messages into helper memory_recall payloads and other messages.
      3. Collect all helper memories, dedupe by id within this delivery.
      4. Filter out memory ids already in the parent's injected_memory_ids
         (read via SessionVariableManager).
      5. If any newly-injected ids remain, atomic-append them via
         sv_mgr.append_to_set_variable(session_id, "injected_memory_ids", new_ids).
         (Same primitive _dedup_memory_results uses — race-free across writers.)
      6. Format output:
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
            for mem in parsed.get("memories") or []:
                mid = mem.get("id") if isinstance(mem, dict) else None
                if mid:
                    helper_memories[mid] = mem  # last-write-wins
        else:
            other_messages.append(msg)

    # Dedup against parent's injected_memory_ids; atomic append survivors.
    new_memories: list[dict[str, Any]] = []
    if helper_memories:
        session_id = event.session_id if event is not None else None
        sv_mgr = SessionVariableManager(self.db) if session_id else None
        already: set[str] = set()
        if sv_mgr is not None:
            try:
                existing_vars = sv_mgr.get_variables(session_id)
                already = set(existing_vars.get("injected_memory_ids", []) or [])
            except Exception as e:  # noqa: BLE001 — fail open on dedup-state read
                logger.debug(f"Failed to read injected_memory_ids for dedup: {e}")
                already = set()
        new_memories = [m for m in helper_memories.values() if m.get("id") not in already]
        new_ids = [m["id"] for m in new_memories if m.get("id")]
        if new_ids and sv_mgr is not None and session_id:
            try:
                sv_mgr.append_to_set_variable(session_id, "injected_memory_ids", new_ids)
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

Notes for the implementer:

- The `format_discovery_result` synthetic dispatch with `tool="search_memories"` is a deliberate reuse of the existing memory formatter so helper-surfaced memories render identically to fast-recall memories (consistent UX, single audit format). If `format_discovery_result` does not have a `search_memories` formatter today, add a minimal one alongside this work — verify by reading `src/gobby/hooks/dispatchers/mcp.py`'s formatter dispatch.
- `SessionVariableManager.append_to_set_variable` is the same atomic primitive `_dedup_memory_results` (`src/gobby/hooks/hook_manager.py:737`) uses for the same variable. Race-free across concurrent writers because it goes through the same DB transaction path.
- Empty short-circuit comes first so the empty-queue noise case ALSO covers any future tool with `inject_result: true`. Helper-aware path only fires for `deliver_pending_messages`.
- Errors in dedup-state read or write are fail-open (log debug, proceed) — same posture as `_dedup_memory_results`.
- `HookManager._evaluate_workflow_rules`'s dedup loop is left untouched. We do NOT need it to fire on `deliver_pending_messages` results; 2.3 handles the full pipeline inline.

Validation criteria: unit tests in a new `tests/workflows/test_delivery_pipeline.py` cover all of: (1) empty result `{"messages": [], "count": 0}` → `_format_delivery_result` returns `None`, no `context_parts.append` called, no `injected_memory_ids` mutation; (2) result with one `memory_recall` message containing memory `m1` and `injected_memory_ids` initially empty → returns formatted string containing `m1` rendered through the search_memories formatter, and `injected_memory_ids` after the call contains `["m1"]`; (3) result with `memory_recall` containing `m1` when `injected_memory_ids` already contains `m1` → returns `None` (or empty) and `injected_memory_ids` unchanged; (4) result with `memory_recall` (`m1`) AND a non-memory_recall plain text message → returned formatted string contains `m1` once (NOT twice in raw messages and once in memories) AND the plain text message; (5) result with malformed message content (not JSON) → message falls through to "other_messages" and renders via generic formatter; (6) two concurrent calls to `append_to_set_variable` from different rule evaluations do not lose either's IDs (race test). End-to-end (manual): submit a real prompt, observe a helper run surface memory `m_xyz`, observe on the parent's next turn that `m_xyz` appears once in injected context and `get_variable(name="injected_memory_ids", session_id=#<self>)` includes `m_xyz`; on a subsequent turn where the helper selects `m_xyz` again, `m_xyz` does NOT re-appear in injected context. `tests/e2e/test_inter_agent_messages.py` continues to pass (parent ↔ child messaging via `send_message` + `deliver_pending_messages` is unaffected because non-memory_recall messages still flow through `other_messages`).

## Phase 3: Wiring (depends: Phase 1, Phase 2)

**Goal**: Spawn the helper at parent `turn_start` and deliver its output (correctly: with dedup, no empty-noise, no double-render) at the parent's next `turn_start`.

### 3.1 Modify `deliver-pending-messages` rule to fire for parent sessions [category: config] (depends: 2.3)

Target: `src/gobby/install/shared/workflows/rules/messaging/deliver-pending-messages.yaml` (existing file).

Three changes vs the current file:

1. Drop the `when: "variables.get('is_spawned_agent')"` line so the rule fires for parents too (the underlying tool is session-scoped).
2. Add an explicit `arguments: { target_session_id: "{{ event.session_id }}" }` block — the dispatcher does not auto-inject `target_session_id` (only `session_id`), and `deliver_pending_messages`'s schema requires `target_session_id`.
3. Add `inject_result: true` to the `mcp_call` effect. Phase 2.3's pipeline is the consumer of `inject_result` for this tool.

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
          target_session_id: "{{ event.session_id }}"
        inject_result: true
```

Existing tests touching this rule (`tests/e2e/test_inter_agent_messages.py::test_parent_child_message_exchange`) must continue to pass — child → parent and parent → child messaging both still rely on this rule, so gate removal must not regress those flows.

Validation criteria: file at the listed path matches the YAML above exactly. Daemon restart loads the rule; `gobby rules list` shows `deliver-pending-messages` enabled with no `when:` clause and `inject_result: true` on the mcp_call effect with the documented `arguments` block.

**Rule-definition tests (required, not optional)**: update `tests/workflows/test_messaging_rules.py::TestDeliverPendingMessages` (which currently hard-codes the old `is_spawned_agent` gate and no-arguments effect) to assert the new contract:

- No `when:` clause on the rule definition (the test must explicitly check `rule.condition is None` or equivalent — failing if a stale `is_spawned_agent` gate is reintroduced).
- Effect's `arguments` field equals `{"target_session_id": "{{ event.session_id }}"}` (string match on the templated value, exactly as written in the YAML).
- Effect's `inject_result` field is `True`.
- Effect's `server` is `"gobby-agents"` and `tool` is `"deliver_pending_messages"`.
- Rule's `event` is `"turn_start"` and `priority` is `10`.

`tests/e2e/test_inter_agent_messages.py` passes after the change. A manual end-to-end test: from a parent session, call `send_message(to_session=#<self>, content='{"type":"memory_recall","memories":[{"id":"test1","content":"test memory"}],"rationale":"manual"}')` (omitting `from_session` to verify 2.1's auto-fill), then trigger a `turn_start`; result is (a) the test memory rendered ONCE via the search_memories formatter in the parent's context, (b) `get_variable(name="injected_memory_ids", session_id=#<self>)` containing `"test1"` after the turn, and (c) repeating the same content message and another turn_start results in `test1` being filtered (no re-injection) and `injected_memory_ids` unchanged. A turn_start with no pending messages results in NO `inject_result` noise in the parent's context.

### 3.2 Create `spawn-memory-recall-helper` rule [category: config] (depends: 1.2, 1.3, 2.2, 3.1)

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
            the parent (omitting from_session — the proxy auto-fills it
            from your session context) or finish silently. Do NOT write to
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
- The prompt explicitly tells the helper to omit `from_session` on `send_message` calls. 2.1's runtime change auto-fills it from the helper's SessionContext (the proxy populates it from the helper's session header), so the helper does not need to know its own child session id.

Validation criteria: file exists at the listed path; daemon restart loads the rule; `gobby rules list` shows `spawn-memory-recall-helper` enabled at priority 12 with the documented `when:` clause.

**Rule-definition tests (required, not optional)**: add a new rule-level test class `TestSpawnMemoryRecallHelper` to `tests/workflows/test_memory_lifecycle_rules.py` paralleling the existing `TestMemoryRecallOnPrompt` class in the same file (which is the closest structural analog — both are `turn_start` rules with a `when:` clause and a single `mcp_call` effect). The new class asserts the rule's contract:

- Rule's `event` is `"turn_start"`, `priority` is `12`, `enabled` is `True`.
- Rule's `condition` (the `when:` clause) contains all three guards as substrings (or parses to an AST including all three): (a) `len((event.data.get('prompt') or '').split()) >= 6`, (b) `not variables.get('is_spawned_agent')`, (c) `variables.get('memory_recall_helper_enabled', True)`.
- Rule has exactly one effect of type `mcp_call`.
- Effect's `server` is `"gobby-agents"`, `tool` is `"spawn_agent"`, `background` is `True`.
- Effect's `arguments` includes `agent: "memory-recall-helper"` and `parent_session_id: "{{ event.session_id }}"` (string match).
- Effect's `arguments.prompt` is a non-empty string containing the literal `"{{ event.session_id }}"` and `"{{ event.data.prompt }}"` template references.

Additionally, add `"spawn-memory-recall-helper"` to the `MEMORY_RULES` set defined at the top of `tests/workflows/test_memory_lifecycle_rules.py` (line 33). That manifest is consulted by `TestMemoryLifecycleSync` (lines 73, 82, 92) for cross-rule sync checks; omitting the new rule here would leave it outside the file's existing coverage net.

Behavioral validation: submitting a real prompt of ≥ 6 words to a parent (non-spawned-agent) session triggers a spawn — `gobby agents runs list --status running --json` shows a new run shortly after the prompt with `agent_name == "memory-recall-helper"` (use the JSON variant — plain text output may not include the agent name). Equivalent: `gobby agents runs show <run_id_prefix> --json` and assert `agent_name`. Direct-DB equivalent: query `agent_runs` for `agent_name='memory-recall-helper'` ordered by `created_at DESC` and inspect the most recent row. Submitting a 1-word prompt does not spawn. Manually setting `is_spawned_agent: true` on a session via `set_variable` and submitting a prompt does NOT spawn. Setting `memory_recall_helper.enabled: false` in the daemon config and restarting causes new sessions to NOT spawn the helper on prompts. The parent session's `turn_start` is not blocked — Claude Code starts streaming a response within the normal latency window (no Haiku-call wait inserted into the critical path). When the helper completes and `send_message`s a `memory_recall` payload (omitting `from_session`), the parent's NEXT `turn_start` (a) injects the helper's selected memories once via the search_memories formatter (NOT as raw JSON dump of the message body), (b) appends the surfaced IDs to the parent's `injected_memory_ids` (verifiable by `get_variable`), and (c) on a subsequent helper turn that re-selects those IDs, dedup filters them out before injection.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
