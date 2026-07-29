# Split Workflow Definition Storage by Domain

**Plan ID:** split-workflow-definition-storage

## Overview
`kind: framing`

Epic #18879. Replace the overloaded `workflow_definitions` table (discriminator
`workflow_type`) and the generic `workflow_instances` runtime table with five
independent domain tables — `rule_definitions`, `agent_definitions`,
`agent_step_workflows` (optional 1:1 child of agent_definitions),
`session_variable_defaults`, `pipeline_definitions` — plus an agent-step-specific
runtime table `agent_step_instances` that stores an **immutable step-workflow
snapshot** taken at spawn/persona activation. No new discriminator, no generic
registry, no dual-read/dual-write facades, no backward compatibility (pre-0.5).

Why: today five kinds share one table keyed by `UNIQUE NULLS NOT DISTINCT
(name, project_id, source)` with a `workflow_type` column that is not part of
the key. Step workflows are materialized as generated `${agent}-steps` rows
(`workflow_type='workflow'`, `source='agent'`) via raw SQL
(`src/gobby/agents/step_workflow.py`) that bypasses validation. Runtime
enforcement resolves instance→definition **by name at read time** with no
project/type filter, so concurrent spawns share one mutable global row: a
definition edit mid-run makes `current_step` vanish and step enforcement
silently disappears. HTTP/MCP agent edits don't refresh the generated row.
Project-scoped step agents don't work. The single process-local revision
counter invalidates unrelated caches. The split fixes all of these
structurally.

## Epic Review Notes
`kind: framing`

Research corrections to the epic text (verified against code and the live hub):

1. The claimed `(name, workflow_type, project_id)` uniqueness does not exist;
   the real key is `UNIQUE NULLS NOT DISTINCT (name, project_id, source)` with
   no partial indexes (`postgres_baseline_schema.sql:1420-1449`).
2. A fifth discriminator value `workflow_type='workflow'` exists (29 generated
   `-steps` rows, 4 orphaned) outside `SUPPORTED_WORKFLOW_DEFINITION_TYPES`.
3. The epic's MCP tool names (`list_workflow_definitions`,
   `create_workflow_definition`) are Python function names; the actual tools
   are `create_workflow`, `update_workflow`, `delete_workflow`,
   `export_workflow`, `restore_workflow`, `list_workflows`, `get_workflow`,
   `get_workflow_status`, `import_workflow`, `reload_cache`.
4. No `/api/pipelines` definition routes and no `/api/variables` routes exist —
   both UI features drive the generic `/api/workflows`; the target APIs must be
   **built**, not migrated.
5. Live-data anomalies the migration must handle: one `source='gobby'` variable
   row (`_agent_context_injected`), 4 orphan `-steps` definition rows, orphan
   instances on dead sessions (`session-lifecycle`, `auto-task`,
   `developer-steps`), and soft-deleted agent rows for retired bundled agents
   whose generated `-steps` row and instances outlive them. Retiring a bundled
   agent leaves exactly this residue, so treat these as a standing class rather
   than a fixed row list.
6. 25 stepful / 4 step-less bundled agents: confirmed exact. The DB row count
   can exceed the bundled file count whenever a retired agent is still present
   as a soft-deleted row, so derive migration counts from the hub, never from
   the bundled file inventory.
7. `docs/plans/workflow-refactor.md` is a conflicting older design (generic
   `definition_registry` + inline step workflows) — superseded and deleted by
   this plan. `docs/reviews/cli-build-ops.md:56-60` falsely claims the new
   tables already exist — corrected in P7.
8. The epic's referenced test paths (`tests/workflows/test_rule_engine.py`,
`tests/workflows/test_session_defaults.py`) both exist and are extended or
retargeted in P7; only the new legacy-reference absence audit is written from
scratch there.

Decisions made with the operator (2026-07-26):

- **Staged per-domain migrations** (not the epic's single copy+drop
  transaction): a create-tables migration first (baseline keeps BOTH shapes
  during the epic); each domain ships a guarded copy migration in the same
  commit as its code cutover; a final drop migration removes the legacy
  tables and the baseline entries. The drop migration RAISEs if live legacy
  rows exist that never reached the typed tables (backstop for mid-epic
  writes through old surfaces).
- **One step instance per session**: `UNIQUE(session_id)`, no `priority`
  column. Persona activation with a different agent replaces the instance;
  the same agent preserves it.
- **`workflow_states` legacy table removal is IN scope** (P7): rewrite its one
  reader (`cli/tasks/_utils/claims.py`) against the session-variables store,
  drop the table in the drop migration.

## Constraints
`kind: framing`

- Pre-0.5: clean API/YAML break. No compatibility shims, no dual-write. The
  single sanctioned scaffolding: `register_agent_step_workflow` keeps writing
  generated legacy rows (reading `body.step_workflow`) from P2 until P3 task
  3.2 deletes it — this keeps every intermediate commit a working daemon.
- Migration ordinals: allocate the next free `NNN` under
  `src/gobby/storage/migrations/` at implementation time (tail is 342 at
  planning time). Relative order must hold: create-tables < all domain copies
  < drop. Every migration is guarded (`IF NOT EXISTS` DDL / DO-block
  `information_schema` checks) so it is valid against both the pre-migration
  shape and the current baseline (fresh installs replay all migrations on top
  of the baseline — see `tests/storage/test_migration_contract.py`).
- Copy migrations preserve row UUIDs and timestamps; unknown `source` values
  normalize to `'installed'`. **Conflict target**: every copy INSERT uses
  targetless `ON CONFLICT DO NOTHING`, never `ON CONFLICT (name, project_id)
  WHERE deleted_at IS NULL`. Copies deliberately include soft-deleted rows so a
  restore keeps its payload, and a soft-deleted row is invisible to the live
  partial index: on a rerun it collides on the **primary key** instead, which an
  index-targeted clause does not cover, so the statement aborts. The same
  asymmetry governs the guard's join — live rows match on the live natural key,
  soft-deleted rows match on the preserved `id`, because deleted natural keys
  are not unique and two retired rows can share a name.
- Each copy migration ends with an **equivalence guard**: join every legacy
  source row to its typed target by that rule and compare **both identity and
  payload** — `target.id = source.id`, then the payload after the migration's
  documented normalization and shape transformation — RAISEing with the
  offending source ids and names on mismatch. `ON CONFLICT DO NOTHING` plus a
  count check silently passes when a pre-existing typed row holds a divergent
  payload — the routine outcome of re-migrating a partially staged dev database
  — and the P7 drop backstop compares `(name, project_id)` only, so it cannot
  catch that divergence either. Identity is checked because it is a promise
  this plan makes and targetless `ON CONFLICT DO NOTHING` is exactly what hides
  its violation: a pre-existing live typed row with the same natural key and an
  identical normalized payload but a **different UUID** suppresses the insert
  and then satisfies a payload-only guard, so the copy is reported complete
  while `id` silently changed. Since `agent_step_workflows.agent_definition_id`
  and `agent_step_instances.agent_step_workflow_id` are FKs onto these
  preserved ids, that is a lineage redirect that survives all the way to P7
  reporting equivalence. The same `target.id = source.id` condition is part of
  the P7 drop backstop. Each domain's focused migration test covers six cases:
  first run, rerun over live rows, rerun over soft-deleted rows, two
  soft-deleted rows sharing a natural key, a divergent payload conflict (loud
  failure), and a live same-key/same-payload/**different-UUID** row (loud
  failure).
- 1,000-line rule: `workflows/definitions.py` (911), `state_manager.py` (774),
  `workflows/hooks.py` (965), `dry_run.py` (992),
  `spawn_agent/_implementation.py` (954), `mcp_proxy/tools/workflows/__init__.py`
  (818) are all near the cap — the named extractions in P2/P3/P5 are mandatory,
  not optional. Each extraction is scheduled in or before the first task that
  grows its file, never as a later cleanup: `definitions.py` splits in 2.1,
  `dry_run.py`'s trace helpers move in 2.4 (eight lines of headroom cannot absorb
  2.4's nested-agent rewrite), and `_implementation.py`'s step-state block moves
  in 3.2. A conditional "extract if it gets close" leaves an over-cap commit in
  between and is not used anywhere in this plan.
- Tests: focused runs only with `GOBBY_TEST_PROTECT=1`; never the full suite.
- Daemon must be restarted (`uv run gobby restart`) after each phase lands to
  run its migrations.

## P1: Storage Foundation
`kind: framing`

**Goal**: New tables exist (empty), typed managers and per-domain cache
revisions are available, nothing consumes them yet.

### 1.1 Create domain tables migration and baseline DDL [category: code]
`kind: deliverable`

Targets: `src/gobby/storage/postgres_baseline_schema.sql`,
`src/gobby/storage/migrations/NNN_create_domain_definition_tables.sql` (new),
`tests/storage/test_migration_contract.py`

Add six tables to the baseline (KEEP `workflow_definitions` and
`workflow_instances` in the baseline until P7) and an identical guarded
DDL-only migration (`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT
EXISTS` throughout):

```sql
CREATE TABLE rule_definitions (
    id UUID PRIMARY KEY,
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    name TEXT NOT NULL,
    description TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    priority INTEGER NOT NULL DEFAULT 100,
    sources JSONB,
    definition_json JSONB NOT NULL,
    source TEXT NOT NULL DEFAULT 'installed' CHECK (source IN ('installed','custom','project')),
    tags JSONB,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_rule_defs_project ON rule_definitions(project_id);
CREATE INDEX idx_rule_defs_event ON rule_definitions((definition_json->>'event')) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX uq_rule_defs_live_name
    ON rule_definitions (name, project_id) NULLS NOT DISTINCT WHERE deleted_at IS NULL;
```

`agent_definitions`: same shape minus `priority`/`sources` (body =
`AgentDefinitionBody` JSON **without** step fields). `pipeline_definitions`:
same shape minus `priority`/`sources`, plus `version TEXT NOT NULL DEFAULT
'1.0'` and `canvas_json JSONB`. `session_variable_defaults`: fully typed — no
JSON body: `id, project_id, name, description, enabled, default_value JSONB,
source, tags, deleted_at, created_at, updated_at` (same indexes/partial unique
pattern; `default_value` holds any JSON scalar/object). Each definition table
gets `idx_<t>_project`, and partial unique `uq_<t>_live_name (name, project_id)
NULLS NOT DISTINCT WHERE deleted_at IS NULL`.

```sql
CREATE TABLE agent_step_workflows (
    id UUID PRIMARY KEY,
    agent_definition_id UUID NOT NULL UNIQUE
        REFERENCES agent_definitions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    steps_json JSONB NOT NULL,
    variables_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    exit_condition TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE agent_step_instances (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL UNIQUE
        REFERENCES sessions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    agent_step_workflow_id UUID REFERENCES agent_step_workflows(id) ON DELETE SET NULL,
    agent_name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    current_step TEXT,
    step_entered_at TIMESTAMPTZ,
    step_action_count INTEGER NOT NULL DEFAULT 0,
    total_action_count INTEGER NOT NULL DEFAULT 0,
    variables JSONB NOT NULL DEFAULT '{}'::jsonb,
    context_injected BOOLEAN NOT NULL DEFAULT FALSE,
    snapshot_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_asi_step_workflow ON agent_step_instances(agent_step_workflow_id);
```

Extend `tests/storage/test_migration_contract.py` two ways. Fast snippet
assertions stay as diagnostics: the migration contains the six `CREATE TABLE IF
NOT EXISTS` statements and the partial unique indexes; the baseline contains
DDL for the six tables and still contains the legacy tables. The acceptance
proof is executable catalog equivalence — build the six tables once from the
baseline and once from the guarded migration into two isolated schemas in the
test database, then compare normalized `pg_catalog`/`information_schema`
metadata: column names, types, nullability and defaults; constraint definitions
including CHECK bodies; FK referenced columns, `ON DELETE` actions and
deferrability; and index definitions including partial predicates and `NULLS
NOT DISTINCT`. Text-identical DDL is the weaker claim: a drifted default, FK
action, or partial predicate passes every snippet check while splitting the
fresh-install and migrated lineages, and catalog comparison is what closes it.

**Acceptance:**

- 1.1.1 - Baseline contains the six new tables with partial unique live-name indexes and retains the legacy tables. file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 1.1.2 - Guarded DDL-only migration creates the six tables idempotently on both lineages. file: `src/gobby/storage/migrations/NNN_create_domain_definition_tables.sql`.
- 1.1.3 - Snippet diagnostics pin the six CREATE TABLE statements and the partial unique indexes in both files. test: `tests/storage/test_migration_contract.py`.
- 1.1.4 - Baseline-built and migration-built schemas are catalog-identical across columns, defaults, constraints, FK actions, deferrability, and index predicates. test: `tests/storage/test_migration_contract.py::test_domain_tables_catalog_equivalence`.

### 1.2 Typed definition managers package [category: code] (depends: 1.1, 1.4)
`kind: deliverable`

Targets: `src/gobby/storage/definitions/__init__.py` (new),
`src/gobby/storage/definitions/_shared.py` (new),
`src/gobby/storage/definitions/rules.py` (new),
`src/gobby/storage/definitions/variables.py` (new),
`src/gobby/storage/definitions/pipelines.py` (new),
`tests/storage/definitions/test_rules_manager.py` (new),
`tests/storage/definitions/test_variables_manager.py` (new),
`tests/storage/definitions/test_pipelines_manager.py` (new)

New package `gobby.storage.definitions`. Rows are dataclasses (match
`WorkflowDefinitionRow` convention); managers use `db.transaction()` with
psycopg `%s` placeholders. `_shared.py`: `DefinitionNameConflictError`,
`DefinitionNotFoundError`; `compute_definition_hash` moved verbatim from
`src/gobby/storage/workflow_definitions.py:29-40` (old module keeps importing
it from here until P7 deletion); scoped `get_by_name` helper (project-first,
global fallback, `include_deleted` flag); soft-delete/restore (restore
pre-checks live-name conflict and raises `DefinitionNameConflictError`);
`purge_deleted(older_than_days)`; `_move_scope` core (pre-checks target scope);
JSON tags/sources codecs.

Common manager surface (per-table SQL, shared helpers): `create`, `get`,
`get_by_name`, `update(**fields)` (per-domain allow-list; never `source`/
`project_id`), `toggle_enabled`, `delete`, `hard_delete`, `restore`,
`purge_deleted`, `list_all(project_id, enabled, include_deleted)`,
`move_to_project`, `move_to_global`, `duplicate(id, new_name)` (conflict
pre-check). Domain additions: `RuleDefinitionManager.list_by_event(event,
project_id, enabled)` and `list_by_group(group, ...)` using native
`definition_json->>'event'`, `ORDER BY priority, name` (ports of
`storage/workflow_definitions.py:380-438`);
`SessionVariableDefaultManager.get_defaults_map(project_id=None,
enabled_only=True) -> dict[str, Any]` reading typed `name`/`default_value`
columns (no `source` filter — see 4.2); no `duplicate` for variables.
`PipelineDefinitionManager.update` additionally allows `version`,
`canvas_json`. Every mutator bumps its domain revision (1.4).

1.2 depends on 1.4 rather than the reverse because the manager mutators call
`bump_definitions_revision` in their first commit. Ordering revisions after the
managers that use them means 1.2 and 1.3 either ship calls into a module that
does not exist or ship without the bump and get retrofitted later, and neither
is a working commit. 1.4 needs only the tables from 1.1, so it moves ahead
without acquiring a dependency of its own.

**Acceptance:**

- 1.2.1 - Shared scope/soft-delete/conflict utilities exist with typed errors. file: `src/gobby/storage/definitions/_shared.py`.
- 1.2.2 - Rule manager supports event/group listing with priority ordering. symbol: `RuleDefinitionManager`. file: `src/gobby/storage/definitions/rules.py`.
- 1.2.3 - Variable-defaults manager returns a typed defaults map. symbol: `SessionVariableDefaultManager`. file: `src/gobby/storage/definitions/variables.py`.
- 1.2.4 - Pipeline manager covers CRUD, duplicate, scope moves, canvas/version updates. symbol: `PipelineDefinitionManager`. file: `src/gobby/storage/definitions/pipelines.py`.
- 1.2.5 - CRUD, scope fallback, cross-domain same-name, same-domain live conflict, restore collision, and purge behaviors are covered for rules. test: `tests/storage/definitions/test_rules_manager.py`.
- 1.2.6 - The same behavior set is covered for variable defaults. test: `tests/storage/definitions/test_variables_manager.py`.
- 1.2.7 - The same behavior set is covered for pipelines, including duplicate and canvas/version updates. test: `tests/storage/definitions/test_pipelines_manager.py`.

### 1.3 Agent definition manager with step-workflow child [category: code] (depends: 1.2)
`kind: deliverable`

Targets: `src/gobby/storage/definitions/agents.py` (new),
`tests/storage/definitions/test_agents_manager.py` (new)

`AgentDefinitionRow` (exposes `step_workflow_id: str | None`),
`AgentStepWorkflowRow`, `AgentDefinitionManager`. **Hydration contract**:
`get`, `get_by_name`, `list_all` LEFT JOIN the child and return
`definition_json` with the nested `step_workflow` object merged in
(`{"variables":…, "exit_condition":…, "steps":…}`), so
`AgentDefinitionBody.model_validate` works everywhere with one query. Writes:

- `upsert_with_steps(name, body_json, step_workflow: dict | None, *, source,
  project_id, enabled, tags, description) -> AgentDefinitionRow`: one
  `db.transaction()` covering parent insert/update (body stored WITHOUT step
  fields) and child insert/update/delete (`step_workflow is None` ⇒ delete
  child). Bumps `agents` + `agent_step_workflows` revisions.
- `set_step_workflow(agent_definition_id, step_workflow: dict | None)`
  primitive for surfaces that already hold the parent.
- `get_step_workflow(agent_definition_id) -> AgentStepWorkflowRow | None`.

**Acceptance:**

- 1.3.1 - Reads hydrate the nested step_workflow from the child table in one query. symbol: `AgentDefinitionManager`. file: `src/gobby/storage/definitions/agents.py`.
- 1.3.2 - `upsert_with_steps` writes parent and child atomically, deleting the child when steps are removed. test: `tests/storage/definitions/test_agents_manager.py::test_upsert_with_steps_atomic`.
- 1.3.3 - Child cascade on parent hard-delete and orphan-free child lifecycle are covered. test: `tests/storage/definitions/test_agents_manager.py`.

### 1.4 Domain cache revisions with listener registry [category: code] (depends: 1.1)
`kind: deliverable`

Targets: `src/gobby/storage/definitions/revisions.py` (new),
`tests/storage/definitions/test_revisions.py` (new)

```python
DefinitionDomain = Literal["rules", "agents", "agent_step_workflows", "variables", "pipelines"]
def get_definitions_revision(domain: DefinitionDomain) -> int: ...
def bump_definitions_revision(*domains: DefinitionDomain) -> None: ...
def register_revision_listener(domain: DefinitionDomain, cb: Callable[[], None]) -> None: ...
```

Thread-safe per-domain counters (process-local, same semantics as today's
`_WORKFLOW_DEFINITIONS_REVISION`). This lands before 1.2 so the typed managers
can call `bump_definitions_revision` in the same commit that introduces them.
The listener registry replaces the current
storage→hooks import cycle (`storage/workflow_definitions.py:49-56` importing
`clear_active_rule_names_cache`). Reader wiring lands with each consumer phase
(engine agent cache in 2.4, active-rule-names listener in 4.1, variables TTL
invalidation in 4.2, pipeline loader in 4.3). Listener exceptions are caught
and logged, never propagated to the mutating caller.

**Acceptance:**

- 1.4.1 - Per-domain counters and listener registry exist with thread-safety. file: `src/gobby/storage/definitions/revisions.py`.
- 1.4.2 - Bumping one domain fires only that domain's listeners and leaves other domains' revisions unchanged. test: `tests/storage/definitions/test_revisions.py`.

## P2: Agent Definition Shape
`kind: framing`

**Goal**: `AgentDefinitionBody.step_workflow` replaces the top-level step
fields end-to-end; agent storage reads/writes the typed tables; generated-row
scaffolding remains only for the P3 runtime readers.

### 2.1 Model split and step_workflow field [category: code] (depends: P1)
`kind: deliverable`

Targets: `src/gobby/workflows/agent_models.py` (new),
`src/gobby/workflows/pipeline_models.py` (new),
`src/gobby/workflows/definitions.py`, `src/gobby/agents/step_workflow.py`,
`src/gobby/agents/sync.py`, `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`,
`src/gobby/cli/agents.py`, `src/gobby/dispatch/skill_composition.py`,
`src/gobby/dispatch/spawn.py`, `src/gobby/mcp_proxy/tools/apply_persona.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`,
`src/gobby/workflows/dry_run.py`, `tests/agents/test_discovery_agents.py`

`workflows/definitions.py` is 911 lines — split before growing. Move
`AgentSelector`, `AgentWorkflows`, `AgentDefinitionBody`
(`definitions.py:387-517`) to `agent_models.py`; move `WebhookEndpoint`,
`WebhookConfig`, `PipelineApproval`, `MCPStepConfig`, `PipelineStep`,
`PipelineDefinition` (`definitions.py:646-809`) to `pipeline_models.py`.
`definitions.py` keeps rule models, `WorkflowStep`, `WorkflowTransition`,
`WorkflowDefinition` (survives only as the internal dry-run step-program
shape), `validate_workflow_definition_data`, and permanently re-exports the
moved names (module organization, not a compat layer).

In `agent_models.py` add:

```python
class AgentStepWorkflowBody(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)
    exit_condition: str | None = None
    steps: list[WorkflowStep] = Field(min_length=1)
    def get_step(self, step_name: str) -> WorkflowStep | None: ...
```

`AgentDefinitionBody`: delete `steps`, `step_variables`, `exit_condition`
(currently `definitions.py:471-474`); add
`step_workflow: AgentStepWorkflowBody | None = None`. Keep `extra="ignore"` for
unrelated stale metadata, and add a `mode="before"` model validator that
**rejects** exactly those three removed top-level keys with an actionable
message naming `step_workflow.steps`, `step_workflow.variables`, and
`step_workflow.exit_condition`. Without it `extra="ignore"` discards a
hand-authored or imported step workflow and still reports success — a silent
data loss the audited bundled YAML does nothing to prevent. The validator lives
on the model, so every HTTP, MCP, sync, and import consumer inherits the
fail-loud behavior with no per-surface check and no compatibility layer.

Scaffolding (removed in 3.2): `register_agent_step_workflow`
(`agents/step_workflow.py`) switches to reading
`body.step_workflow.steps/variables/exit_condition` so generated legacy rows
stay fresh for the P2→P3 window; mark with a `# P3 scaffolding` comment.

**Direct-access inventory** (same commit — deleting model fields is not
complete until every reader compiles). Each site below reads `steps`,
`step_variables`, or `exit_condition` off an `AgentDefinitionBody` and is
rewritten to go through `body.step_workflow`, guarding for `None` on the four
step-less agents: `agents/step_workflow.py:27-29`, `agents/sync.py:91`,
`cli/agents.py:91-115`, `dispatch/skill_composition.py:79`,
`dispatch/spawn.py:150,248`, `apply_persona.py:95-120`,
`spawn_agent/_factory.py:412`, `spawn_agent/_implementation.py:103,141,152,873,907`,
`workflows/dry_run.py:268-274,612`, and `tests/agents/test_discovery_agents.py:77,112`.
Sites reading `.steps`/`.exit_condition` off a `WorkflowDefinition` or
`PipelineDefinition` — `cli/pipelines_catalog.py`, `cli/workflows/inspect.py`,
`hooks/session_coordinator.py:746-778`, `_pipeline_discovery.py`,
`_pipelines.py`, `_query.py`, `enforcement_completion.py`,
`handler_route_lint.py`, `step_context.py`, and the non-agent branches of
`workflows/dry_run.py` — are untouched, because those models keep their fields.
This task performs the mechanical rewrite only; 2.4 and 3.2 then change the
semantics of the storage and runtime sites among them.

**Test inventory** (same commit — the field removal is not complete while a
suite still reads the flat shape). These read `agent["step_variables"]` or
`agent["steps"]` as top-level keys off a loaded bundled definition and are
retargeted at the nested shape:
`tests/agents/test_doc_reviewer_definition.py:60,66`,
`tests/agents/test_epic_reviewer_definition.py:111,119`,
`tests/agents/test_qa_reviewer_definition.py:106,121,224`,
`tests/agents/test_tech_writer_definition.py:26`,
`tests/agents/test_merge_lifecycle.py:79-80`,
`tests/agents/test_merge_orchestrator_contract.py:248-249,280,379-380,428`,
and the raw step fixtures in `tests/agents/test_sync.py:174`,
`tests/agents/test_lifecycle_monitor.py:1674`, and
`tests/agents/test_lifecycle_monitor_extra.py:955`. These are exact-shape
assertions, so `extra="ignore"` will not save them: they fail loudly on the
nested body, which is the desired behavior and the reason they are owned here
rather than discovered in P7. `tests/agents/test_dry_run.py:241,282,346`
constructs `WorkflowStep`/`WorkflowDefinition` directly and is untouched —
those models keep their fields.

Raw-key readers are only one of three consumer classes; the other two break the
same commit and are inventoried here rather than left to compile errors.
**Direct constructors** passing a removed field:
`tests/dispatch/test_skill_composition.py:46` (`step_variables=`) and
`tests/mcp_proxy/tools/test_apply_persona.py:158,202,260` (`steps=`) — the
latter is also in 3.2 Targets, for the unrelated runtime rename, and both
concerns land in their own phase. **Removed-field assertions**:
`tests/workflows/test_agent_definitions_v2.py:67` asserts
`body.step_variables == {}` and `:115` asserts `"step_variables" in fields`;
these must move to the nested body and its field set, not merely be deleted, or
the model split loses its only direct field-inventory assertion. One further
raw reader: `tests/agents/test_plan_adversary_taskless_definition.py:27,32,42,72,106`
reads `agent["steps"]` off loaded bundled YAML.

**Acceptance:**

- 2.1.1 - AgentStepWorkflowBody exists and AgentDefinitionBody nests it, with old fields removed. symbol: `AgentStepWorkflowBody`. file: `src/gobby/workflows/agent_models.py`.
- 2.1.2 - Pipeline models live in their own module with definitions.py re-exports intact. file: `src/gobby/workflows/pipeline_models.py`.
- 2.1.3 - definitions.py is under 1,000 lines after the split. file: `src/gobby/workflows/definitions.py`.
- 2.1.4 - Scaffolded register_agent_step_workflow reads the nested shape. symbol: `register_agent_step_workflow`. file: `src/gobby/agents/step_workflow.py`.
- 2.1.5 - Model validation round-trips nested YAML (stepful and step-less). test: `tests/workflows/test_agent_models.py::test_step_workflow_nesting`.
- 2.1.6 - Validating a body that carries top-level steps, step_variables, or exit_condition raises with a message naming the nested replacement key. test: `tests/workflows/test_agent_models.py::test_legacy_step_keys_rejected`.
- 2.1.7 - Every direct-access site reads through step_workflow and handles the step-less case, across the CLI, dispatch, persona, and spawn readers. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`. file: `src/gobby/cli/agents.py`. file: `src/gobby/dispatch/spawn.py`.
- 2.1.8 - Agent-shape tests and mocks assert the nested shape with no residual top-level step fields, including direct AgentDefinitionBody constructors that passed removed fields and the field-inventory assertions, which move to the nested body rather than being deleted. test: `tests/agents/test_discovery_agents.py`. test: `tests/dispatch/test_skill_composition.py`. test: `tests/workflows/test_agent_definitions_v2.py`.
- 2.1.9 - The bundled-definition contract suites and raw step fixtures read the nested shape and none asserts a top-level steps or step_variables key. test: `tests/agents/test_qa_reviewer_definition.py`. test: `tests/agents/test_merge_orchestrator_contract.py`. test: `tests/agents/test_sync.py`. test: `tests/agents/test_plan_adversary_taskless_definition.py`.

### 2.2 Bundled agent YAML rewrite [category: config] (depends: 2.1)
`kind: deliverable`

Targets: `src/gobby/install/shared/workflows/agents/` (29 files, e.g.
`src/gobby/install/shared/workflows/agents/planner.yaml`),
`tests/dispatch/test_bundled_agent_contract.py`

Nest top-level `steps:` → `step_workflow.steps:`, `step_variables:` →
`step_workflow.variables:`, `exit_condition:` → `step_workflow.exit_condition:`
in the 25 stepful agents: analyst, architect, backend-developer, doc-reviewer,
epic-reviewer, expansion-qa, frontend-developer, fullstack-developer,
merge-orchestrator, merge-worker, nightly-linter, nightly-test-fixer,
plan-adversary, plan-adversary-taskless, plan-enhancer, plan-enhancer-taskless,
plan-review-researcher-taskless, planner, product-manager, qa-dev, qa-reviewer,
researcher, tech-writer, trajectory-monitor, wiki-researcher. Verify the 4
step-less agents (comms-agent, default, goal-taskmaster, triage-agent) carry no
stray legacy keys — with `extra="ignore"` a stray key would be silently
dropped. Bump each rewritten file's `version` so template drift detection
re-syncs.

Suites that parse these YAML files directly rather than through the model —
`tests/dispatch/test_bundled_agent_contract.py`, which walks
`data.get("steps")` per file, and the per-agent contract tests listed in 2.1's
test inventory — are retargeted in the same commit as the rewrite. A YAML-shape
change and the assertions about that shape cannot land in different commits
without leaving the suite red in between.

**Acceptance:**

- 2.2.1 - All 25 stepful agent YAMLs use the nested step_workflow shape and none of the 29 carries top-level steps/step_variables/exit_condition. file: `src/gobby/install/shared/workflows/agents/planner.yaml`.
- 2.2.2 - Every rewritten YAML still validates through AgentDefinitionBody with a populated step_workflow. test: `tests/agents/test_sync.py::test_bundled_agents_nested_step_workflow`.
- 2.2.3 - The bundled-agent contract suite reads steps from the nested key and passes against the rewritten YAML in the same commit. test: `tests/dispatch/test_bundled_agent_contract.py`.

### 2.3 Agent sync, write surfaces, and agent copy migration [category: code] (depends: 2.2)
`kind: deliverable`

Targets: `src/gobby/agents/sync.py`,
`src/gobby/mcp_proxy/tools/workflows/_agents.py`,
`src/gobby/servers/routes/agents.py`, `src/gobby/workflows/template_hashes.py`,
`src/gobby/storage/migrations/NNN_copy_agent_definitions.sql` (new),
`tests/storage/test_agent_copy_migration.py` (new)

- `sync_bundled_agents` (`agents/sync.py:95-274`): validate YAML →
  `AgentDefinitionManager.upsert_with_steps` (parent body stripped of step
  fields, child from `step_workflow`). KEEP the `_refresh_step_workflow` call
  (generated legacy row) as P3 scaffolding. Orphan cleanup moves to the typed
  table.
- MCP `update_agent_steps` (`_agents.py:375-419`) → rename
  `update_agent_step_workflow(name, step_workflow: dict | None)`: validate
  `AgentStepWorkflowBody`, call `set_step_workflow`; all `_agents.py` CRUD and
  `_agent_detail` (flat keys → one nested `step_workflow` key) move to
  `AgentDefinitionManager`.
- HTTP agent routes (`routes/agents.py`): list/get/create/PUT/delete/restore/
  import move to the typed manager; the PUT merge (`:446-454`) accepts the
  `step_workflow` key wholesale (validated) instead of three flat keys.
- `template_hashes.py::_load_agents` hashes the nested body shape.
- Copy migration (guarded by `information_schema` check for
  `workflow_definitions`; skips if absent): validate no duplicate live
  `(name, project_id)` among `workflow_type='agent'` rows; INSERT parents
  (body minus `'steps','step_variables','exit_condition'`, source normalized,
  UUID/timestamps preserved) with targetless `ON CONFLICT DO NOTHING` per the
  Constraints conflict-target rule (tolerates rows sync already created and
  reruns over soft-deleted rows); INSERT children from
  agent bodies where `jsonb_typeof(definition_json->'steps') = 'array' AND
  jsonb_array_length(definition_json->'steps') > 0` — the four step-less rows
  store `"steps": null`, and `jsonb_array_length` on a JSON scalar aborts the
  migration with `cannot get array length of a scalar`, so the type guard is
  required, not defensive — including soft-deleted parents so restore keeps
  steps; validate counts and run the Constraints equivalence guard across both
  parent bodies and child step rows; RAISE on mismatch. Generated
  `workflow_type='workflow'` rows are NOT copied.

Known dev-window caveat (accepted): agent reads still hit legacy until 2.4
lands; the P7 drop migration backstop catches any legacy-only stragglers.

**Acceptance:**

- 2.3.1 - Agent sync upserts parent and child in one transaction and no longer manages step data in the parent body. symbol: `sync_bundled_agents`. file: `src/gobby/agents/sync.py`.
- 2.3.2 - MCP agent CRUD operates on the typed manager with a nested step_workflow surface. symbol: `update_agent_step_workflow`. file: `src/gobby/mcp_proxy/tools/workflows/_agents.py`.
- 2.3.3 - HTTP agent definition routes read and write the typed tables. file: `src/gobby/servers/routes/agents.py`.
- 2.3.4 - Copy migration migrates every agent row and one child per row carrying a non-empty steps array (29 and 25 at planning time), preserves soft-deleted rows, skips the four `"steps": null` rows without a scalar-length error, and fails loudly on count mismatch. test: `tests/storage/test_agent_copy_migration.py`.
- 2.3.5 - Sync produces child workflows for all 25 stepful bundled agents, none for the 4 step-less, and leaves no stale child rows. test: `tests/agents/test_sync.py`.
- 2.3.6 - The equivalence guard succeeds idempotently on an identical pre-existing typed row and fails loudly on a divergent one. test: `tests/storage/test_agent_copy_migration.py`.
- 2.3.9 - Rerunning the copy over an already-migrated soft-deleted agent row completes without a primary-key abort, and two soft-deleted rows sharing a natural key each match their own target by preserved id. test: `tests/storage/test_agent_copy_migration.py::test_rerun_over_soft_deleted_rows`.
- 2.3.7 - A public agent write carrying legacy top-level step keys is rejected instead of silently dropping the step workflow. test: `tests/servers/routes/test_agents.py`.
- 2.3.8 - Template hashing reads the nested body shape, so a step_workflow edit registers as drift. symbol: `TemplateHashCache._load_agents`. file: `src/gobby/workflows/template_hashes.py`.

### 2.4 Agent read-consumer rewiring [category: code] (depends: 2.3)
`kind: deliverable`

Targets: `src/gobby/workflows/agent_resolver.py`,
`src/gobby/workflows/engine/core.py`, `src/gobby/dispatch/context.py`,
`src/gobby/tasks/expansion/_common.py`, `src/gobby/cli/agents.py`,
`src/gobby/agents/dry_run.py`, `src/gobby/workflows/dry_run.py`,
`src/gobby/dispatch/skill_composition.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`, `src/gobby/dispatch/spawn.py`,
`src/gobby/mcp_proxy/tools/apply_persona.py`,
`src/gobby/hooks/event_handlers/_session_start/agents.py`,
`src/gobby/hooks/event_handlers/_agent.py`, `src/gobby/build/observability.py`,
`src/gobby/servers/routes/agent_spawn.py`, `src/gobby/hooks/factory.py`,
`src/gobby/tasks/expansion_service.py`,
`src/gobby/tasks/expansion/_compile.py`,
`src/gobby/workflows/dry_run_trace.py` (new)

- `resolve_agent` (`agent_resolver.py:17-53`) →
  `AgentDefinitionManager.get_by_name` (hydrated). Add
  `resolve_agent_with_row(name, db, project_id, cli_source) ->
  tuple[AgentDefinitionBody, AgentDefinitionRow]` for the spawn path (3.2
  needs `row.step_workflow_id`); plain `resolve_agent` keeps its signature for
  the other callers (spawn factory, dispatch/spawn, apply_persona,
  `_session_start/agents.py`, `_agent.py`, build/observability).
- `RuleEngine` (`engine/core.py:88-111, 592-628`): `self.agent_manager =
  AgentDefinitionManager(db)`; `_agent_def_cache` invalidates on
  `get_definitions_revision("agents")`.
- Bulk loaders → `AgentDefinitionManager.list_all`: `dispatch/context.py:200-253`,
  `tasks/expansion/_common.py:153`, `cli/agents.py:318-380` (detail dict:
  flat step keys → nested `step_workflow`). `expansion_service.py` and
  `expansion/_compile.py` consume the same loader output and move with it.
- `agents/dry_run.py:68-86 _load_agent_body`: DELETE; use `resolve_agent`
  (fixes the no-type-filter collision hazard).
- `workflows/dry_run.py:245-317 evaluate_agent_definition`: build the inline
  `WorkflowDefinition` from `agent.step_workflow` (absent ⇒ "no step workflow"
  result).
- `skill_composition.py:79`: `body.step_workflow.variables.get("required_skills")
  if body.step_workflow else None`.
- **Mandatory `dry_run.py` extraction, performed first in this task**: move
  `_build_step_trace` and `_build_lifecycle_path` to
  `workflows/dry_run_trace.py` before rewriting `evaluate_agent_definition`.
  `dry_run.py` is 992 lines — eight below the cap — so the nested-agent rewrite
  crosses it. Constraints already names this extraction mandatory; scheduling it
  as a conditional cleanup in 4.3, after 2.4 has grown the file, contradicts
  that and leaves a commit over the limit in between. 4.3 then consumes the
  extracted helpers rather than performing the move.

**Acceptance:**

- 2.4.1 - resolve_agent resolves via the typed manager with hydrated step_workflow; the row-returning variant exists. symbol: `resolve_agent`. file: `src/gobby/workflows/agent_resolver.py`.
- 2.4.2 - RuleEngine agent cache keys on the agents domain revision. symbol: `RuleEngine`. file: `src/gobby/workflows/engine/core.py`.
- 2.4.3 - Dispatch agent loading reads the typed manager. file: `src/gobby/dispatch/context.py`.
- 2.4.4 - agents/dry_run.py uses resolve_agent; the untyped name lookup is gone. file: `src/gobby/agents/dry_run.py`.
- 2.4.5 - Agent resolution, dry-run, and required-skills composition behave identically for stepful and step-less agents. test: `tests/workflows/test_agent_resolver.py`.
- 2.4.6 - Expansion agent loading reads the typed manager across the common loader, the service, and the compiler. file: `src/gobby/tasks/expansion/_common.py`. file: `src/gobby/tasks/expansion_service.py`. file: `src/gobby/tasks/expansion/_compile.py`.
- 2.4.7 - The CLI agent listing and detail dict read the typed manager and emit the nested step_workflow key. file: `src/gobby/cli/agents.py`.
- 2.4.8 - dry_run.py is under 1,000 lines after the trace extraction and the agent rewrite. file: `src/gobby/workflows/dry_run.py`. file: `src/gobby/workflows/dry_run_trace.py`.

## P3: Runtime Snapshot Cutover
`kind: framing`

**Goal**: enforcement, transitions, completion, context, recovery, and cleanup
run entirely from immutable per-session snapshots; generated `-steps` rows and
the `-steps` name coupling are gone.

### 3.1 Step instance model and manager [category: code] (depends: P2)
`kind: deliverable`

Targets: `src/gobby/workflows/step_instances.py` (new),
`src/gobby/storage/hub/protocol.py`,
`tests/workflows/test_step_instances.py` (new)

New module (old `WorkflowInstance`/`WorkflowInstanceManager` untouched until
3.3):

```python
class AgentStepInstance(BaseModel):
    id: str; session_id: str; agent_name: str
    agent_step_workflow_id: str | None = None
    snapshot: AgentStepWorkflowBody          # immutable after creation
    enabled: bool = True
    current_step: str | None = None
    step_entered_at: datetime | None = None
    step_action_count: int = 0; total_action_count: int = 0
    variables: dict[str, Any] = Field(default_factory=dict)
    context_injected: bool = False
    created_at: datetime; updated_at: datetime

def build_step_instance(agent_body, *, session_id, step_workflow_id,
                        variables=None, current_step=None) -> AgentStepInstance
    # current_step defaults to snapshot.steps[0].name

class AgentStepInstanceManager:
    def get_for_session(self, session_id) -> AgentStepInstance | None
    def save(self, instance) -> None    # upsert ON CONFLICT(session_id);
                                        # DO UPDATE never overwrites snapshot_json,
                                        # agent_step_workflow_id, or created_at
    def replace_for_session(self, instance) -> None
                                        # DELETE + INSERT in one statement pair;
                                        # the ONLY mutator that may change
                                        # snapshot_json or agent_step_workflow_id
    def merge_variables(self, session_id, updates) -> AgentStepInstance | None
    def delete_for_session(self, session_id) -> int
```

Snapshot column = `AgentStepWorkflowBody.model_dump()` exactly; identity/lineage
live in columns. Rename `WorkflowInstanceMutation` →
`AgentStepInstanceMutation(session_id)` in `storage/hub/protocol.py` (drop
`workflow_name`), update `__all__` and the three lock users
(`apply_persona.py:101`, `session_activation.py:651`, state_manager merge path)
mechanically.

`replace_for_session` exists because `save` cannot express replacement:
its `DO UPDATE` deliberately preserves `snapshot_json` and
`agent_step_workflow_id`, so a persona switch routed through `save` would
change `agent_name` and `current_step` while retaining the previous agent's
snapshot — the enforcement-disappears failure this epic exists to remove,
reintroduced through the very primitive meant to prevent it. Replacement is a
lineage change, so it gets its own operation rather than a flag on `save`.

**Writer serialization**: every mutator (`save`, `replace_for_session`,
`merge_variables`, `delete_for_session`) runs inside
`AgentStepInstanceMutation(session_id)`. `save` writes the whole row and
`merge_variables` writes `variables`, so an unlocked step-scope merge landing
between an enforcement read and its subsequent `save` is silently overwritten,
dropping a transition or exit-condition variable. Taking the existing
per-session lock inside the manager covers every caller at one site, including
the enforcement writers in 3.3 that do not hold it today.

**Critical-section primitive**: per-mutator locking is necessary and not
sufficient. A caller computes its write from a row it read *before* acquiring
any lock, so a merge that commits in that gap is still lost when the computed
`save` lands — the lock has only narrowed the window, not closed it. Closing it
requires the whole read-compute-write span to be one critical section, which
only the calling code can delimit. This task therefore ships the two primitives
that make such a span expressible, and nothing that widens a production path:

- The mutation lock is exposed as a **re-entrant** context, so a caller may
  wrap a read and its write in `AgentStepInstanceMutation(session_id)` without
  the mutators inside double-acquiring it.
- `save` accepts an optional **compare-and-set precondition** — the read row's
  `agent_step_workflow_id` and `updated_at` — and rejects a mismatch as a stale
  write, for callers that cannot hold the span.

The lineage hazard motivates the CAS half: a `save` computed from a pre-persona
read would otherwise rewrite agent A's mutable state onto agent B's row, which
`save` accepts because it preserves only `snapshot_json` and
`agent_step_workflow_id`, not `current_step` or `variables`.

Applying these primitives to the production enforcement readers and writers is
owned by 3.2, which is where `enforcement_checks.py`,
`enforcement_handlers.py`, and `enforcement_completion.py` are Targets. Both
halves are pinned here at the manager level only.

**Acceptance:**

- 3.1.1 - AgentStepInstance and its manager exist with one-instance-per-session upsert semantics. symbol: `AgentStepInstanceManager`. file: `src/gobby/workflows/step_instances.py`.
- 3.1.2 - Snapshot, lineage id, and created_at are immutable across saves. test: `tests/workflows/test_step_instances.py::test_snapshot_immutable_on_upsert`.
- 3.1.3 - AgentStepInstanceMutation replaces WorkflowInstanceMutation in the hub protocol. symbol: `AgentStepInstanceMutation`. file: `src/gobby/storage/hub/protocol.py`.
- 3.1.4 - `replace_for_session` swaps snapshot, lineage id, agent name, and step position together, and is the only mutator that changes snapshot or lineage. test: `tests/workflows/test_step_instances.py::test_replace_for_session_swaps_snapshot_and_lineage`.
- 3.1.5 - A step-scope variable merge concurrent with an enforcement save is not lost. test: `tests/workflows/test_step_instances.py::test_merge_variables_serializes_against_save`.
- 3.1.6 - The mutation lock is re-entrant: a caller-held section wrapping a read and its computed save does not deadlock the mutators, and a merge committed outside that section cannot interleave into it. test: `tests/workflows/test_step_instances.py::test_mutation_lock_is_reentrant`.
- 3.1.7 - A save carrying a compare-and-set precondition from a pre-persona read is rejected as stale rather than rewriting the replaced instance's step position and variables. test: `tests/workflows/test_step_instances.py::test_stale_save_after_persona_replacement_rejected`.

### 3.2 Data-plane cutover and instance copy migration [category: code] (depends: 3.1)
`kind: deliverable`

Targets: `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py` (new),
`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`,
`src/gobby/dispatch/spawn.py`, `src/gobby/mcp_proxy/tools/apply_persona.py`,
`src/gobby/workflows/engine/enforcement_checks.py`,
`src/gobby/workflows/engine/enforcement_completion.py`,
`src/gobby/workflows/engine/enforcement_handlers.py`,
`src/gobby/workflows/engine/enforcement.py`,
`src/gobby/workflows/engine/core.py`, `src/gobby/hooks/factory.py`,
`src/gobby/workflows/step_context.py`, `src/gobby/hooks/session_coordinator.py`,
`src/gobby/workflows/hooks.py`, `src/gobby/agents/idle_check_handler.py`,
`src/gobby/agents/step_workflow.py` (deleted), `src/gobby/agents/sync.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py`,
`src/gobby/agents/spawn_executor.py`, `src/gobby/agents/spawn.py`,
`src/gobby/agents/spawn_models.py`, `src/gobby/agents/resume_executor.py`,
`src/gobby/runner_lifecycle_agents.py`,
`src/gobby/dispatch/daemon_resume.py`,
`src/gobby/workflows/state_manager.py`,
`src/gobby/mcp_proxy/tools/agents_spawn_tools.py`,
`src/gobby/servers/websocket/chat/_session.py`,
`src/gobby/storage/migrations/NNN_copy_agent_step_instances.sql` (new),
`tests/storage/test_instance_copy_migration.py` (new),
`tests/agents/test_spawn.py`, `tests/agents/test_spawn_executor.py`,
`tests/agents/test_spawn_executor_droid.py`, `tests/agents/test_srt_spawn.py`,
`tests/agents/test_resume_executor.py`,
`tests/mcp_proxy/tools/spawn_agent/test_execution.py`,
`tests/mcp_proxy/tools/test_apply_persona.py`,
`tests/mcp_proxy/tools/spawn_agent/test_factory.py`

One atomic cutover — writers and readers share the data plane:

- **Spawn**: extract the step-state block
  (`_implementation.py:134-163, 861-905`) into `_step_state.py`;
  `_initial_step_state_for_spawn` takes `AgentStepWorkflowBody`; creation is
  `AgentStepInstanceManager.save(build_step_instance(...))` using
  `resolve_agent_with_row` for `step_workflow_id`; kill `_step_workflow_name`
  seeding. Delete `_factory.py:237-250,406-409` registration and
  `dispatch/spawn.py:231-236`; delete `src/gobby/agents/step_workflow.py` and
  the `_refresh_step_workflow` scaffolding call in `src/gobby/agents/sync.py`.
- **Persona**: the public entry point is `build_session_persona_changes` /
  `apply_persona_impl` in `apply_persona.py`, not the session-start helper —
  wire the step-instance work there so every persona caller inherits it. Drop
  the `_step_workflow_name` change. Under `AgentStepInstanceMutation`, the
  transition is a function of three inputs — target stepfulness, whether the
  target agent matches the row's `agent_name`, and whether a row exists at all
  — and all four reachable combinations are specified, because "same agent"
  and "row exists" are independent:
  - **stepful target, same agent, row present** → preserve the instance and its
    step position; no write.
  - **stepful target, same agent, no row** → `replace_for_session` with a fresh
    snapshot. This is reachable and is not a no-op: an agent that gained a
    `step_workflow` since the session started, or whose row was deleted or
    never created, hits it. Treating same-agent as implying row-present would
    report activation success while leaving the session with no snapshot —
    precisely the durable-row guarantee this task's fail-closed boundary
    exists to make.
  - **stepful target, different agent** → `replace_for_session` with a fresh
    snapshot; never `save`, which would keep the previous agent's snapshot
    under a new `agent_name`.
  - **step-less target** (the four step-less bundled agents) →
    `delete_for_session`, idempotent whether or not a row exists, since a
    snapshot row with no snapshot to hold is not representable.
- **Rule engine**: `engine/core.py` constructs the instance manager. Switch
  `self.instance_manager` to `AgentStepInstanceManager` in this task, in the
  same commit as the readers that expect it. Leaving construction on the legacy
  class while 3.2 readers call the typed API breaks the daemon at the first
  enforcement check, so the constructor is part of the cutover rather than the
  cleanup.
- **Fail-closed snapshot persistence**: a stepful spawn does not return success
  and a persona activation does not report complete until its
  `agent_step_instances` row is durable. Reporting success without the row hands
  the session to definition-time recovery and reproduces the
  vanishing-enforcement bug this epic exists to remove, so this is the stated
  success boundary of the cutover. **Placement is load-bearing, and "before
  `start_run_or_cleanup`" is not early enough**: `execute_spawn`
  (`_implementation.py:713`) has already launched the provider tmux process by
  then, and it is also what inserts the child session record via
  `prepare_terminal_spawn`, so no ordering *within* the current call sequence
  puts the snapshot ahead of the process. `cleanup_failed_spawn`
  (`_failure_cleanup.py:23-50`) fails the run, delivers terminal notification,
  cleans isolation, and deletes the child session row — it never terminates the
  PID or the tmux session. So both halves are required:
  - **Hoist child-session preregistration out of `execute_spawn`** into the
    caller, so the session row and its `agent_step_instances` row are both
    durable before any process exists. `execute_spawn` then receives the
    child session id instead of minting it. This is what makes the no-live-child
    guarantee true rather than asserted.

    The session is not minted in `execute_spawn` itself: it is minted by
    `prepare_terminal_spawn` (`src/gobby/agents/spawn.py:76-249`), which
    creates the child session, the `agent_runs` row, and the terminal env
    vars, and returns them as the existing `PreparedSpawn` dataclass
    (`spawn.py:48`). Hoisting therefore means inverting that function's
    position rather than adding a field: the caller calls
    `prepare_terminal_spawn` first, saves the step instance, and passes the
    resulting `PreparedSpawn` down. `SpawnRequest`
    (`src/gobby/agents/spawn_models.py:16`) carries no prepared-spawn field
    today, so it gains one required field holding that object, and the
prepare call is deleted from the five provider consumers that reach it
through `SpawnRequest`: `spawn_executor.py:171, 301, 401, 513, 628`.
Reuse `PreparedSpawn`; do not introduce a second prepared-spawn type.

Hoisting preparation creates a new pre-launch failure boundary: by the time the
caller saves the step instance, `prepare_terminal_spawn` has already created both
the child session and its `agent_runs` row. If that save fails, the caller runs
bounded compensation that deletes those exact two newly-created rows before
returning the error. No provider process exists yet, so this path does not invoke
post-launch termination cleanup; it simply leaves no durable spawn rows and does
not claim a task. A compensation failure is surfaced with both row ids for
operator recovery rather than hiding the leak behind the original save error.

**`resume_executor.py:192` is deliberately excluded.** It is a sixth
caller of `prepare_terminal_spawn`, but it is not on the `SpawnRequest`
path: `resume_agent_run` builds its own `spawn_context` inline and its
    sole caller supplies no `PreparedSpawn`, so deleting the call there leaves
    the resume path with nothing to construct a session from. Preparation
    stays inside `resume_agent_run`.

    **Resume step-state continuity is out of scope; see #18974.** A resumed run
    creates a new child session and `agent_step_instances` is keyed
    `UNIQUE(session_id)`, so the original session's row does not follow it — a
    daemon-stop resume comes back at `steps[0]` with empty variables. That is
    the behavior today and this task preserves it: `runtime_cleanup.py:24-65`
    already deletes the source row when shutdown terminalizes the run with
    `terminal_reason='daemon_stop'` (`runner_lifecycle_agents.py:349-411`), so a
    clone-on-resume rule stated here would have no source to read. Making resume
    continuous requires source retention across terminal cleanup, a defined
    commit point, and a prepared-child rollback contract shared with spawn —
    `_fail_run` (`resume_executor.py:604-625`) does not delete the prepared
    child, unlike `spawn_agent/_failure_cleanup.py:23-50`. That is a lifecycle
    change, not a storage split, and it is task #18974.
    `dispatch/daemon_resume.py:70` is the sole caller of `resume_agent_run` and
    stays in Targets only so the exclusion is anchored to a real call site; this
    task makes no edit there.

    `tests/agents/test_spawn.py` asserts `prepare_terminal_spawn`'s current
    return and persistence contract, `tests/agents/test_spawn_executor.py`
    constructs `SpawnRequest` and fakes the spawn context per provider,
    `tests/agents/test_resume_executor.py` monkeypatches
    `prepare_terminal_spawn` in seven places, and
    `tests/mcp_proxy/tools/spawn_agent/test_execution.py` is the existing
    execution-and-preregistration suite. Two provider-specific suites sit on
    the same seam and are easy to miss because they are not named for the
    executor: `tests/agents/test_spawn_executor_droid.py` builds
    `SpawnRequest(**values)` at `:38` and patches
    `spawn_executor.prepare_terminal_spawn` at `:75`, `:93`, `:136`, `:198`,
    and `tests/agents/test_srt_spawn.py` constructs `SpawnRequest` at `:82`
    and `:153` and patches the same target at `:122` and `:173`. A new
    required field on `SpawnRequest` breaks every direct constructor, and a
    deleted prepare call breaks every patch of it, so all six move with the
    boundary.
  - **Add explicit process termination to `cleanup_failed_spawn`** as
    compensation for every failure mode that remains after launch. It
    terminates the recorded PID and kills the tmux session before deleting the
    child session row, and tolerates an already-dead process. The compensation
    needs process identity that outlives the failure, and today it does not
    have it: `_persist_spawn_runtime` (`_implementation.py:766`, `:790`) is
    what writes the PID and tmux name to the run row, and it runs *after* the
    launch. `cleanup_failed_spawn` receives only `runner`, `run_id`, the
    error, and `child_session_id`, so at the earlier failure points the row it
    would read is empty while `spawn_result` — which holds the identity — is
    in scope at the call site and never passed. Persist or pass
    `SpawnResult`'s PID and tmux identity as soon as the provider returns,
    before anything that can fail, and thread it into the cleanup call. The
    post-launch failure points this must cover are all four, not just the two
    the previous revision named:
    - `task_spawn_lease.attach` (`_implementation.py:727-745`), between launch
      and runtime persistence;
    - the tmux **live-pane verification** at `_implementation.py:752-762`,
      which on failure sets `spawn_result.success = False` and falls through
      the `if spawn_result.success` block at `:788` — reaching no cleanup call
      at all today, so it leaks both the process and the attached lease;
    - `start_run_or_cleanup` (`:775`, `:800`);
    - the atomic post-claim update below.
- **Auto-claim ordering**: `_initial_step_state_for_spawn` derives
  `task_claimed` and the claim-step advance from `task_owned_by_child`
  (`_implementation.py:854`, `:158`), which is computed *after* `execute_spawn`.
  Persisting the snapshot before the claim therefore preserves the
  no-claim-on-failure property while losing the successful claim transition,
  leaving an auto-claimed agent parked at `claim` with `task_claimed` unset —
  the failure path fixed and the normal path broken. Resolve it as a two-stage
  write, not a reordering: the pre-launch save persists the unclaimed initial
  state, and the auto-claim performs an atomic follow-up update of
  `current_step` and `variables` on the existing row inside
  `AgentStepInstanceMutation`. A fault in the follow-up is a post-launch failure
  and routes through the termination compensation above.
- **Persona cross-row atomicity**: a persona switch writes two rows — the step
  instance (`replace_for_session` or `delete_for_session`) and the session
  variables carrying `_agent_type` — and both belong to one state transition.
  Enclose them in one transaction opened as
  `db.transaction_immediate(AgentStepInstanceMutation(session_id))`, with
  `conn.acquire_additional_lock(SessionVariableMutation(session_id))` taken
  inside it. The plain `db.transaction()` cannot be used: it is non-immediate,
  `_ambient.py:45` rejects a nested `transaction_immediate` inside it, and
  `_PostgresTransaction.acquire_additional_lock`
  (`postgres_pool.py:239-250`) raises unless the transaction is immediate — so
  "a single immediate `db.transaction()`" names no callable API. The lock order
  is not a convention either: `_acquire_lock` (`postgres_pool.py:503-511`)
  raises `LockAcquisitionOrderError` unless nested priority strictly increases,
  and `AgentStepInstanceMutation` inherits `PRIORITY = 875` against
  `SessionVariableMutation`'s `950` (`storage/hub/protocol.py:258`, `:267`), so
  instance-outermost is the only order the adapter accepts. Both managers must
  therefore join the ambient transaction or accept its `Transaction` handle
  rather than opening their own. Without the enclosure a variable-merge failure
  after a successful
  replacement leaves the new snapshot advertising the old `_agent_type`, and the
  opposite order leaves the new identity pointing at the prior agent's
  snapshot — each a different spelling of the enforcement mismatch the epic
  removes. `SessionVariableManager` is in Targets for the transaction handle it
  must accept. The two direct callers of `apply_persona_impl` —
  `mcp_proxy/tools/agents_spawn_tools.py` and
  `servers/websocket/chat/_session.py` — are in Targets because the new
  transaction and fixed lock order are only sound if neither caller already
  holds a database transaction or the session-variable lock when it calls in; a
  nested acquisition in the opposite order is the deadlock this ordering exists
  to prevent. Verify both and, if either does, hoist its call outside its own
  transaction rather than relaxing the atomicity requirement.
- **Caller fail-closed propagation**: making `apply_persona_impl` fail closed
  achieves nothing while a caller converts the failure into success.
  `servers/websocket/chat/_session.py:910-923` wraps the call in
  `except Exception as e: logger.warning(...)` and then falls straight into
  `registry.register(session_key, session)` at `:925-929`, so a step-instance
  or cross-row transaction failure still returns a started, registered chat
  session whose persona never applied — a session running with no snapshot,
  which is the vanishing-enforcement state in a different costume. Make this
  caller propagate: stop the runtime, unregister it, and delete or roll back
  the newly created chat session, then re-raise. The activation block twenty
  lines earlier (`:840-871`) already does exactly this for its own failure and
  is the pattern to follow. Audit `agents_spawn_tools.py:105` for the same
  swallow.
- **Enforcement critical sections**: apply the 3.1 primitives to the
  production paths this task owns. Every enforcement read whose value feeds a
  later write runs inside the same `AgentStepInstanceMutation(session_id)` as
  the write it produces — `_get_step_for_session` in `enforcement_checks.py`
  paired with the transition writer in `enforcement_handlers.py:150-198`, and
  the completion read/write pair in `enforcement_completion.py:210-473`. The
  re-entrant lock means the inner mutators do not double-acquire. Where a span
  genuinely cannot be held, pass the read row's `agent_step_workflow_id` and
  `updated_at` as the compare-and-set precondition instead and treat rejection
  as a lost race, not an error to swallow. 3.1 ships the primitives; the
  widening lands here because these three files are Targets of this task.
- **Readers**: `_get_step_for_session` (`enforcement_checks.py:64-92`) becomes
  a single-row lookup returning `(step, instance)` with
  `instance.snapshot.get_step(instance.current_step)`; update mixin protocol
  declarations and every `(step, instance, definition)` unpacker
  (`enforcement.py:40`, `enforcement_completion.py:37,48-50`,
  `enforcement_handlers.py:22`). `step_context.py:41-89` reads the instance
  (`workflow_name` → `agent_name`); `session_coordinator.py:708-785` uses
  `instance.snapshot.exit_condition` (drop the definition-manager loop).
- **Transition/completion writers** (`enforcement_handlers.py:150-198`,
  `enforcement_completion.py:210-473`): new manager; `instance.workflow_name`
  → `instance.agent_name`; exit evaluation reads the snapshot.
- **Context injection** (`workflows/hooks.py:660-694`) and idle reprompt
  (`idle_check_handler.py:638-676`): field rename; drop `_step_workflow_name`
  writes.
- **Instance copy migration** (same commit): guarded; one row per session
  (latest `updated_at` wins, **over all candidate rows regardless of
  `enabled`**); `agent_name = regexp_replace(workflow_name, '-steps$', '')`.
  Dead-session rows (incl. the 21 `session-lifecycle`/`auto-task` orphans and
  24 `developer-steps` rows) are dropped with the legacy table in P7.
  Selecting only enabled rows would contradict the column mapping below, which
  copies `enabled` verbatim: a live session whose only instance is disabled
  would be skipped as absent, 3.3's recovery would then find no typed row and
  build a fresh *enabled* snapshot at `steps[0]`, and a deliberately disabled
  instance would come back re-enabled and rewound. `enabled` is state to
  preserve, not a selection filter.
  - **Live-status filter**: copy sessions whose `status IN ('active','paused',
    'handoff_ready')`. `handoff_ready` is a live state — a session mid
    compaction handoff — and omitting it deletes the instance of a running
    agent, after which recovery rebuilds from the current definition at
    `steps[0]` and silently rewinds an in-flight run. Take the status set from
    the session-status constants the lifecycle code uses, not a hand-copied
    literal list, so a future status cannot silently fall out of scope.
  - **Column mapping**: copy `id`, `session_id`, `enabled`, `current_step`,
    `step_entered_at`, `variables`, `context_injected`, `created_at`, and
    `updated_at` verbatim; resolve `agent_step_workflow_id` from
    `agent_step_workflows` via the derived `agent_name` and populate it
    **whenever that child row exists**, since it is the typed lineage column
    and a target-only field with no legacy source — leaving it NULL where a
    child does exist silently detaches the migrated row from its definition,
    which is the FK the snapshot-vs-definition distinction is built on. Where
    the child does not exist the column stays NULL rather than aborting: the
    column is declared nullable with `ON DELETE SET NULL` precisely so a
    snapshot can outlive its definition (3.4 behavior (b) pins that), so a
    live row carrying a valid snapshot whose agent was hard-deleted before P2
    is a legitimate migration input, not a failure. The migration still fails
    on a row with neither lineage nor a recoverable snapshot.
    `COALESCE` the nullable legacy counters and JSON
    (`step_action_count`, `total_action_count` → `0`; `variables` → `'{}'`;
    `context_injected` → `false`) into the NOT NULL typed columns. Dropping
    `variables` re-runs satisfied transitions and re-blocks passed gates;
    inserting a legacy NULL into a NOT NULL column aborts the migration.
  - **Snapshot source, two branches**: the generated-row body converted to the
    `{steps, variables, exit_condition}` shape; else rebuilt from
    `agent_step_workflows` via the agent name; else RAISE (epic fail-loud
    contract).
  - **Per-branch equivalence guard**: on the generated-row branch, compare the
    copied snapshot and step position against the normalized legacy body; on
    the rebuild branch, where there is no legacy snapshot to compare against,
    compare the child payload verbatim. **Both branches then assert
    `current_step` is a member of `snapshot.steps`** — membership is a
    structural invariant of the copied row, independent of which source
    produced the snapshot, so it is checked unconditionally rather than as a
    substitute for payload comparison on the branch that lacks one. Restricting
    it to the rebuild branch treats equality with the generated legacy row as
    proof of resolvability, and it is not: `register_agent_step_workflow`
    refreshes that generated row whenever the agent definition changes, so an
    instance that entered a step later removed by a refresh has a
    `current_step` absent from the very body it is being compared against —
    the payload check passes, the row migrates, readers return no step, and
    enforcement silently stops. Equality of a copied `current_step` value
    proves nothing about membership on either branch.

**Acceptance:**

- 3.2.1 - Spawn creates the per-session snapshot instance and no generated-row registration remains in the spawn path. file: `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`. file: `src/gobby/dispatch/spawn.py`.
- 3.2.1a - Persona activation creates, preserves, or removes the instance on the public apply_persona_impl path. symbol: `apply_persona_impl`. file: `src/gobby/mcp_proxy/tools/apply_persona.py`.
- 3.2.2 - agents/step_workflow.py is deleted and the sync scaffolding call is gone. file: `src/gobby/agents/step_workflow.py`. file: `src/gobby/agents/sync.py`.
- 3.2.3 - Enforcement reads resolve the step from the instance snapshot in one row lookup. symbol: `EnforcementCheckMixin._get_step_for_session`. file: `src/gobby/workflows/engine/enforcement_checks.py`.
- 3.2.3a - Transition and completion writers read and write the snapshot instance with agent_name in place of workflow_name. file: `src/gobby/workflows/engine/enforcement_handlers.py`. file: `src/gobby/workflows/engine/enforcement_completion.py`. file: `src/gobby/workflows/engine/enforcement.py`.
- 3.2.3b - Step context and the coordinator completion gate read the snapshot instead of the definition manager. file: `src/gobby/workflows/step_context.py`. file: `src/gobby/hooks/session_coordinator.py`.
- 3.2.4 - The instance copy migration preserves live-session step state with a valid snapshot and fails loudly when none is recoverable. test: `tests/storage/test_instance_copy_migration.py`.
- 3.2.5 - _step_workflow_name is gone from the spawn, persona, context-injection, and idle-reprompt writers. file: `src/gobby/workflows/hooks.py`. file: `src/gobby/agents/idle_check_handler.py`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py`. file: `src/gobby/mcp_proxy/tools/apply_persona.py`.
- 3.2.6 - spawn_agent/_implementation.py is under 1,000 lines after extraction. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.
- 3.2.7 - The equivalence guard fails on the generated-row branch when snapshot or step position diverges, and fails on both branches when current_step is absent from the copied snapshot's steps, including a generated row whose refresh removed the active step. test: `tests/storage/test_instance_copy_migration.py`.
- 3.2.8 - A failed step-instance save aborts before any child process starts or task is claimed, deletes the child session and agent-run rows created by pre-launch preparation, and aborts the persona switch with the prior instance intact. test: `tests/workflows/test_step_snapshot_semantics.py`.
- 3.2.9 - A handoff_ready session keeps its step position and variables through the migration. test: `tests/storage/test_instance_copy_migration.py::test_handoff_ready_session_continuity`.
- 3.2.10 - Variables, both action counters, timestamps, enabled, and context_injected survive the copy with nullable legacy values normalized; agent_step_workflow_id is populated wherever the typed child exists and left NULL for a valid snapshot whose child does not, while a row with neither lineage nor a recoverable snapshot fails the migration. test: `tests/storage/test_instance_copy_migration.py::test_runtime_field_equivalence`. test: `tests/storage/test_instance_copy_migration.py::test_definitionless_snapshot_migrates_with_null_lineage`.
- 3.2.11 - A persona switch to a different agent replaces snapshot and lineage together; a switch to a step-less agent removes the instance. test: `tests/workflows/test_step_snapshot_semantics.py`.
- 3.2.12 - RuleEngine constructs AgentStepInstanceManager in the same commit as the typed readers. symbol: `RuleEngine`. file: `src/gobby/workflows/engine/core.py`.
- 3.2.13 - The caller calls prepare_terminal_spawn and saves the step instance before launch, so the instance row is durable before any provider process exists. symbol: `prepare_terminal_spawn`. file: `src/gobby/agents/spawn.py`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.
- 3.2.13a - SpawnRequest carries the prepared spawn and every provider path consumes it without creating a second session across all five executor call sites. file: `src/gobby/agents/spawn_models.py`. file: `src/gobby/agents/spawn_executor.py`.
- 3.2.13b - The spawn, executor, droid, SRT, resume, and execution suites are retargeted to the moved boundary and still pin agent_run persistence and per-provider spawn context. test: `tests/agents/test_spawn.py`. test: `tests/agents/test_spawn_executor.py`. test: `tests/agents/test_spawn_executor_droid.py`. test: `tests/agents/test_srt_spawn.py`. test: `tests/agents/test_resume_executor.py`. test: `tests/mcp_proxy/tools/spawn_agent/test_execution.py`.
- 3.2.13c - The resume path keeps its inline prepare_terminal_spawn call and gains no step-state transfer, so daemon-stop resume behavior is unchanged by this task and remains task #18974. symbol: `resume_agent_run`. file: `src/gobby/agents/resume_executor.py`.
- 3.2.21 - Persona tests are retargeted off _step_workflow_name and WorkflowInstanceManager onto the typed instance. test: `tests/mcp_proxy/tools/test_apply_persona.py`.
- 3.2.22 - The spawn factory suite's self-healing registration tests are retargeted at prepared snapshot creation, since the symbol they import is deleted in this task. test: `tests/mcp_proxy/tools/spawn_agent/test_factory.py`.
- 3.2.14 - cleanup_failed_spawn terminates the recorded PID and tmux session before deleting the child session row, and tolerates an already-dead process. symbol: `cleanup_failed_spawn`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py`.
- 3.2.14a - Provider process identity is available to cleanup at every post-launch failure point, and the tmux live-pane verification failure routes through cleanup instead of falling through. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.
- 3.2.14b - Fault injection at lease attach, live-pane verification, run start, and the post-claim update each leave no surviving PID, no tmux session, and no attached lease. test: `tests/workflows/test_step_snapshot_semantics.py::test_post_launch_faults_leave_no_live_process`.
- 3.2.15 - An auto-claimed spawn reaches the same effective initial step and variables as before the reordering, via the atomic post-claim update. test: `tests/workflows/test_step_snapshot_semantics.py::test_auto_claimed_spawn_initial_step_preserved`.
- 3.2.16 - A persona switch commits instance replacement and the _agent_type variable merge in one immediate transaction with the instance lock outermost; a fault in either leaves neither applied, for stepful and step-less targets. test: `tests/workflows/test_step_snapshot_semantics.py::test_persona_switch_is_atomic_across_rows`.
- 3.2.17 - Persona activation targeting the same agent with no existing instance creates a fresh snapshot rather than reporting a successful no-op, and a persistence fault on that branch fails the activation. test: `tests/workflows/test_step_snapshot_semantics.py::test_persona_same_agent_missing_instance_creates_snapshot`.
- 3.2.18 - A persona failure on the web-chat path propagates: the runtime is stopped, the session is not registered, and the caller does not report success. file: `src/gobby/servers/websocket/chat/_session.py`.
- 3.2.19 - Enforcement read-compute-write pairs hold one mutation section, so a concurrent step-scope merge cannot be lost across a transition or completion write. test: `tests/workflows/test_step_snapshot_semantics.py::test_enforcement_write_paths_hold_one_critical_section`.
- 3.2.20 - A live session whose only legacy instance is disabled migrates with enabled preserved, and activation neither re-enables it nor rewinds its step. test: `tests/storage/test_instance_copy_migration.py::test_disabled_instance_continuity`.

### 3.3 Recovery, cleanup, and auxiliary surfaces [category: code] (depends: 3.2)
`kind: deliverable`

Targets: `src/gobby/hooks/session_activation.py`,
`src/gobby/hooks/event_handlers/_session_end.py`,
`src/gobby/agents/runtime_cleanup.py`, `src/gobby/agents/agent_cleanup.py`,
`src/gobby/workflows/reserved_variables.py`,
`src/gobby/mcp_proxy/tools/workflows/_variables.py`,
`src/gobby/mcp_proxy/tools/workflows/_query.py`,
`src/gobby/mcp_proxy/tools/workflows/__init__.py`,
`src/gobby/servers/routes/workflows.py`,
`src/gobby/workflows/state_manager.py`, `src/gobby/workflows/definitions.py`,
`src/gobby/storage/session_lifecycle.py`,
`src/gobby/storage/sessions/_lifecycle_delegate.py`,
`src/gobby/hooks/event_handlers/_session_start/flow.py`,
`tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py`,
`tests/storage/sessions/test_lifecycle.py`,
`tests/storage/sessions/test_pruning.py`,
`tests/hooks/test_session_end_handlers.py`,
`tests/hooks/test_session_start_handlers.py`,
`tests/hooks/test_session_handoff_handlers.py`

- `session_activation.py`: `_activation_agent_name` (:531-547) drops both
  `-steps`-suffix branches; `_missing_step_workflow` (:594-621) →
  `_missing_step_state` checking `AgentStepInstanceManager.get_for_session`;
  `_workflow_definition_exists` (:624-633) → `_agent_has_step_workflow` via
  `resolve_agent(...).step_workflow is not None`;
  `_ensure_step_workflow_from_definition` (:636-684) → `_ensure_step_instance`
  under the mutation lock: recovery takes a **fresh snapshot** from the
  current definition (`current_step = steps[0].name`, variables from
  `snapshot.variables`) — the immutable snapshot's lifetime is the row's
  lifetime. That is a real semantic break: the session resumes under a
  possibly-newer definition than it started on. `_ensure_step_instance`
  therefore emits exactly one structured warning through the module's existing
  logger, carrying `session_id`, `agent_name`, the resolved agent-definition
  and step-workflow ids, and a stable marker string so operators can tell
  reconstructed continuity from original continuity. No new metric or logging
  subsystem.
- Cleanup: `_session_end.py:107-118` and `runtime_cleanup.py:53` →
  `delete_for_session`. `agent_cleanup.py:454` logs the cleared-state summary
  under a `workflow_instances=` key; rename it to `agent_step_instances=`, since
  7.2's audit matches the token and nothing else owns this occurrence.
- **Compaction continuity must survive the port** (landed as #18973, commit
  `92b3ca567`; this plan preserves it rather than introducing it).
  `_session_end.py` now classifies the end reason *first* and derives
  `terminal_outcome = end_status == "expired"`; both `complete_agent_run` and
  the instance delete are gated on it, so `COMPACT` → `handoff_ready` and IDLE
  web-chat → `paused` retain their state. The port must carry that gate across
  unchanged — dropping it reintroduces a per-compaction rewind, and the audit
  in 7.2 cannot see a missing conditional.
- **Compact handoff is in-place (#18994); there is no transfer to port.**
  `transfer_compact_handoff_state` was deleted: a compact restart reactivates
  the same session row, so `workflow_instances.session_id` never moves during
  compaction and the `UNIQUE(session_id)` key on `agent_step_instances` is
  satisfied trivially. What must carry over instead: the sweeps.
  `expire_orphaned_handoff_sessions` (`session_lifecycle.py`) only flips
  status now (workflow state is kept for revival), and
  `prune_stale_compact_workflow_instances` deletes instances for sessions
  expired >24h that still carry the unconsumed `handoff_source` marker —
  retarget that DELETE at `agent_step_instances`.
- **The suites #18973 added assert against the legacy table** and move with it:
  `tests/storage/sessions/test_lifecycle.py` (expire-only orphan sweep and
  marker-gated retention pruning), `tests/storage/sessions/test_pruning.py`,
  `tests/hooks/test_session_end_handlers.py` (the terminal-outcome gate),
  `tests/hooks/test_session_start_handlers.py`, and
  `tests/hooks/test_session_handoff_handlers.py`.
- `reserved_variables.py`: remove `_step_workflow_name`; keep
  `step_workflow_complete`.
- MCP variable tools (`_variables.py:71-257`): replace the `workflow: str`
  param with `scope: Literal["session","step"] = "session"`; step scope targets
  the session's single instance via `merge_variables`; update tool schemas in
  `workflows/__init__.py`.
- `_query.py::get_workflow_status` → `get_step_status(session_id)` reporting
  `agent_name`, `current_step`, snapshot step list, exit condition (MCP
  registration renamed in 5.2).
- **Completion-seed re-gating**: `_step_completion_updates` keys
  `step_workflow_complete` initialization on `_step_workflow_name`, which 3.3
  removes, and it runs before recovery creates the typed instance. Re-gate the
  seeding on `AgentStepInstanceManager.get_for_session` and order it **after**
  `_ensure_step_instance`. Left as-is, the completion key is never seeded once
  the variable disappears and activation reports an unresolved invariant on
  every turn. State explicitly whether `_missing_step_state` also checks the
  completion key so the two predicates cannot drift apart.
- **Pre-deletion rewiring** (same commit as the deletion): `WorkflowInstance`
  and `WorkflowInstanceManager` still have live importers that P5 does not
  touch until later — the MCP variable/query tool registrations in
  `mcp_proxy/tools/workflows/__init__.py` and the generic runtime-variable
  routes in `servers/routes/workflows.py`. Convert both to the typed manager
  here, before the classes are deleted; otherwise every intermediate commit
  from P3 to P5 fails to import and the "working daemon at every commit"
  constraint is violated.
- Delete `WorkflowInstance` from `definitions.py:863-911` and
  `WorkflowInstanceManager` from `state_manager.py:66-192`; fix remaining
  test imports.

**Acceptance:**

- 3.3.1 - Restart recovery rebuilds step state from the agent definition without any -steps name parsing. symbol: `_ensure_step_instance`. file: `src/gobby/hooks/session_activation.py`.
- 3.3.2 - Session-end and agent-terminal cleanup delete the per-session instance. file: `src/gobby/hooks/event_handlers/_session_end.py`. file: `src/gobby/agents/runtime_cleanup.py`.
- 3.3.3 - Workflow-scoped variable tools use the scope parameter against the single instance. file: `src/gobby/mcp_proxy/tools/workflows/_variables.py`.
- 3.3.4 - WorkflowInstance and WorkflowInstanceManager no longer exist. file: `src/gobby/workflows/state_manager.py`.
- 3.3.5 - _step_workflow_name is absent from reserved variables and all rule/variable plumbing. file: `src/gobby/workflows/reserved_variables.py`.
- 3.3.6 - Fresh-snapshot recovery emits one structured warning carrying the session, agent name, resolved definition ids, and a stable recovery marker. test: `tests/workflows/test_step_snapshot_semantics.py`.
- 3.3.7 - step_workflow_complete is seeded from the typed instance after recovery creates it, with no reference to the removed variable. test: `tests/hooks/test_session_activation.py::test_completion_seed_after_step_instance_recovery`.
- 3.3.8 - The MCP tool registrations and generic runtime-variable routes use the typed manager before WorkflowInstanceManager is deleted, so the tree imports at that commit. file: `src/gobby/servers/routes/workflows.py`. file: `src/gobby/mcp_proxy/tools/workflows/__init__.py`.
- 3.3.9 - The agent runtime-cleanup log no longer names workflow_instances. file: `src/gobby/agents/agent_cleanup.py`.
- 3.3.10 - Session-end cleanup keeps the terminal-outcome gate, so a COMPACT or IDLE web-chat end retains the typed instance and only an expired end deletes it. file: `src/gobby/hooks/event_handlers/_session_end.py`. test: `tests/hooks/test_session_end_handlers.py`.
- 3.3.12 - In-place compact reactivation (#18994) leaves the typed instance keyed to the same session across a compact restart, with no ownership move and no legacy table named. file: `src/gobby/storage/session_lifecycle.py`. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.
- 3.3.13 - Orphan handoff expiry only flips status; the marker-gated retention sweep deletes typed instances for sessions expired past the revival horizon, with no legacy table named. symbol: `expire_orphaned_handoff_sessions`. symbol: `prune_stale_compact_workflow_instances`. file: `src/gobby/storage/session_lifecycle.py`.
- 3.3.14 - A compacted mid-workflow agent resumes on its same session at the same nonzero step with the same variables after the port. test: `tests/storage/sessions/test_lifecycle.py`. test: `tests/workflows/test_step_snapshot_semantics.py`.
- 3.3.11 - The spawn initial-variables suite queries the typed instance instead of importing the deleted WorkflowInstanceManager, dropping its `<agent>-steps` name arguments. test: `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py`.

### 3.4 Snapshot behavior regression suite [category: test] (depends: 3.3)
`kind: deliverable`

Target: `tests/workflows/test_step_snapshot_semantics.py` (new)

Standalone behavior-pinning suite (isolated test daemon state,
`GOBBY_TEST_PROTECT=1`): (a) definition edited during an active run — running
snapshot unaffected, next spawn sees the edit; (b) definition deleted during a
run — FK SET NULL, snapshot still drives enforcement and the completion gate;
(c) two concurrent spawns of the same agent get independent snapshots — a step
rename between them affects neither in-flight run; (d) project-scoped agent
override — spawn snapshot captures the project-resolved definition; (e)
daemon-restart recovery recreates a missing instance from the current
definition and emits the structured recovery warning; (f) persona switch to a
different agent replaces the instance, same agent preserves step position; (g)
pre-launch snapshot persistence fault injection — `AgentStepInstanceManager.save`
raises during a stepful spawn and `replace_for_session` raises during persona
activation: the spawn reports failure leaving no started child session, no live
child process, no `agent_started` broadcast, and no task claim, and the persona
switch leaves the prior instance intact rather than half-replaced; (h)
post-launch fault injection — the fault is injected *after* the provider process
starts, at each of the four post-launch failure points (lease attach, tmux
live-pane verification, `start_run_or_cleanup`, and the post-claim update), and
the test asserts through the real spawn executor rather than a mocked one that
the recorded PID, the tmux session, and the attached lease are all gone
afterward, since a mock cannot prove the compensation ran; (i) persona cross-row
atomicity — a session-variable
merge failure after a successful instance replacement leaves neither applied, and
the same holds with the operations inverted, for both stepful and step-less
targets; (j) auto-claim normal path — a spawn whose task is auto-claimed by the
child reaches the same `current_step` and `task_claimed` state as the
pre-reordering behavior; (k) persona activation targeting the same agent when no
instance row exists — a fresh snapshot is created rather than a silent no-op,
and a persistence fault on that branch fails the activation; (l) persona failure
on the web-chat caller — the runtime is stopped, the chat session is not
registered, and the caller reports failure rather than a started session with no
snapshot; (m) compaction continuity — a COMPACT end retains the instance, the in-place
compact reactivation (#18994) keeps it keyed to the same session, and the
agent resumes at the same nonzero step with the same variables, while an
expired end still deletes it.

**Acceptance:**

- 3.4.1 - All thirteen pinned behaviors pass against the snapshot runtime. test: `tests/workflows/test_step_snapshot_semantics.py`.
- 3.4.2 - Post-launch fault injection runs against the real spawn executor at all four failure points and proves no PID, tmux session, or attached lease survives. test: `tests/workflows/test_step_snapshot_semantics.py::test_post_launch_failure_terminates_process`.

## P4: Rules, Variables, Pipelines Rewiring
`kind: framing`

**Goal**: each remaining domain reads and writes only its typed table; each
task ships its copy migration in the same commit as its cutover.

**Ordering**: every P4 cutover depends on completed P3, and 4.2 additionally
depends on 4.1. The domains are independent in storage but not in source files.
P2 and P3 still own `apply_persona.py`, `engine/core.py`,
`hooks/session_activation.py`, `workflows/hooks.py`, `state_manager.py`, and the
spawn factory while P4 would otherwise be free to start; 4.1 and 4.2 overlap
each other on `hooks.py`, `_session_start/agents.py`, `apply_persona.py`, and
`state_manager.py`. Starting P4 off P1/P2 lets a rule or variable cutover edit
the pre-P3 shape of a file P3 is concurrently rewriting, which produces merge
conflicts and intermediate commits that do not run. 4.3 keeps its parallelism
with 4.1 and 4.2 because it shares no production file with either once its
`dry_run.py` extraction has moved into 2.4 (see 2.4's mandatory extraction).

### 4.1 Rules cutover and copy migration [category: code] (depends: P3)
`kind: deliverable`

Targets: `src/gobby/workflows/engine/core.py`,
`src/gobby/workflows/sync_rules.py`, `src/gobby/hooks/session_activation.py`,
`src/gobby/mcp_proxy/tools/workflows/_rules.py`,
`src/gobby/servers/routes/rules.py`, `src/gobby/cli/rules.py`,
`src/gobby/mcp_proxy/tools/apply_persona.py`,
`src/gobby/hooks/event_handlers/_session_start/agents.py`,
`src/gobby/workflows/engine/effects.py`, `src/gobby/workflows/selectors.py`,
`src/gobby/workflows/hooks.py`,
`src/gobby/workflows/engine/evaluation.py`,
`src/gobby/workflows/reserved_variables.py`,
`src/gobby/storage/migrations/NNN_copy_rule_definitions.sql` (new),
`tests/storage/test_rule_copy_migration.py` (new)

- `RuleEngine`: `self.rule_manager = RuleDefinitionManager(db)`; `_load_rules`
  (`core.py:471-494`) → `list_by_event`; row type becomes `RuleDefinitionRow`
  (propagate hints through `engine/effects.py`, `workflows/selectors.py:130`,
  `hooks.py` rule tuples).
- **Row-type import closure**: two more production modules import
  `WorkflowDefinitionRow` from the legacy storage module and must be retyped
  here, or 7.1's deletion of that module breaks the tree and 7.2's token audit
  stays red. `workflows/engine/evaluation.py` imports it at `:12` and annotates
  `EvaluationMixin` with it at `:96`, `:207`, and `:229` (the last as
  `list[tuple[WorkflowDefinitionRow, RuleDefinitionBody]]`);
  `workflows/reserved_variables.py` imports it at `:6` and types
  `is_internal_rule(row)` with it at `:39`. Both take `RuleDefinitionRow`.
  Closure is established by running **both** `gcode imports` on each candidate
  file and an exact `gcode grep` for the symbol, then assigning every hit to a
  deliverable or to the 7.2 allowlist — an earlier review of this plan
  dismissed `evaluation.py` on an unverified reading and the module is a
  genuine consumer, so the grep is not optional.
- `sync_rules.py`: typed manager; DELETE the self-healing `workflow_type`
  UPDATE (:113-127) — impossible by construction;
  `_has_gobby_rule_name_collision` → manager query.
- Register `clear_active_rule_names_cache` as a `"rules"` revision listener in
  `session_activation.py` (replaces the bump's hardcoded import).
- Rule surfaces swap managers internally (`_rules.py`, `routes/rules.py`,
  `cli/rules.py`, persona/session-start rule loads).
- Copy migration: guarded; dup-check; copy `workflow_type='rule'` rows
  (priority, sources, tags intact; source normalized); targetless `ON CONFLICT
  DO NOTHING` per the Constraints conflict-target rule; count validation and
  the Constraints equivalence guard, including `target.id = source.id`.
  `tests/storage/test_rule_copy_migration.py` covers all six cases in full,
  restated here rather than referenced, because an expansion agent receives
  only this deliverable section and cannot read Constraints: (1) first run;
  (2) rerun over already-migrated live rows; (3) rerun over already-migrated
  soft-deleted rows; (4) two soft-deleted rule rows sharing a natural key;
  (5) a pre-existing typed row with a divergent payload — loud failure;
  (6) a pre-existing live typed row with the same natural key and payload but
  a different UUID — loud failure.

**Acceptance:**

- 4.1.1 - Rule evaluation loads through the typed rule manager with event/group filtering and priority order. symbol: `RuleEngine._load_rules`. file: `src/gobby/workflows/engine/core.py`.
- 4.1.2 - Bundled rule sync writes the typed table and the self-heal UPDATE is gone. file: `src/gobby/workflows/sync_rules.py`.
- 4.1.3 - Rule mutations invalidate the active-rule-names cache via the rules revision listener. file: `src/gobby/hooks/session_activation.py`.
- 4.1.4 - Copy migration migrates 160+ rules including soft-deleted rows with counts validated. test: `tests/storage/test_rule_copy_migration.py`.
- 4.1.5 - Rule HTTP routes behave identically on the typed manager. file: `src/gobby/servers/routes/rules.py`. test: `tests/servers/routes/test_rules.py`.
- 4.1.5a - Rule MCP tools and the rules CLI behave identically on the typed manager. file: `src/gobby/mcp_proxy/tools/workflows/_rules.py`. file: `src/gobby/cli/rules.py`.
- 4.1.5b - The rule row type propagates as RuleDefinitionRow through effects, selectors, and the hook rule tuples. file: `src/gobby/workflows/engine/effects.py`. file: `src/gobby/workflows/selectors.py`. file: `src/gobby/workflows/hooks.py`.
- 4.1.6 - The equivalence guard fails when a pre-existing typed rule row diverges from its legacy source. test: `tests/storage/test_rule_copy_migration.py`.
- 4.1.7 - Rerunning the rule copy over already-migrated soft-deleted rows completes without a primary-key abort. test: `tests/storage/test_rule_copy_migration.py::test_rerun_over_soft_deleted_rows`.
- 4.1.8 - Rerunning the rule copy over already-migrated live rows is a clean no-op, and two soft-deleted rule rows sharing a natural key both migrate. test: `tests/storage/test_rule_copy_migration.py::test_rerun_over_live_rows`.
- 4.1.9 - A live typed rule row matching a legacy row on natural key and payload but carrying a different UUID fails the guard loudly. test: `tests/storage/test_rule_copy_migration.py::test_divergent_identity_fails`.
- 4.1.10 - EvaluationMixin and is_internal_rule accept RuleDefinitionRow, and no rule-path module imports WorkflowDefinitionRow. file: `src/gobby/workflows/engine/evaluation.py`. file: `src/gobby/workflows/reserved_variables.py`.

### 4.2 Variables cutover and copy migration [category: code] (depends: 4.1)
`kind: deliverable`

Targets: `src/gobby/workflows/variable_defaults.py` (new),
`src/gobby/workflows/state_manager.py`, `src/gobby/workflows/hooks.py`,
`src/gobby/hooks/event_handlers/_session_start/agents.py`,
`src/gobby/mcp_proxy/tools/apply_persona.py`,
`src/gobby/workflows/sync_variables.py`,
`src/gobby/mcp_proxy/tools/workflows/_variables.py`,
`src/gobby/storage/migrations/NNN_copy_session_variable_defaults.sql` (new),
`tests/storage/test_variable_copy_migration.py` (new)

- New `variable_defaults.py`: `load_variable_defaults(db, project_id=None) ->
  dict[str, Any]` on `SessionVariableDefaultManager.get_defaults_map`.
  **Unify all four application paths on it** and drop the `source='installed'`
  filter inconsistency: `SessionVariableManager._get_variable_defaults`
  (`state_manager.py:227-256`; keep TTL cache, add variables-revision
  invalidation), `workflows/hooks.py:705-750` lazy backfill (replace the
  inline loop; keep the `_variable_defaults_loaded` sentinel),
  `_session_start/agents.py:211-225`, `apply_persona.py:48-88`.
- `sync_variables.py`: write typed columns (`name`, `default_value`,
  `description`); variable-definition MCP tools (`_variables.py`
  list/get/create/update/delete/export) move to typed fields.
- Copy migration: guarded; copy `workflow_type='variable'` rows into typed
  columns (`name = COALESCE(definition_json->>'variable', name)`,
  `default_value = definition_json->'value'`); normalize the `source='gobby'`
  anomaly to `'installed'` (matches its bundled YAML; becomes sync-managed and
  reader-visible again); dup-check, targetless `ON CONFLICT DO NOTHING` per the
  Constraints conflict-target rule, count validation, and the Constraints
  equivalence guard including `target.id = source.id`.
  `tests/storage/test_variable_copy_migration.py` covers all six cases in full,
  restated here rather than referenced, because an expansion agent receives
  only this deliverable section: (1) first run; (2) rerun over already-migrated
  live rows; (3) rerun over already-migrated soft-deleted rows; (4) two
  soft-deleted variable rows sharing a natural key; (5) a pre-existing typed row
  with a divergent payload — loud failure; (6) a pre-existing live typed row
  with the same natural key and payload but a different UUID — loud failure.

**Acceptance:**

- 4.2.1 - One helper feeds all four default-application paths with identical visibility. symbol: `load_variable_defaults`. file: `src/gobby/workflows/variable_defaults.py`.
- 4.2.2 - The session-variables TTL cache invalidates on the variables domain revision. file: `src/gobby/workflows/state_manager.py`.
- 4.2.3 - Variable sync writes typed columns. file: `src/gobby/workflows/sync_variables.py`.
- 4.2.3a - Variable-definition MCP CRUD reads and writes typed columns. file: `src/gobby/mcp_proxy/tools/workflows/_variables.py`.
- 4.2.4 - Copy migration lands 42 variable rows including the normalized source anomaly. test: `tests/storage/test_variable_copy_migration.py`.
- 4.2.5 - The equivalence guard fails when a pre-existing typed variable row diverges from its legacy source. test: `tests/storage/test_variable_copy_migration.py`.
- 4.2.6 - Rerunning the variable copy over already-migrated soft-deleted rows completes without a primary-key abort. test: `tests/storage/test_variable_copy_migration.py::test_rerun_over_soft_deleted_rows`.
- 4.2.7 - Rerunning the variable copy over already-migrated live rows is a clean no-op, and two soft-deleted variable rows sharing a natural key both migrate. test: `tests/storage/test_variable_copy_migration.py::test_rerun_over_live_rows`.
- 4.2.8 - A live typed variable row matching a legacy row on natural key and payload but carrying a different UUID fails the guard loudly. test: `tests/storage/test_variable_copy_migration.py::test_divergent_identity_fails`.

### 4.3 Pipelines cutover and copy migration [category: code] (depends: P3)
`kind: deliverable`

Targets: `src/gobby/workflows/pipeline_loader.py` (new),
`src/gobby/workflows/loader.py` (deleted),
`src/gobby/workflows/loader_discovery.py` (deleted),
`src/gobby/workflows/loader_sync.py`, `src/gobby/workflows/loader_cache.py`,
`src/gobby/workflows/dry_run.py`, `src/gobby/workflows/sync_pipelines.py`,
`src/gobby/workflows/imports.py`,
`src/gobby/workflows/pipeline_executor_steps.py`,
`src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py`,
`src/gobby/mcp_proxy/tools/workflows/_pipeline_discovery.py`,
`src/gobby/mcp_proxy/tools/workflows/_pipelines.py`,
`src/gobby/mcp_proxy/tools/workflows/_query.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`,
`src/gobby/servers/routes/pipelines.py`, `src/gobby/scheduler/executor.py`,
`src/gobby/dispatch/stage_pipeline.py`, `src/gobby/cli/pipelines_catalog.py`,
`src/gobby/agents/dry_run.py`, `src/gobby/hooks/factory.py`,
`src/gobby/mcp_proxy/registries.py`, `src/gobby/runner.py`,
`src/gobby/runner_init/orchestration.py`, `src/gobby/app_context.py`,
`src/gobby/mcp_proxy/tools/agents_context.py`,
`src/gobby/mcp_proxy/tools/agents_registry.py`,
`src/gobby/cli/workflows/common.py`,
`src/gobby/mcp_proxy/tools/workflows/__init__.py`,
`src/gobby/mcp_proxy/tools/workflows/_definitions.py`,
`src/gobby/mcp_proxy/tools/workflows/_import.py`,
`src/gobby/storage/migrations/NNN_copy_pipeline_definitions.sql` (new),
`tests/storage/test_pipeline_copy_migration.py` (new),
`tests/workflows/test_pipeline_loader.py` (new)

- New `PipelineLoader` replacing `WorkflowLoader`/discovery: `load_pipeline`
  (extends-chain resolution with cycle detection, row-`enabled` forcing,
  override-conflict detection ported from `loader.py:60-71,112-282`),
  `discover_pipelines`, `validate_pipeline_for_agent`, `clear_cache`; cache
  entries carry `get_definitions_revision("pipelines")` and re-fetch on drift
  (closes the known loader staleness gap). `register_inline_workflow` (zero
  callers) dies. Sync mixin (`loader_sync.py`) retained with a slimmed
  protocol.
- **Deletion closure** (same commit): `loader.py` cannot be deleted while
  anything still imports `WorkflowLoader`, so this task owns every importer, not
  only the method callers. Beyond the call sites below that is the construction
  and injection layer — `hooks/factory.py:36,74,98,523`,
  `mcp_proxy/registries.py:33,61`, `runner.py:68,175`,
  `runner_init/orchestration.py:136`, `app_context.py:74`,
  `mcp_proxy/tools/agents_context.py:26,44`,
  `mcp_proxy/tools/agents_registry.py:31,46`, and
  `cli/workflows/common.py:10-26` (the CLI factory, whose group 6.1 deletes
  later but which still imports at this commit) — plus the type-annotation and
  docstring holders in `mcp_proxy/tools/workflows/__init__.py:61,104,135`,
  `_definitions.py:19,60,137,244,302`, and `_import.py:17,23,131`. Retype each
  to `PipelineLoader` here; the ones later deleted in 5.2 and 6.1 simply stop
  existing then. Sweep the test seams (`tests/workflows/test_workflow_variables.py`
  and its siblings that construct a loader fixture) in the same commit.
- Rewire callers: `pipeline_executor_steps.py:188,284`,
  `_pipeline_execution.py:334,428,659`, `_pipeline_discovery.py:27`,
  `routes/pipelines.py:299`, `scheduler/executor.py:379`,
  `dispatch/stage_pipeline.py:81`, `cli/pipelines_catalog.py:32,72,163`,
  `_pipelines.py:174,537-579` (dynamic `pipeline:<name>` tools),
  `_query.py:44`, `_factory.py:411-426` (spawn `workflow` param → pipeline
  lookup only), `agents/dry_run.py:181`.
- `dry_run.py`: `evaluate_workflow` → `evaluate_pipeline_definition`; the
  step-workflow fallback branch dies (agent steps evaluate only via
  `evaluate_agent_definition`). The `_build_step_trace`/`_build_lifecycle_path`
  extraction into `workflows/dry_run_trace.py` already happened in 2.4; this
  task consumes it and does not repeat it. Retire the now-unowned
  `WorkflowEvaluation.workflow_type` field (`dry_run.py:101,111,189,200,264`) —
  the audit in 7.2 matches that token, and no other deliverable removes it.
- `sync_pipelines.py` → typed manager (root `dev.yaml`/`qa.yaml`/`review.yaml`
  scan preserved); `imports.py::sync_imported_definition` dispatches per-kind
  to typed managers and refuses kind changes by table instead of stored type.
- Copy migration: guarded; copy `workflow_type='pipeline'` rows (version,
  canvas_json); dup-check; targetless `ON CONFLICT DO NOTHING` per the
  Constraints conflict-target rule; count validation; the Constraints
  equivalence guard including `target.id = source.id`.
  `tests/storage/test_pipeline_copy_migration.py` covers all six cases in full,
  restated here rather than referenced, because an expansion agent receives
  only this deliverable section: (1) first run; (2) rerun over already-migrated
  live rows; (3) rerun over already-migrated soft-deleted rows; (4) two
  soft-deleted pipeline rows sharing a natural key; (5) a pre-existing typed row
  with a divergent payload — loud failure; (6) a pre-existing live typed row
  with the same natural key and payload but a different UUID — loud failure.

**Acceptance:**

- 4.3.1 - PipelineLoader serves load/discover/validate with extends resolution and a revision-aware cache. symbol: `PipelineLoader`. file: `src/gobby/workflows/pipeline_loader.py`.
- 4.3.2 - loader.py and loader_discovery.py are deleted and no source or test module imports WorkflowLoader. file: `src/gobby/workflows/loader.py`. file: `src/gobby/workflows/loader_discovery.py`.
- 4.3.3 - Pipeline dry-run is pipeline-only and agent dry-run is unchanged. symbol: `evaluate_pipeline_definition`. file: `src/gobby/workflows/dry_run.py`.
- 4.3.9 - The construction and injection layer types the loader as PipelineLoader, so the tree imports at this commit. file: `src/gobby/hooks/factory.py`. file: `src/gobby/mcp_proxy/registries.py`. file: `src/gobby/runner_init/orchestration.py`.
- 4.3.10 - WorkflowEvaluation no longer carries workflow_type. symbol: `WorkflowEvaluation`. file: `src/gobby/workflows/dry_run.py`.
- 4.3.4 - Copy migration lands 11 pipelines with counts validated. test: `tests/storage/test_pipeline_copy_migration.py`.
- 4.3.5 - Dynamic pipeline MCP tool exposure and stage/scheduler execution load through the typed manager. test: `tests/workflows/test_pipeline_loader.py`.
- 4.3.6 - The equivalence guard fails when a pre-existing typed pipeline row diverges from its legacy source. test: `tests/storage/test_pipeline_copy_migration.py`.
- 4.3.8 - Rerunning the pipeline copy over already-migrated soft-deleted rows completes without a primary-key abort. test: `tests/storage/test_pipeline_copy_migration.py::test_rerun_over_soft_deleted_rows`.
- 4.3.11 - Rerunning the pipeline copy over already-migrated live rows is a clean no-op, and two soft-deleted pipeline rows sharing a natural key both migrate. test: `tests/storage/test_pipeline_copy_migration.py::test_rerun_over_live_rows`.
- 4.3.12 - A live typed pipeline row matching a legacy row on natural key and payload but carrying a different UUID fails the guard loudly. test: `tests/storage/test_pipeline_copy_migration.py::test_divergent_identity_fails`.
- 4.3.7 - Pipeline sync and per-kind import dispatch write the typed tables and refuse a kind change by target table. file: `src/gobby/workflows/imports.py`. file: `src/gobby/workflows/sync_pipelines.py`.

## P5: HTTP and MCP Surfaces
`kind: framing`

**Goal**: the generic `/api/workflows` and generic MCP definition CRUD are
gone; domain surfaces exist for everything the UI and agents need.

### 5.1 HTTP surface rebuild [category: code] (depends: P3, P4)
`kind: deliverable`

Targets: `src/gobby/servers/routes/workflows.py` (deleted),
`src/gobby/servers/routes/pipeline_definitions.py` (new),
`src/gobby/servers/routes/variable_definitions.py` (new),
`src/gobby/servers/_app_routes.py`, `src/gobby/servers/routes/__init__.py`,
`src/gobby/workflows/template_hashes.py`,
`src/gobby/servers/routes/sessions/variables.py` (new),
`src/gobby/servers/routes/sessions/__init__.py`,
`src/gobby/mcp_proxy/stdio_proxy.py`,
`src/gobby/servers/middleware/project_context.py`,
`tests/servers/routes/test_pipeline_definitions.py` (new),
`tests/servers/routes/test_variable_definitions.py` (new),
`tests/servers/routes/test_session_variables.py` (new),
`tests/mcp_proxy/test_stdio_proxy.py`,
`tests/servers/routes/test_workflows.py` (deleted)

- DELETE the 16-route generic router and its registration.
- New `/api/pipelines/definitions` router: list (enabled/include_deleted
  filters + template-drift annotation), `GET /templates`, get, create, update,
  delete, toggle, duplicate, import (YAML), export, restore,
  restore-from-template, move-to-project, move-to-global. **Registration-order
  hazard**: mount BEFORE `create_pipelines_router` — `pipelines.py` has
  `GET /{execution_id}` which would shadow `/definitions`; pin with a comment
  and a route-order test.
- New `/api/variables` router: list, create, update, delete, toggle,
  restore-from-template.
- Relocate runtime variable endpoints (`workflows.py:386-450`) to the sessions
  router family as `POST /api/sessions/{session_id}/variables/get|set` with
  `scope: "session" | "step"` (no `workflow` field). `servers/routes/sessions`
  is a **package**, not a module — the endpoints land in a new
  `sessions/variables.py` registered from `sessions/__init__.py` alongside
  `core.py`, `lifecycle.py`, and the rest.
- The daemon-proxy client of the old endpoints is `mcp_proxy/stdio_proxy.py`
  (the `/api/workflows/variables/set|get` literals at `:462` and `:476`), not
  `mcp_proxy/stdio.py`; retarget it at the relocated paths in the same commit.
  Its focused client tests are `tests/mcp_proxy/test_stdio_proxy.py`, which
  asserts the old literal at `:63` — that suite is the seam that proves
  `DaemonProxy` get/set call the relocated path with the `scope` payload, and a
  server-route test cannot prove it because it never exercises the client.
  Both are required: the route test for endpoint behavior, the client test for
  the call the client actually makes.
  `servers/middleware/project_context.py:10` names the old route in the comment
  that documents why the middleware exists — update it here, since 7.2's audit
  matches `/api/workflows` and no other deliverable owns that line.
- **Deleted-surface test closure**: deleting the router in this commit breaks
  `tests/servers/routes/test_workflows.py`, which exercises all sixteen routes
  (`/api/workflows` literals at `:77` through `:400`). It is deleted here, not
  in P7. Deferring it would leave the phase's own prescribed focused test run
  red for two phases, which contradicts the working-daemon-per-commit rule.
  Behavior worth keeping — the variables get/set cases at `:379-400` — moves
  into `test_session_variables.py` rather than being dropped.
- `template_hashes.py`: key by `kind` instead of `workflow_type` (same five
  loaders); consumers are the domain list routes and restore-from-template
  handlers.

**Acceptance:**

- 5.1.1 - The generic workflows router no longer exists and nothing registers it. file: `src/gobby/servers/_app_routes.py`.
- 5.1.2 - Pipeline definition routes cover the full UI demand set and mount before the execution router. file: `src/gobby/servers/routes/pipeline_definitions.py`.
- 5.1.3 - Variable definition routes cover the settings-editor demand set. file: `src/gobby/servers/routes/variable_definitions.py`.
- 5.1.4 - Session variable get/set live under the sessions API with scope semantics. file: `src/gobby/servers/routes/sessions/variables.py`. test: `tests/servers/routes/test_session_variables.py`.
- 5.1.5 - Template drift annotation works per domain through the re-keyed cache. symbol: `TemplateHashCache`. file: `src/gobby/workflows/template_hashes.py`.
- 5.1.6 - The daemon-proxy client calls the relocated session-variable endpoints with scope, proven at the client seam, and no /api/workflows literal remains in it. file: `src/gobby/mcp_proxy/stdio_proxy.py`. test: `tests/mcp_proxy/test_stdio_proxy.py`.
- 5.1.7 - No /api/workflows reference remains in the project-context middleware. file: `src/gobby/servers/middleware/project_context.py`.
- 5.1.8 - The generic workflows route suite is deleted in this commit and its variables get/set coverage survives under the sessions API. test: `tests/servers/routes/test_workflows.py`. test: `tests/servers/routes/test_session_variables.py`.

### 5.2 MCP surface prune and re-scope [category: code] (depends: P3, P4)
`kind: deliverable`

Targets: `src/gobby/mcp_proxy/tools/workflows/__init__.py`,
`src/gobby/mcp_proxy/tools/workflows/_definitions.py` (deleted),
`src/gobby/mcp_proxy/tools/workflows/_query.py`,
`src/gobby/mcp_proxy/tools/workflows/_import.py`,
`src/gobby/mcp_proxy/tools/workflows/_auto_export.py`,
`src/gobby/mcp_proxy/tools/workflows/_agents.py`,
`src/gobby/mcp_proxy/tools/workflows/_rules.py`,
`src/gobby/mcp_proxy/tools/workflows/_variables.py`,
`src/gobby/sync_registry.py` (new),
`src/gobby/cli/installers/shared.py`, `src/gobby/cli/install_setup.py`,
`src/gobby/cli/sync.py`, `src/gobby/runner_init/storage.py`,
`tests/mcp_proxy/tools/test_workflow_crud.py` (deleted),
`tests/mcp_proxy/tools/workflows/test_import.py`,
`tests/mcp_proxy/tools/workflows/test_project_scope.py`,
`tests/mcp_proxy/tools/workflows/test_query.py`

Server name stays `gobby-workflows` (bundled pipelines/skills reference it).
Disposition: DELETE `create_workflow`, `update_workflow`, `delete_workflow`,
`export_workflow`, `restore_workflow`, `get_workflow`, `list_workflows`,
`import_workflow` (domain CRUD already exists for all four kinds). RE-SCOPE
`get_workflow_status` → `get_step_status` (3.3) and `evaluate_workflow` →
`evaluate_pipeline` + new `evaluate_agent` (wraps `resolve_agent` +
`evaluate_agent_definition`). KEEP `reload_cache`, reimplemented over the new
sync registry.

- New `src/gobby/sync_registry.py`: the single bundled-sync fan-out
  (`SYNC_TARGETS` + `sync_bundled_content_to_db(db, *, only=None,
  skip_types=None)`), extracted from `cli/installers/shared.py:241-317`;
  `installers/shared.py` delegates (keeps `_sync_user_templates_to_db`);
  `_import.py::reload_cache` calls it with
  `only={"rules","agents","pipelines","variables","detection_manifests"}` and
  clears `PipelineLoader` cache; the third copy of the fan-out dies with the
  CLI group in 6.1.
- `_auto_export.py`: `auto_export_definition`/`auto_delete_definition` take an
  explicit `kind: Literal["rule","variable","agent","pipeline"]` from each
  caller; `has_gobby_name_collision` becomes a per-domain manager query.
- **Deleted-tool test closure**, in this commit rather than P7, for the same
  working-commit reason as 5.1: `tests/mcp_proxy/tools/test_workflow_crud.py`
  imports the deleted `_definitions` module at `:7` and asserts
  `create_workflow` is still registered (`:591`, `:595`, `:617`, `:738`) — the
  exact opposite of this task's disposition — so it is deleted.
  `workflows/test_import.py` calls `import_workflow` and `get_workflow`
  (`:92-225`) and is retargeted at the per-kind import path;
  `workflows/test_project_scope.py` calls `get_workflow` and `list_workflows`
  with `workflow_type` filters (`:80`, `:220-223`) and is retargeted at the
  domain list surfaces, preserving its project-scope assertions;
  `workflows/test_query.py` tests `list_workflows` throughout and is retargeted
  at `get_step_status`, keeping the DB/filesystem-merge cases that still apply
  to pipeline discovery. Retarget preserves domain behavior; only assertions
  that a deleted generic tool exists are dropped.

**Acceptance:**

- 5.2.1 - Generic definition CRUD tools are gone from the registry; domain tools remain. file: `src/gobby/mcp_proxy/tools/workflows/__init__.py`.
- 5.2.2 - evaluate_pipeline and evaluate_agent expose the complete dry-run story. test: `tests/mcp_proxy/tools/workflows/test_registry_surface.py::test_evaluate_tools_cover_pipeline_and_agent`.
- 5.2.3 - get_step_status is registered under its new name and reports the snapshot step list for a session. symbol: `get_step_status`. file: `src/gobby/mcp_proxy/tools/workflows/_query.py`.
- 5.2.4 - One sync registry feeds install, reload_cache, and CLI sync. symbol: `sync_bundled_content_to_db`. file: `src/gobby/sync_registry.py`.
- 5.2.5 - Auto-export dispatches on explicit kind with per-domain collision checks. file: `src/gobby/mcp_proxy/tools/workflows/_auto_export.py`.
- 5.2.5a - Every auto-export caller passes its kind explicitly. file: `src/gobby/mcp_proxy/tools/workflows/_agents.py`. file: `src/gobby/mcp_proxy/tools/workflows/_rules.py`. file: `src/gobby/mcp_proxy/tools/workflows/_variables.py`.
- 5.2.6 - Registry tool inventory and schemas match the disposition table. test: `tests/mcp_proxy/tools/workflows/test_registry_surface.py`.
- 5.2.7 - The generic-CRUD suite is deleted and the import, project-scope, and query suites are retargeted at surviving tools with their domain assertions intact. test: `tests/mcp_proxy/tools/test_workflow_crud.py`. test: `tests/mcp_proxy/tools/workflows/test_import.py`. test: `tests/mcp_proxy/tools/workflows/test_project_scope.py`. test: `tests/mcp_proxy/tools/workflows/test_query.py`.

## P6: CLI and Web UI
`kind: framing`

**Goal**: user-facing surfaces speak domains; no `workflow_type` in public
vocabulary.

### 6.1 CLI restructure [category: code] (depends: P5)
`kind: deliverable`

Targets: `src/gobby/cli/workflows/` (deleted), `src/gobby/cli/__init__.py`,
`src/gobby/cli/agents.py`, `src/gobby/cli/pipelines_catalog.py`,
`src/gobby/cli/sync.py`, `src/gobby/cli/export_import.py`,
`src/gobby/cli/variables.py` (new), `src/gobby/workflows/imports.py`,
`src/gobby/mcp_proxy/tools/workflows/_import.py`

- DELETE the `gobby workflows` group (list/show/status/check/audit/import/
  reload/reinstall — including the raw `DELETE FROM workflow_definitions`
  statements and the legacy `"workflow"→pipelines` alias map in
  `manage.py:93-99`). Replacements: `gobby agents steps [--session REF]`
  (reads `agent_step_instances`), `gobby agents check <name>` (wraps
  `evaluate_agent_definition`), `gobby pipelines show <name>` and
  `gobby pipelines check <name>` (catalog + `evaluate_pipeline_definition`),
  `gobby sync --reinstall [rules|agents|pipelines|variables|all]` (typed
  managers via the sync registry). If `cli/agents.py` (857 lines) approaches
  the cap, put the new subcommands in `cli/agents_steps.py`.
- `gobby export/import` (`export_import.py`): public resource type `workflow`
  → `pipeline`; validation via `PipelineDefinition` only.
- `cli/workflows/variables.py` (set-var/get-var) → `gobby variables get|set
  --session` in new `cli/variables.py` with the scope model.
- `imports.py::sync_imported_workflows`: glob per-kind subdirectories
  `.gobby/workflows/{rules,agents,pipelines,variables}/*.yaml` (symmetry with
  auto-export); drop the root-only glob.

**Acceptance:**

- 6.1.1 - The gobby workflows group is gone and per-domain replacements exist. file: `src/gobby/cli/__init__.py`.
- 6.1.2 - Reinstall runs per-domain through the sync registry with no raw legacy SQL. file: `src/gobby/cli/sync.py`.
- 6.1.3 - Export/import public vocabulary uses pipeline instead of workflow. file: `src/gobby/cli/export_import.py`.
- 6.1.4 - Filesystem imports cover the per-kind directories. symbol: `sync_imported_workflows`. file: `src/gobby/workflows/imports.py`.
- 6.1.5 - New CLI subcommands are covered by focused tests. test: `tests/cli/test_agents_steps.py`.
- 6.1.6 - `gobby variables get|set --session` reads and writes both scopes and replaces the deleted set-var/get-var commands. file: `src/gobby/cli/variables.py`.

### 6.2 Web UI migration [category: code] (depends: P5)
`kind: deliverable`

Targets: `web/src/hooks/useWorkflows.ts` (deleted),
`web/src/hooks/usePipelineDefs.ts` (new),
`web/src/hooks/useVariableDefs.ts` (new),
`web/src/components/activity/pipelines/PipelinesDefsActions.ts`,
`web/src/components/activity/pipelines/PipelineEditor.types.ts`,
`web/src/components/settings/WorkflowVariablesEditor.tsx` (renamed
`web/src/components/settings/VariableDefaultsEditor.tsx`),
`web/src/components/settings/sections/AutomationWorkflowsSection.tsx`,
`web/src/components/activity/agents/AgentsTabData.ts`

- Replace `useWorkflows.ts` with `usePipelineDefs.ts`
  (`/api/pipelines/definitions`, `WorkflowDetail` → `PipelineDefDetail`, no
  `workflow_type`) and `useVariableDefs.ts` (`/api/variables`).
- `PipelinesDefsActions.ts` + editors → new endpoints/types.
- `WorkflowVariablesEditor.tsx` → `VariableDefaultsEditor.tsx` on
  `useVariableDefs`; update section references.
- `AgentsTabData.ts::loadPipelineList` → `/api/pipelines/definitions` and fix
  the latent bug: read `data.definitions` (it reads `data.workflows`, which is
  always undefined, so the agent editor's pipeline picker is empty today).
- Retarget tests: `PipelinesDefs.test.tsx`, `PipelineEditor.test.tsx`,
  `WorkflowVariablesEditor.test.tsx`, `AgentsTab.test.tsx`,
  `useFilteredRefetches.test.ts`, `useSelectionFetchRaces.test.ts`,
  `AutomationWorkflowsSection.test.tsx`.

**Acceptance:**

- 6.2.1 - No web code references /api/workflows or workflow_type. file: `web/src/hooks/usePipelineDefs.ts`.
- 6.2.2 - Pipeline definitions UI performs full CRUD against the domain routes. file: `web/src/components/activity/pipelines/PipelinesDefsActions.ts`.
- 6.2.3 - Variable defaults editor works against /api/variables under its new name. file: `web/src/components/settings/VariableDefaultsEditor.tsx`.
- 6.2.4 - The agent editor's pipeline picker is populated (data.definitions bug fixed). file: `web/src/components/activity/agents/AgentsTabData.ts`.
- 6.2.5 - The retargeted pipeline-definition and editor suites pass. test: `web/src/components/activity/pipelines/__tests__/PipelinesDefs.test.tsx`. test: `web/src/components/activity/pipelines/__tests__/PipelineEditor.test.tsx`.
- 6.2.6 - The retargeted settings and agents-tab suites pass. test: `web/src/components/settings/__tests__/WorkflowVariablesEditor.test.tsx`. test: `web/src/components/settings/sections/__tests__/AutomationWorkflowsSection.test.tsx`. test: `web/src/components/activity/__tests__/AgentsTab.test.tsx`.
- 6.2.7 - The refetch and selection-race hook suites pass against the new hooks. test: `web/src/hooks/__tests__/useFilteredRefetches.test.ts`. test: `web/src/hooks/__tests__/useSelectionFetchRaces.test.ts`.

## P7: Legacy Removal, Audit, Documentation
`kind: framing`

**Goal**: legacy storage is physically gone, a standing audit prevents
regression, docs describe the final state.

### 7.1 Drop migration and legacy module deletion [category: code] (depends: P6)
`kind: deliverable`

Targets: `src/gobby/storage/migrations/NNN_drop_legacy_workflow_tables.sql`
(new), `src/gobby/storage/postgres_baseline_schema.sql`,
`src/gobby/storage/workflow_definitions.py` (deleted),
`src/gobby/storage/definitions/_shared.py`,
`src/gobby/cli/tasks/_utils/claims.py`,
`src/gobby/sessions/lifecycle.py`,
`src/gobby/workflows/template_hashes.py`,
`src/gobby/storage/skills/_metadata.py`,
`tests/storage/test_migration_contract.py`,
`tests/storage/test_drop_legacy_migration.py` (new)

- Rewrite `get_claimed_task_owners` (`claims.py:11-88`): source `session_task`
  from the session-variables store (join `sessions.status='active'`) instead
  of `workflow_states.variables`; preserve the `#N`/UUID/prefix resolution and
  wildcard/list handling.
- `src/gobby/sessions/lifecycle.py:347` calls
  `_purge_soft_deleted_definitions`, which imports
  `LocalWorkflowDefinitionManager` and calls `purge_deleted(older_than_days=30)`.
  Despite living on the session lifecycle scheduler this job is a
  **definition** purge, not a session or runtime-instance sweep: it hard-deletes
  definitions soft-deleted more than 30 days ago. Replace the single legacy call
  with a fan-out over the four typed parent managers' `purge_deleted` (1.2);
  `agent_step_workflows` needs no entry because its FK onto `agent_definitions`
  cascades. Step instances are session-owned and never appear here —
  `agent_step_instances.session_id` cascades on session delete, and per-agent-run
  runtime cleanup is `agents/agent_cleanup.py`, which 3.3 owns.
- **Baseline contract test**: 1.1 taught `tests/storage/test_migration_contract.py`
  to assert the baseline still contains the legacy tables, and its UUID identity
  inventory includes `workflow_instances`. Removing those objects from the
  baseline here without updating that test makes the final focused storage run
  fail even when the drop migration is correct. Delete only the legacy identity
  and presence expectations; the six typed-table catalog-equivalence checks added
  in 1.1.4 stay and are what keeps the two lineages pinned after the drop.
- **Residual audited tokens with no other owner**: `workflows/template_hashes.py:20`
  imports from `storage/workflow_definitions.py` and must lose that import in the
  same commit the module is deleted (5.1 re-keys the cache by `kind`; it does not
  touch the import). `src/gobby/storage/skills/_metadata.py:250` names the
  `workflow_definitions` pattern in a docstring describing installed-copy
  precedence — reword it. Both are matched by 7.2's audit.
- Drop migration (guarded): **backstop first**, and the backstop re-runs each
  domain's normalized **payload equivalence** at drop time rather than checking
  key presence. It covers **every non-generated legacy row, live and
  soft-deleted**, not just live ones — matching live rows on the live natural
  key and soft-deleted rows on the preserved `id`, the same asymmetry the copy
  guards use, and requiring `target.id = source.id` in both cases. Restricting
  the backstop to live rows would be unsound because the copy migrations
  deliberately include soft-deleted rows so a restore keeps its payload, and
  the generic legacy CRUD surfaces survive until P5: a definition created after
  its domain's copy ran and then soft-deleted before P5 is invisible to a
  live-only check, so the drop takes its restorable payload with it. For every
  such row require exactly one typed match whose payload equals the source
  after that domain's documented normalization, and RAISE with the offending
  ids and names otherwise; explicitly exclude the generated
  `workflow_type='workflow'` rows, which are never copied. A presence-only
  check passes for a legacy row updated after its copy migration ran, and for a
  same-name/different-source row that matches a typed row it has nothing to do
  with — both then get dropped, which is exactly the mid-epic-write data loss
  the backstop exists to prevent. Then
  `DROP TABLE workflow_instances`, `DROP TABLE workflow_definitions`,
  `DROP TABLE workflow_states`.
- Remove all three tables from the baseline.
- Delete `src/gobby/storage/workflow_definitions.py` (row, manager, global
  revision helpers — `compute_definition_hash` already lives in
  `src/gobby/storage/definitions/_shared.py`); sweep remaining test imports (~45 files;
  most already rewritten in P1–P6 — this task deletes stragglers, rewriting
  tests that still seed `workflow_definitions` fixtures such as
  `tests/agents/test_merge_orchestrator_contract.py:119` and
  `tests/storage/tasks/test_stage_registry_default_agent_fk.py:30`).

**Acceptance:**

- 7.1.1 - Claims lookup reads session variables and workflow_states has no reader. symbol: `get_claimed_task_owners`. file: `src/gobby/cli/tasks/_utils/claims.py`.
- 7.1.2 - The drop migration backstop refuses to drop when a live legacy row was updated after its copy, and when a same-name/different-source row has no true typed counterpart. test: `tests/storage/test_drop_legacy_migration.py`.
- 7.1.2a - The backstop also covers soft-deleted legacy rows by preserved id, refusing to drop a definition created after its copy migration and soft-deleted before P5. test: `tests/storage/test_drop_legacy_migration.py::test_backstop_covers_soft_deleted_rows`.
- 7.1.3 - All three legacy tables are gone from the baseline and the live schema after migration. file: `src/gobby/storage/postgres_baseline_schema.sql`.
- 7.1.4 - storage/workflow_definitions.py is deleted and no source or test imports it. file: `src/gobby/storage/definitions/_shared.py`.
- 7.1.5 - The scheduled soft-deleted-definition purge drops the legacy manager import and fans out over the four typed parent managers, with agent step-workflow children removed by cascade and no step-instance branch. symbol: `_purge_soft_deleted_definitions`. file: `src/gobby/sessions/lifecycle.py`.
- 7.1.6 - The migration contract test drops its legacy identity and presence expectations while retaining the six typed-table catalog-equivalence checks. test: `tests/storage/test_migration_contract.py`.
- 7.1.7 - Template hashing and the skills metadata docstring carry no legacy storage reference. file: `src/gobby/workflows/template_hashes.py`. file: `src/gobby/storage/skills/_metadata.py`.

### 7.2 Legacy-reference audit test [category: test] (depends: 7.1, 7.3)
`kind: deliverable`

Targets: `tests/audit/test_legacy_workflow_storage_removed.py` (new),
`src/gobby/storage/postgres_baseline_schema.sql`,
`src/gobby/install/shared/`

Grep-style pytest failing on word-boundary occurrences of
`workflow_definitions`, `workflow_instances`, `workflow_states`,
`LocalWorkflowDefinitionManager`, `WorkflowDefinitionRow`, `workflow_type`,
`register_agent_step_workflow`, `_step_workflow_name`, `/api/workflows`, and
`-steps` name-derivation patterns. Scope: `src/gobby/**/*.py` excluding
`storage/migrations/` (historical migrations legitimately name the dropped
tables), `web/src/**/*.{ts,tsx}`, the live baseline schema
`src/gobby/storage/postgres_baseline_schema.sql`, and the current bundled
YAML/skill/prompt sources under `src/gobby/install/shared/`. The baseline SQL
and the bundled templates are precisely what 7.1 and 7.3 rewrite, so leaving
them unscanned lets the removal regress exactly where regression is easiest.
`ALLOWLIST` is a list of exact `(path, token, reason)` triples; the audit fails
both when a non-allowlisted occurrence appears **and** when an allowlisted
occurrence no longer exists, so exceptions stay narrow and self-prune instead of
outliving their reason. Additionally assert no bundled agent YAML contains
top-level `steps:`/`step_variables:`/`exit_condition:` keys (guards the
`extra="ignore"` silent-drop trap that 2.1's validator closes at runtime).

The dependency on 7.3 is load-bearing, not bookkeeping: 7.2 scans the bundled
YAML/skill/prompt sources that 7.3 rewrites, so as siblings the audit lands red
after 7.1 completes and phase-by-phase expansion has no valid ordering.

**Owner inventory.** Every token the audit matches must have an upstream
deliverable that removes it or a justified allowlist entry; an audit written
against an unowned occurrence lands red at the end of the epic with no
deliverable left to fix it. The occurrences that are not obviously owned by the
domain cutover that renames them are assigned as follows: `workflow_states` in
`src/gobby/config/tasks.py` → 7.3; the `workflow_definitions` import in
`src/gobby/workflows/template_hashes.py:20` and the `workflow_definitions`
docstring in `src/gobby/storage/skills/_metadata.py:250` → 7.1; the
`workflow_instances` log key in `src/gobby/agents/agent_cleanup.py:454` → 3.3;
the `/api/workflows` comment in
`src/gobby/servers/middleware/project_context.py:10` → 5.1;
`WorkflowEvaluation.workflow_type` in `src/gobby/workflows/dry_run.py:101` → 4.3;
the `WorkflowDefinitionRow` import and annotations in
`src/gobby/workflows/engine/evaluation.py:12,96,207,229` → 4.1; the same import
and the `is_internal_rule` annotation in
`src/gobby/workflows/reserved_variables.py:6,39` → 4.1; and the `workflow_type`
column and index in
`src/gobby/storage/postgres_baseline_schema.sql:1425,1443` → 7.1.

The audit itself asserts this closure: it enumerates every occurrence present in
the tree at the time it is written, and fails if any of them is neither removed
nor covered by an exact allowlist triple. That turns "we listed the owners" into
a checked property rather than a claim in prose.

**Acceptance:**

- 7.2.1 - The audit fails on any production reference to removed storage and passes on the final tree. test: `tests/audit/test_legacy_workflow_storage_removed.py`.
- 7.2.2 - The audit covers the baseline SQL and bundled YAML/skill/prompt sources, and fails when an allowlist entry no longer matches anything. test: `tests/audit/test_legacy_workflow_storage_removed.py::test_allowlist_is_self_pruning`.
- 7.2.3 - Every occurrence in the owner inventory is either absent from the final tree or covered by an exact allowlist triple, asserted rather than assumed. test: `tests/audit/test_legacy_workflow_storage_removed.py::test_every_preexisting_occurrence_has_an_owner`.

### 7.3 Documentation sweep [category: docs] (depends: 7.1)
`kind: deliverable`

Targets: `docs/guides/agents.md`, `docs/guides/rules.md`,
`docs/guides/variables.md`, `docs/guides/workflows-overview.md`,
`docs/guides/http-endpoints.md`, `docs/guides/pipelines.md`,
`docs/architecture/architecture.md`, `docs/reviews/cli-build-ops.md`,
`docs/audits/configuration-audit.md`,
`docs/plans/workflow-refactor.md` (deleted),
`src/gobby/install/shared/workflows/rules/CLAUDE.md`,
`src/gobby/dispatch/CLAUDE.md`, `src/gobby/config/tasks.py`, bundled skill
docs (e.g. `src/gobby/install/shared/skills/persona/SKILL.md`)

Update guides to the nested `step_workflow` agent shape, domain tables, new
HTTP routes, MCP tool set, and CLI commands. Verify every MCP tool named in
bundled skills (`build-rule`, `pipelines-and-cron`, `persona`, `dev`,
`expand`, `qa`, `review`, `tech-writer` SKILL.md files) and
`install/shared/prompts/chat/system.md` against the 5.2 disposition table and
fix references to removed generics. Verify bundled pipelines
(`expand-task.yaml`, `gobby-merge.yaml`, nightly YAMLs) only use surviving
`gobby-workflows` tools. DELETE `docs/plans/workflow-refactor.md` (superseded
conflicting design). Correct `docs/reviews/cli-build-ops.md:56-60` (claimed
the typed tables already existed). `architecture.md:101`
`WorkflowInstanceManager` → `AgentStepInstanceManager`. Remove the residual
`workflow_states` reference in `src/gobby/config/tasks.py` per the 7.2 owner
inventory.

**Active-doc inventory.** The sweep is scoped by searching the active docs tree
for every removed route, command, table, and discriminator rather than by the
guide list this plan started with. That search adds three artifacts the initial
list missed: `docs/guides/http-endpoints.md` and `docs/guides/pipelines.md` both
document `/api/workflows` or `workflow_type` as current behavior, and
`docs/audits/configuration-audit.md` needs an explicit active-versus-historical
disposition — either updated to the final state, or marked as a dated historical
audit so a future reader does not treat it as current. `src/gobby/workflows/`
has no `CLAUDE.md`; the module guidance that mentions the legacy tables lives in
`src/gobby/dispatch/CLAUDE.md`.

**Acceptance:**

- 7.3.1 - Guides and architecture docs describe the domain-table model and snapshot runtime. file: `docs/guides/workflows-overview.md`. file: `docs/architecture/architecture.md`.
- 7.3.2 - The conflicting prior design doc is deleted and the false review claim corrected. file: `docs/reviews/cli-build-ops.md`.
- 7.3.3 - Bundled skills and prompts reference only surviving MCP tools. file: `src/gobby/install/shared/skills/persona/SKILL.md`.
- 7.3.4 - No audited legacy token remains in config/tasks.py. file: `src/gobby/config/tasks.py`.
- 7.3.5 - The HTTP endpoint and pipelines guides describe the domain routes with no /api/workflows or workflow_type reference. file: `docs/guides/http-endpoints.md`. file: `docs/guides/pipelines.md`.
- 7.3.6 - The configuration audit carries an explicit active-or-historical disposition. file: `docs/audits/configuration-audit.md`.
- 7.3.7 - Module guidance describes the domain tables. file: `src/gobby/dispatch/CLAUDE.md`. file: `src/gobby/install/shared/workflows/rules/CLAUDE.md`.

## E1 End-to-End Verification
`kind: verification`

Per-phase: `uv run gobby restart` runs that phase's migrations; then focused
tests only (`GOBBY_TEST_PROTECT=1 uv run pytest <paths> -v`) — storage
(`tests/storage/definitions/`, per-migration tests), runtime
(`tests/workflows/test_step_instances.py`,
`tests/workflows/test_step_snapshot_semantics.py`), surfaces
(`tests/servers/routes/`, `tests/mcp_proxy/tools/workflows/`), UI (vitest for
the retargeted suites). Epic-final: restore a copy of the live hub, replay all
migrations, confirm **249 definition rows** land in the typed tables — 167
rules (162 live + 5 soft-deleted), 29 agents (28 live + 1 soft-deleted),
42 variables, 11 pipelines — plus **25** `agent_step_workflows` children (every
agent row carrying a non-empty `steps` array), for 274 rows total; the 29
generated `workflow_type='workflow'` rows are not copied. Re-derive these five
counts from the restored hub before asserting them: the legacy tables are still
live and writable until P7, so a sync or a retired bundled agent moves them.
Confirm only live-session instances survive; spawn a stepful agent (e.g. planner) and observe step
enforcement, transitions, and the completion gate from the snapshot; edit the
agent definition mid-run and confirm the run is unaffected while a second
spawn picks up the edit; restart the daemon against a session whose instance
was deleted and confirm the structured fresh-snapshot recovery warning appears
in `~/.gobby/logs/` with the session, agent name, and resolved ids; exercise
the pipelines and variables editors in the web UI; run `gobby sync`,
`gobby agents steps`, `gobby pipelines check`;
`uv run ruff check src/`, `uv run mypy src/`, and the test-types ratchet must
pass; the P7 audit test is the standing regression gate.

## V1 Plan Changelog

`kind: verification`

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
