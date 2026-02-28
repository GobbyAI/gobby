# Agents v3: Selectors + Extends + Inherit + Skills

## Context

The current agent-to-rule binding doesn't scale. `AgentWorkflows.rules` is a flat list of rule names (50+ rules = 50 entries), and `agent_scope` on rules requires editing existing rules for every new agent. There's no inheritance between agents, no way to say "use the host CLI's provider/model", `mode: self` isn't in the definition model despite existing in the spawn layer, and skills are disconnected from agents entirely (`SkillProfile` exists in `src/gobby/skills/injector.py` but nobody wires it into the agent definition or session flow).

**Goal**: Every session activates a baseline agent (`mode: self`, `provider: inherit`) that loads rules via selectors (by tag, group, name, source) and has all skills available by default. Derived agents use `extends` to inherit and override the baseline's rule/variable/skill set.

---

## Task Breakdown (Epic #9085)

| # | Title | Depends On |
|---|-------|-----------|
| 1 | Data model changes (AgentSelector, extends, mode:self, inherit, skill_selectors, VariableDefinitionBody) | — |
| 2 | Selector resolution engine (`selectors.py`) — rules + skills | 1 |
| 3 | Agent resolver with extends chain (`agent_resolver.py`) | 1, 2 |
| 4 | Rule engine `_filter_by_active_rules` | 1 |
| 5 | mode:self persona path (no workflow required) | 1, 2, 3 |
| 6 | Spawn factory uses resolver | 3 |
| 7 | Deep load default agent at session start + config (rules + skills) | 2, 3, 4 |
| 8 | Skill filtering at serve time (list_skills, inject_context, system prompt) | 7 |
| 9 | Update default.yaml template | 1 |
| 10 | Delete SkillProfile + format resolution | 1 |
| 11 | Create new `agents` skill (messaging, commands, dos/don'ts) | — |
| 12 | Variable YAML templates + sync | 1 |
| 13 | Variable loading at session start + deprecate init-* rules | 7, 12 |
| 14 | Documentation: delete `agents.md`, write `agent_definitions.md` | all |

---

## Phase 1: Selectors + Extends + Inherit + mode:self + Skills + default_agent + Variables

### Step 1: Data Model Changes

**File**: `src/gobby/workflows/definitions.py`

Add `AgentSelector` model:
```python
class AgentSelector(BaseModel):
    include: list[str] = Field(default_factory=lambda: ["*"])
    exclude: list[str] = Field(default_factory=list)
```

Selector string format: `tag:X`, `group:X`, `name:X`, `source:X`, `category:X`, `*`. **Bare strings (no `:` prefix) default to name matching.** Prefixes only needed for non-name dimensions. Fnmatch globs within each. Multiple includes are OR'd. Exclude wins on conflict. This applies uniformly to all selector types.

Modify `AgentWorkflows`:
- Add `rule_selectors: AgentSelector | None = None` (new selector-based activation)
- Keep existing `rules: list[str]` (explicit names merged with selector results)
- Add `variable_selectors: AgentSelector | None = None` (variable filtering — null = all enabled session defaults loaded)
- Add `skill_selectors: AgentSelector | None = None` (skill filtering — null = all skills available)
- Add `skill_format: str | None = None` (default injection format for agent's skills — e.g., `"full"` for workers. NOTE: Explicit skill `injectionFormat` in SKILL.md takes precedence over this agent default)

Modify `AgentDefinitionBody`:
- `provider: str = "inherit"` (changed from `"claude"`)
- `model: str | None = None` (unchanged — None already means inherit)
- `mode: Literal["terminal", "embedded", "headless", "self"] = "self"` (added `"self"`, changed default)
- Add `extends: str | None = None` (parent agent name)
- Change `base_branch` default from `"main"` to `"inherit"` — sentinel meaning "auto-detect current branch at spawn time". Consistent with `provider: inherit` and `model: inherit`. Only relevant when `isolation` is `worktree`/`clone`; ignored for `mode: self` and `isolation: none`. Resolve in `_implementation.py` where auto-detect already exists (line 198-200).

### Step 2: Selector Resolution Engine — NEW FILE

**File**: `src/gobby/workflows/selectors.py`

Four functions:
- `parse_selector(s: str) -> tuple[str, str]` — splits `"tag:gobby"` into `("tag", "gobby")`. Uses `str.partition(":")` and checks against known prefixes (`tag`, `group`, `name`, `source`, `category`). If no known prefix, defaults to `("name", s)` (e.g., `feature:messaging` falls back to name match).
- `resolve_rules_for_agent(agent: AgentDefinitionBody, all_rules: list[WorkflowDefinitionRow]) -> set[str]`
  - Gathers explicit `workflows.rules` (merged parent + child)
  - Gathers selector `include` matches
  - Takes the union of explicit rules and include matches
  - Subtracts `exclude` matches from the entire combined set
  - Returns set of rule names
- `resolve_skills_for_agent(agent: AgentDefinitionBody, all_skills: list[Skill]) -> set[str]`
  - Uses `workflows.skill_selectors` matches
  - Returns set of skill names (or None if `skill_selectors` is null — meaning no filtering)
- `resolve_variables_for_agent(agent: AgentDefinitionBody, all_variables: list[WorkflowDefinitionRow]) -> set[str]`
  - Uses `workflows.variable_selectors` matches
  - Returns set of variable definition names (or None if `variable_selectors` is null — meaning no filtering)

Rule selector matching per dimension:
- `tag:X` → `any(fnmatch(t, X) for t in row.tags)`
- `group:X` → `fnmatch(json_extract(definition_json, '$.group'), X)`
- `name:X` or bare string → `fnmatch(row.name, X)`
- `source:X` → `fnmatch(row.source, X)`
- `*` → always True

Skill selector matching per dimension:
- `tag:X` → skill tags (from `metadata.skillport.tags` or `metadata.gobby.tags`)
- `category:X` → skill category
- `name:X` or bare string → `fnmatch(skill.name, X)`
- `source:X` → `fnmatch(skill.source_type, X)`
- `*` → always True

Include is OR (any match = included). Then exclude is subtracted (any match = excluded).

### Step 3: Agent Resolver with Extends Chain — NEW FILE

**File**: `src/gobby/workflows/agent_resolver.py`

- `resolve_agent(name: str, db: DatabaseProtocol, cli_source: str | None = None) -> AgentDefinitionBody | None`
  - Follows `extends` chain up to `MAX_EXTENDS_DEPTH = 5`
  - Cycle detection via `seen: set[str]`. Any cycle or depth breach raises `AgentResolutionError`.
  - Merges from root ancestor → leaf (child overrides parent)
  - Resolves `"inherit"` provider from `cli_source`

Merge logic (`_merge_agent_bodies`):
- Use Pydantic `model_fields_set` to detect which fields the child explicitly set
- Child's explicitly-set fields override parent
- `workflows.rules`: concatenate + deduplicate
- `workflows.rule_selectors`: child wins if set, else parent's
- `workflows.skill_selectors`: child wins if set, else parent's
- `workflows.variable_selectors`: child wins if set, else parent's
- `workflows.skill_format`: child wins if set, else parent's
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
4. If `agent_body.workflows.skill_selectors` is set, calls `resolve_skills_for_agent(agent_body, all_enabled_skills)` to get active skill names
5. Writes to `session_variables` via `SessionVariableManager.merge_variables()`:
   - `_agent_type = agent_body.name`
   - `_active_rule_names = [resolved rule names]`
   - `_active_skill_names = [resolved skill names]` (only if `skill_selectors` is set — null means no filter)
   - `_skill_format = agent_body.workflows.skill_format` (only if set)
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

### Step 7: Skill Filtering at Serve Time

**File**: `src/gobby/mcp_proxy/tools/skills/__init__.py`

In `list_skills()`: if `_active_skill_names` session variable is set, filter results to that set. `get_skill()` remains unfiltered (escape hatch for explicit lookups). `search_skills()` filters to the active set.

**File**: `src/gobby/workflows/context_actions.py`

In `inject_context` with `source="skills"`: filter to `_active_skill_names` before audience matching. This ensures always-apply skills outside the agent's set don't inject.

**File**: `src/gobby/hooks/event_handlers/_agent.py`

System prompt skill listing (the `Available Skills` section in system-reminder): filter by `_active_skill_names` if set.

**Design rationale**: When `skill_selectors` is null (the default agent case), `_active_skill_names` is not written to session variables, and no filtering occurs — all enabled skills are available. Rules are enforcement (explicit selectors), skills are informational (permissive by default). Only derived agents that need lean context set `skill_selectors`.

### Step 8: mode:self Persona Path (no workflow required)

**File**: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`

Change line 146-147: allow `mode: self` without a workflow when agent_body exists.

**File**: `src/gobby/mcp_proxy/tools/spawn_agent/_modes.py`

Add `_handle_self_persona()`:
- Resolves rule selectors → writes `_active_rule_names` to session variables
- If `skill_selectors` is set, resolves skill selectors → writes `_active_skill_names` + `_skill_format`
- Writes `_agent_type` and preset variables
- Returns `{"success": True, "mode": "self", "persona_applied": agent_name}`

### Step 9: Spawn Factory Uses Resolver

**File**: `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`

Replace `_load_agent_body(name)` with `resolve_agent(name, db, cli_source)` from the new `agent_resolver` module. Pass `cli_source` from the parent session when available.

### Step 10: Update Default Agent Template

**File**: `src/gobby/install/shared/agents/default.yaml`

```yaml
name: default
description: Baseline agent — loads all gobby-tagged rules, all skills available, inherits provider from CLI
mode: self
provider: inherit
workflows:
  rule_selectors:
    include:
      - "tag:gobby"
  # skill_selectors intentionally omitted (null) — all enabled skills available.
  # variable_selectors intentionally omitted (null) — all enabled session defaults apply.
  # Rules are enforcement (explicit), skills/variables are permissive by default.
  # Derived agents narrow skills via skill_selectors when they need lean context.
  variables: {}
```

Agent sync already handled by existing `src/gobby/agents/sync.py` — no new sync code needed.

### Step 11: Delete SkillProfile + Update Format Resolution

Delete `SkillProfile` dataclass from `src/gobby/skills/injector.py` (lines 98-119). Remove `skill_profile` dict field from `src/gobby/servers/routes/agents.py`. Remove `_skill_profile` references from `src/gobby/agents/runner.py`. Clean up any imports. `skill_selectors` + `skill_format` on `AgentWorkflows` fully replace this.

When updating the skill injector, invert the format resolution priority so `format_overrides` > `injectionFormat` (skill author) > `skill_format` (agent default), allowing explicit skill formats (like `full` for the agents skill) to override an agent's default `summary` format.

### Step 12: Create New `agents` Skill

**File**: `src/gobby/install/shared/skills/agents/SKILL.md` (NEW — replaces empty deprecated stub)

A skill that teaches spawned agents how to use the gobby-agents system. Content covers:

- **Server clarity**: All agent tools live on `gobby-agents`, NOT `gobby-sessions`. `gobby-sessions` is for session lifecycle (CRUD, handoffs). `gobby-agents` is for spawning, messaging, and commands.
- **Messaging tools** (all on `gobby-agents`):
  - `send_message(from_session, to_session, content)` — P2P between any sessions in the same project
  - `deliver_pending_messages(session_id)` — fetch unread messages
  - `send_command(from_session, to_session, command_text)` — ancestor → descendant structured command
  - `complete_command(session_id, command_id, result)` — descendant completes command, sends result back
  - `activate_command(session_id, command_id)` — activate pending command, set session variables
- **Usage patterns**: report to parent on completion, poll for messages, command lifecycle
- **Dos and don'ts**:
  - DO use your Gobby Session ID (shown as `Gobby Session ID: #N`), not the CLI external_id
  - DO send completion messages to parent before shutting down
  - DO poll for messages periodically during long-running work
  - DON'T look for messaging tools on `gobby-sessions`
  - DON'T send commands to non-descendant sessions (validation will reject)
  - DON'T send a new command to a session with an active command

Frontmatter:
```yaml
name: agents
description: "Inter-agent messaging and command coordination on gobby-agents"
category: core
alwaysApply: false
injectionFormat: full
triggers: agent, spawn, message, send, parent, child, command
metadata:
  gobby:
    audience: autonomous
    depth: "1-3"
    tags: [gobby, agents, messaging]
```

`audience: autonomous` + `depth: "1-3"` means this skill only injects for spawned agents (not interactive sessions at depth 0). `injectionFormat: full` ensures the content is fully available since spawned agents can't invoke slash commands.

### Step 13: Variable Definitions Model + Templates

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

### Step 14: Variable Loading at Session Start + Deprecate init-* Rules

**File**: `src/gobby/hooks/event_handlers/_session.py`

Add `_load_variable_defaults(session_id)` — called after `_activate_default_agent()`. Loads enabled variable definitions. If the resolved agent has `variable_selectors`, it uses `resolve_variables_for_agent` to filter the enabled set; otherwise (null), it loads all enabled definitions. It writes the filtered set to `session_variables` if they are not already set.

Disable the 14 init-* session-defaults rules (mark `enabled: false` in installed copies). The 6 conditional set_variable rules in other groups stay as rules — they have real conditions (progressive-disclosure resets, memory-lifecycle, plan-mode, context-handoff).

### Step 15: Documentation

- **Delete** `docs/guides/agents.md` — `agents.md` is a reserved filename for Codex
- **Create** `docs/guides/agent_definitions.md` — comprehensive guide covering:
  - Agent definition schema (all fields, defaults, sentinels)
  - `inherit` sentinel for provider, model, base_branch
  - `mode: self` (persona) vs `mode: terminal/headless/embedded` (process)
  - Selectors: syntax, dimensions (tag/group/name/source/category/*), bare string = name match
  - `extends`: inheritance, merge behavior, cycle limits
  - Skill selectors: how agents control skill availability, `skill_format`, null = permissive
  - Variable definitions: schema, YAML format, how they replace init-* rules
  - `default_agent` config: how baseline activation works at session_start
  - Examples: baseline agent, derived worker with skill narrowing, derived reviewer with rule overrides

---

## Example: Worker with Narrowed Skills

```yaml
name: task-worker
extends: default
mode: terminal
workflows:
  skill_selectors:
    include:
      - tasks
      - committing-changes
      - memory
      - bug
      - feat
      - chore
      - ref
  skill_format: full
```

Child's `skill_selectors` overrides parent's permissive default (null = all) with an explicit include list. Bare strings match by name. Child's `skill_format: full` ensures all skills inject their full content (workers can't invoke slash commands).

---

## Implementation Order

```
Steps 1-4 have no inter-dependencies, then sequential:
  1. definitions.py          — model changes (AgentSelector, extends, mode:self, inherit, skill_selectors, variable_selectors, skill_format, VariableDefinitionBody)
  2. selectors.py            — NEW: selector parsing + rule/skill/variable resolution
  3. agent_resolver.py       — NEW: extends chain resolution + merge + inherit
  4. config/app.py           — add default_agent field
  5. rule_engine.py          — add _filter_by_active_rules (depends on 1)
  6. _factory.py             — use resolve_agent (depends on 3)
  7. _implementation.py      — allow mode:self without workflow (depends on 1)
  8. _modes.py               — add _handle_self_persona with rules + skills (depends on 2, 3)
  9. _session.py             — deep load default agent at session_start, rules + skills (depends on 2, 3, 4)
 10. skills/__init__.py      — filter list_skills/search_skills by _active_skill_names (depends on 9)
 11. context_actions.py      — filter inject_context skills by _active_skill_names (depends on 9)
 12. _agent.py               — filter system prompt skill listing (depends on 9)
 13. default.yaml            — update template content
 14. injector.py + routes    — delete SkillProfile, skill_profile field, _skill_profile refs
 15. agents/SKILL.md         — NEW: agents skill (messaging, commands, dos/don'ts)
 16. Variable YAML templates — src/gobby/install/shared/variables/
 17. sync.py                 — add sync_bundled_variables
 18. _session.py             — add _load_variable_defaults, deprecate init-* rules
 19. Documentation           — delete docs/guides/agents.md, write docs/guides/agent_definitions.md
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
| `src/gobby/mcp_proxy/tools/skills/__init__.py` | Modify |
| `src/gobby/workflows/context_actions.py` | Modify |
| `src/gobby/hooks/event_handlers/_agent.py` | Modify |
| `src/gobby/install/shared/agents/default.yaml` | Modify |
| `src/gobby/skills/injector.py` | Modify (delete `SkillProfile`) |
| `src/gobby/servers/routes/agents.py` | Modify (delete `skill_profile` field) |
| `src/gobby/agents/runner.py` | Modify (remove `_skill_profile` refs) |
| `src/gobby/install/shared/skills/agents/SKILL.md` | **New** (agents skill for spawned agents) |
| `src/gobby/install/shared/variables/*.yaml` | **New** (14 files) |
| `src/gobby/workflows/sync.py` | Modify |
| `docs/guides/agents.md` | **Delete** |
| `docs/guides/agent_definitions.md` | **New** |

---

## Existing Code to Reuse

- `src/gobby/agents/sync.py` — `sync_bundled_agents()` already syncs agent YAML templates; no new agent sync code needed
- `src/gobby/storage/workflow_definitions.py` — `LocalWorkflowDefinitionManager.list_all(workflow_type=...)` for loading rules/variables
- `src/gobby/workflows/hooks.py:64-117` — `_evaluate_rules` pattern for loading session vars + passing to rule engine
- `src/gobby/hooks/event_enrichment.py` — `EventEnricher.enrich()` for the deep-load metadata pattern
- `src/gobby/storage/session_variables.py` — `SessionVariableManager.merge_variables()` for writing preset vars
- `src/gobby/skills/injector.py` — `SkillInjector.select_skills()` for audience-aware matching (keep; only delete `SkillProfile`)
- `src/gobby/storage/skills.py` — `LocalSkillManager` for loading enabled skills from DB

---

## Verification

### Unit Tests
```bash
uv run pytest tests/workflows/test_selectors.py -v          # selector parsing + matching (rules + skills + variables)
uv run pytest tests/workflows/test_agent_resolver.py -v      # extends chain, merge, inherit, cycles, skill/variable merge
uv run pytest tests/workflows/test_rule_engine.py -v -k active_rules  # _filter_by_active_rules
```

### Skill Filtering Tests
```bash
uv run pytest tests/mcp_proxy/tools/test_skills.py -v -k active      # list_skills respects _active_skill_names
uv run pytest tests/workflows/test_context_actions.py -v -k skill     # inject_context respects _active_skill_names
```

### Integration Test
```bash
uv run pytest tests/hooks/test_session_default_agent.py -v   # session_start deep load (rules + skills)
```

### Manual E2E
1. Start daemon: `uv run gobby restart --verbose`
2. Start a Claude Code session — check logs for "Activated default agent: default"
3. Verify session variables contain `_agent_type=default` and `_active_rule_names=[...]`
4. Verify `_active_skill_names` is NOT set (default agent has null skill_selectors = all available)
5. Verify `list_skills()` returns all enabled skills
6. Create a derived worker agent:
   ```yaml
   name: task-worker
   extends: default
   mode: terminal
   workflows:
     skill_selectors:
       include:
         - tasks
         - memory
     skill_format: full
   ```
7. Spawn it: `spawn_agent(prompt="test", agent="task-worker")`
8. Verify child session has `_active_skill_names=["tasks", "memory"]` and `_skill_format="full"`
9. Verify `list_skills()` in child session returns only tasks + memory
