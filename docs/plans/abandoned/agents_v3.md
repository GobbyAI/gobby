# Agents v3: Selectors + Extends + Inherit

## Context

The current agent-to-rule binding doesn't scale. `AgentWorkflows.rules` is a flat list of rule names (50+ rules = 50 entries), and `agent_scope` on rules requires editing existing rules for every new agent. There's no inheritance between agents, no way to say "use the host CLI's provider/model", and `mode: self` isn't in the definition model despite existing in the spawn layer.

**Goal**: Every session activates a baseline agent (`mode: self`, `provider: inherit`) that loads rules via selectors (by tag, group, name, source). Derived agents use `extends` to inherit and override the baseline's rule/variable set.

---

## Task Breakdown (Epic #9085 — subtasks to be expanded after plan approval)

| # | Title | Depends On |
|---|-------|-----------|
| 1 | Data model changes (AgentSelector, extends, mode:self, inherit, VariableDefinitionBody) | — |
| 2 | Selector resolution engine (`selectors.py`) | 1 |
| 3 | Agent resolver with extends chain (`agent_resolver.py`) | 1, 2 |
| 4 | Rule engine `_filter_by_active_rules` | 1 |
| 5 | mode:self persona path (no workflow required) | 1, 2, 3 |
| 6 | Spawn factory uses resolver | 3 |
| 7 | Deep load default agent at session start + config | 2, 3, 4 |
| 8 | Update default.yaml template | 1 |
| 9 | Variable YAML templates + sync | 1 |
| 10 | Variable loading at session start + deprecate init-* rules | 7, 9 |
| 11 | Documentation: delete `agents.md`, write `agent_definitions.md` | all |

---

## Phase 1: Selectors + Extends + Inherit + mode:self + default_agent

### Step 1: Data Model Changes

**File**: `src/gobby/workflows/definitions.py`

Add `AgentSelector` model:
```python
class AgentSelector(BaseModel):
    include: list[str] = Field(default_factory=lambda: ["*"])
    exclude: list[str] = Field(default_factory=list)
```

Selector string format: `tag:X`, `group:X`, `name:X`, `source:X`, `*`. Fnmatch globs within each. Multiple includes are OR'd. Exclude wins on conflict.

Modify `AgentWorkflows`:
- Add `rule_selectors: AgentSelector | None = None` (new selector-based activation)
- Keep existing `rules: list[str]` for backward compat (explicit names merged with selector results)
- Add `variable_selectors: AgentSelector | None = None` (Phase 2 placeholder)

Modify `AgentDefinitionBody`:
- `provider: str = "inherit"` (changed from `"claude"`)
- `model: str | None = None` (unchanged — None already means inherit)
- `mode: Literal["terminal", "embedded", "headless", "self"] = "self"` (added `"self"`, changed default)
- Add `extends: str | None = None` (parent agent name)
- Change `base_branch` default from `"main"` to `"inherit"` — sentinel meaning "auto-detect current branch at spawn time". Consistent with `provider: inherit` and `model: inherit` — one concept everywhere. Only relevant when `isolation` is `worktree`/`clone`; ignored for `mode: self` and `isolation: none`. Resolve in `_implementation.py` where auto-detect already exists (line 198-200).

### Step 2: Selector Resolution Engine — NEW FILE

**File**: `src/gobby/workflows/selectors.py`

Two functions:
- `parse_selector(s: str) -> tuple[str, str]` — splits `"tag:gobby"` into `("tag", "gobby")`
- `resolve_rules_for_agent(agent: AgentDefinitionBody, all_rules: list[WorkflowDefinitionRow]) -> set[str]`
  - Combines explicit `workflows.rules` names + `workflows.rule_selectors` matches
  - Returns set of rule names

Selector matching per dimension:
- `tag:X` → `any(fnmatch(t, X) for t in row.tags)`
- `group:X` → `fnmatch(jsonb_path_query(definition_json, '$.group'), X)`
- `name:X` → `fnmatch(row.name, X)`
- `source:X` → `fnmatch(row.source, X)`
- `*` → always True

Include is OR (any match = included). Then exclude is subtracted (any match = excluded).

### Step 3: Agent Resolver with Extends Chain — NEW FILE

**File**: `src/gobby/workflows/agent_resolver.py`

- `resolve_agent(name: str, db: HubDatabase, cli_source: str | None = None) -> AgentDefinitionBody | None`
  - Follows `extends` chain up to `MAX_EXTENDS_DEPTH = 5`
  - Cycle detection via `seen: set[str]`
  - Merges from root ancestor → leaf (child overrides parent)
  - Resolves `"inherit"` provider from `cli_source`

Merge logic (`_merge_agent_bodies`):
- Use Pydantic `model_fields_set` to detect which fields the child explicitly set
- Child's explicitly-set fields override parent
- `workflows.rules`: concatenate + deduplicate
- `workflows.rule_selectors`: child wins if set, else parent's
- `workflows.variables`: dict merge (child wins on key conflict)
- `extends`: cleared in merged result

Provider inherit resolution — only normalize the divergent cases:
```python
def _normalize_provider(cli_source: str) -> str:
    """Resolve 'inherit' to provider. Only claude_sdk variants and antigravity need mapping."""
    if cli_source.startswith("claude_sdk") or cli_source == "antigravity":
        return "claude"
    return cli_source  # claude, gemini, codex, cursor, windsurf, copilot pass through
```

For `mode: self` agents, `inherit` stays unresolved — provider only matters when a derived `mode: terminal` agent spawns.

### Step 4: Config Setting

**File**: `src/gobby/config/app.py`

Add to `DaemonConfig`:
```python
default_agent: str = Field(default="default", description="Agent definition activated on session_start. Set to 'none' to disable.")
```

### Step 5: Deep Load at Session Start

**File**: `src/gobby/hooks/event_handlers/_session.py`

Add `_activate_default_agent()` method, called in `handle_session_start` after session registration (after step 2c, before step 4). Only for new sessions, NOT pre-created sessions.

This method:
1. Reads `default_agent` from config (via `ConfigStore`)
2. Calls `resolve_agent(name, db, cli_source)` to get the fully resolved agent body
3. Calls `resolve_rules_for_agent(agent_body, all_enabled_rules)` to get active rule names
4. Writes to `session_variables` via `SessionVariableManager.merge_variables()`:
   - `_agent_type = agent_body.name`
   - `_active_rule_names = [resolved rule names]`
   - Any preset variables from `agent_body.workflows.variables`

**Why this works as a "deep load"**: The handler runs first for SESSION_START (hook_manager.py:341-346). By the time `_evaluate_workflow_rules` fires (line 348), session variables already contain `_agent_type` and `_active_rule_names`. The rule engine loads these at hooks.py:92 and filters accordingly.

### Step 6: Rule Engine Filtering

**File**: `src/gobby/workflows/rule_engine.py`

Add `_filter_by_active_rules()` method after `_filter_by_agent_scope` (line 91):

```python
def _filter_by_active_rules(self, rules, variables):
    active_names = variables.get("_active_rule_names")
    if active_names is None:
        return rules  # no filter — current behavior preserved
    active_set = set(active_names)
    return [(row, body) for row, body in rules if row.name in active_set]
```

Call order in `evaluate()` becomes:
1. `_load_rules(event)` — load enabled rules for this event type
2. `_apply_overrides(rules, overrides)` — session-specific enable/disable
3. `_filter_by_agent_scope(rules, agent_type)` — existing agent_scope filtering
4. **NEW**: `_filter_by_active_rules(rules, variables)` — selector-based filtering

### Step 7: mode:self Persona Path (no workflow required)

**File**: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`

Change line 146-147: allow `mode: self` without a workflow when agent_body exists.

**File**: `src/gobby/mcp_proxy/tools/spawn_agent/_modes.py`

Add `_handle_self_persona()`:
- Resolves rule selectors → writes `_active_rule_names` to session variables
- Writes `_agent_type` and preset variables
- Returns `{"success": True, "mode": "self", "persona_applied": agent_name}`

### Step 8: Spawn Factory Uses Resolver

**File**: `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`

Replace `_load_agent_body(name)` with `resolve_agent(name, db, cli_source)` from the new `agent_resolver` module. Pass `cli_source` from the parent session when available.

### Step 9: Update Default Agent Template

**File**: `src/gobby/install/shared/agents/default.yaml`

```yaml
name: default
description: Baseline agent — loads all gobby-tagged rules, inherits provider from CLI
mode: self
provider: inherit
workflows:
  rule_selectors:
    include:
      - "tag:gobby"
  variables: {}
```

Agent sync already handled by existing `src/gobby/agents/sync.py` — no new sync code needed.

---

### Step 10: Variable Definitions Model + Templates (#9094)

**File**: `src/gobby/workflows/definitions.py`

Add `VariableDefinitionBody`:
```python
class VariableDefinitionBody(BaseModel):
    variable: str           # variable name
    value: Any              # default value
    description: str | None = None
```

Stored as `workflow_type='variable'` in `workflow_definitions`. No schema change needed.

**New dir**: `src/gobby/install/shared/variables/` — YAML files for the 14 session-defaults variables:
`chat_mode`, `enforce_tool_schema_check`, `listed_servers`, `max_stop_attempts`, `mode_level`, `pre_existing_errors_triaged`, `require_commit_before_close`, `require_task_before_edit`, `require_uv`, `servers_listed`, `stop_attempts`, `task_claimed`, `task_ref`, `unlocked_tools`

**File**: `src/gobby/workflows/sync.py` — Add `sync_bundled_variables()` following `sync_bundled_rules` pattern.

### Step 11: Variable Loading at Session Start + Deprecate init-* Rules (#9095)

**File**: `src/gobby/hooks/event_handlers/_session.py`

Add `_load_variable_defaults(session_id)` — called after `_activate_default_agent()`. Loads enabled variable definitions, writes to session_variables if not already set. Uses selector system if agent has `variable_selectors`, else loads all enabled.

Disable the 14 init-* session-defaults rules (mark `enabled: false` in installed copies). The 6 conditional set_variable rules in other groups stay as rules — they have real conditions (progressive-disclosure resets, memory-lifecycle, plan-mode, context-handoff).

### Step 12: Documentation

- **Delete** `docs/guides/agents.md` — `agents.md` is a reserved filename for Codex
- **Create** `docs/guides/agent_definitions.md` — comprehensive guide covering:
  - Agent definition schema (all fields, defaults, sentinels)
  - `inherit` sentinel for provider, model, base_branch
  - `mode: self` (persona) vs `mode: terminal/headless/embedded` (process)
  - Selectors: syntax, dimensions (tag/group/name/source/*), include/exclude
  - `extends`: inheritance, merge behavior, cycle limits
  - Variable definitions: schema, YAML format, how they replace init-* rules
  - `default_agent` config: how baseline activation works at session_start
  - Examples: baseline agent, derived worker, derived reviewer with rule overrides

---

## Implementation Order

```
Steps 1-4 have no inter-dependencies, then sequential:
  1. definitions.py          — model changes (AgentSelector, extends, mode:self, inherit, VariableDefinitionBody)
  2. selectors.py            — NEW: selector parsing + rule filtering
  3. agent_resolver.py       — NEW: extends chain resolution + merge + inherit
  4. config/app.py           — add default_agent field
  5. rule_engine.py          — add _filter_by_active_rules (depends on 1)
  6. _factory.py             — use resolve_agent (depends on 3)
  7. _implementation.py      — allow mode:self without workflow (depends on 1)
  8. _modes.py               — add _handle_self_persona (depends on 2, 3)
  9. _session.py             — deep load default agent at session_start (depends on 2, 3, 4)
 10. default.yaml            — update template content
 11. Variable YAML templates — src/gobby/install/shared/variables/
 12. sync.py                 — add sync_bundled_variables
 13. _session.py             — add _load_variable_defaults, deprecate init-* rules
 14. Documentation            — delete docs/guides/agents.md, write docs/guides/agent_definitions.md
```

---

## Files Changed Summary

| File | Type |
|------|------|
| `src/gobby/workflows/definitions.py` | Modify |
| `src/gobby/workflows/selectors.py` | **New** |
| `src/gobby/workflows/agent_resolver.py` | **New** |
| `src/gobby/config/app.py` | Modify |
| `src/gobby/workflows/rule_engine.py` | Modify |
| `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py` | Modify |
| `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` | Modify |
| `src/gobby/mcp_proxy/tools/spawn_agent/_modes.py` | Modify |
| `src/gobby/hooks/event_handlers/_session.py` | Modify |
| `src/gobby/install/shared/agents/default.yaml` | Modify |
| `src/gobby/install/shared/variables/*.yaml` | **New** (14 files) |
| `src/gobby/workflows/sync.py` | Modify |
| `docs/guides/agents.md` | **Delete** (`agents.md` is reserved by Codex) |
| `docs/guides/agent_definitions.md` | **New** (replaces agents.md) |

---

## Existing Code to Reuse

- `src/gobby/agents/sync.py` — `sync_bundled_agents()` already syncs agent YAML templates; no new agent sync code needed
- `src/gobby/storage/workflow_definitions.py` — `LocalWorkflowDefinitionManager.list_all(workflow_type=...)` for loading rules/variables
- `src/gobby/workflows/hooks.py:64-117` — `_evaluate_rules` pattern for loading session vars + passing to rule engine
- `src/gobby/hooks/event_enrichment.py` — `EventEnricher.enrich()` for the deep-load metadata pattern
- `src/gobby/storage/session_variables.py` — `SessionVariableManager.merge_variables()` for writing preset vars

---

## Verification

### Unit Tests
```bash
uv run pytest tests/workflows/test_selectors.py -v          # selector parsing + matching
uv run pytest tests/workflows/test_agent_resolver.py -v      # extends chain, merge, inherit, cycles
uv run pytest tests/workflows/test_rule_engine.py -v -k active_rules  # _filter_by_active_rules
```

### Integration Test
```bash
uv run pytest tests/hooks/test_session_default_agent.py -v   # session_start deep load
```

### Manual E2E
1. Start daemon: `uv run gobby restart --verbose`
2. Start a Claude Code session — check logs for "Activated default agent: default"
3. Verify session variables contain `_agent_type=default` and `_active_rule_names=[...]`
4. Create a derived agent:
   ```yaml
   name: fast-worker
   extends: default
   mode: terminal
   rules:
     rule_selectors:
       exclude:
         - "group:stop-gates"
   variables:
     mode_level: 2
   ```
5. Spawn it: `spawn_agent(prompt="test", agent="fast-worker")` — verify it inherits default's rules minus stop-gates
