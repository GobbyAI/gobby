# Stage Manifest Cutover — Implementation Plan for #13482

`plan_kind: implementation` — deliverable manifest emitted by plan-adversary on approval.

## Overview
`kind: framing`

Replace gobby's dual-enum task state model (`status` + `lifecycle` + `lifecycle_stage`) with a registry-backed, tri-state-per-stage manifest model. Every task carries an ordered, task-type-specific manifest of `(stage_name, state)` rows where `state ∈ {needs_doing, in_progress, done}`. The 14-stage registry is bundled YAML synced to a new `task_stages_registry` table. The dispatcher, MCP/HTTP/CLI surfaces, and the web kanban all migrate to the manifest model. Legacy lifecycle/status columns and active status values are dropped in the same epic — no compatibility shims, no shadow model.

This is the implementation companion to the strategy plan at `.gobby/plans/task-13482-lifecycle-status-kanban.md`. The strategy plan defines the target model; this plan defines the executable steps. Read the strategy plan first if you need the *why* — every section here assumes that context.

## Constraints
`kind: framing`

- **Pre-launch clean cutover.** Do not build compatibility facades or long-lived legacy write paths. Callers move to the stage manifest APIs directly within this epic; old `lifecycle`, `lifecycle_stage`, and active `status` semantics are removed by Phase 5 close.
- **No new agents — but placeholder shims are in scope.** Agents for `expansion_qa`, `code_review_qa`, `holistic_qa`, `merge` already exist as bundled YAMLs and only need rewiring against new stage names. Four discovery-stage agents have no surviving YAML or follow-up task after the #12725 cascade-delete (stage → agent slug mapping: `ideation → analyst`, `research → researcher`, `architecture → architect`, `prd → product-manager`), and `pr` is owned by #13552 (already open). This epic ships **disabled placeholder YAMLs** for the four missing discovery agents (clearly marked as such) and creates a parent epic plus four tracking tasks for the real implementation work. Real agent behavior remains out of scope.
- **Single project.** No cross-project / multi-tenant kanban work.
- **`escalated` is preserved** as the human-in-the-loop flag — promoted from a `status` value to first-class `is_escalated` column. Every other active `status` value is subsumed by per-stage tri-state.
- **Readiness/blocking semantics stay equivalent.** `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates`, and task `state.is_blocked` must return the same results as the old model for equivalent fixtures after cutover.
- **Schema baseline before this epic = 220** (`src/gobby/storage/migrations.py:65`). New migrations begin at 221.
- **No explicit test tasks anywhere in this plan.** TDD sandwiches are auto-inserted by `/gobby expand` for every `category: code` and `category: config` task.

## Phase 1: Registry + Manifest Schema
`kind: framing`

**Goal**: Land the three new tables, the bundled stages YAML, the artifact-column extensions, and the four discovery-agent placeholder YAMLs. After Phase 1, the database can store a stage manifest, the registry seeds itself on startup, and the registry's `default_agent` slot resolves to a real (if disabled) bundled agent for every stage.

### 1.1 Schema migration: registry, defaults, manifest, and PR/merge artifact columns [category: code]
`kind: deliverable`

Target: `src/gobby/storage/migrations.py`

Add migration version `221` to the `MIGRATIONS` list. Migration adds three new tables and four new columns to `task_artifacts`. Use `db.transaction()` around the schema changes; follow the existing `_add_task_artifact_evidence_columns` pattern (`src/gobby/storage/migrations.py:111-159`) for the artifact-column additions.

Tables to create (each guarded by `IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS task_stages_registry (
    name TEXT PRIMARY KEY,
    display_label TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('discovery','design','verification','implementation','delivery')),
    default_agent TEXT,
    position_hint INTEGER NOT NULL,
    requires_human INTEGER NOT NULL DEFAULT 0,
    is_terminal INTEGER NOT NULL DEFAULT 0,
    bundled_hash TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_type_default_stages (
    task_type TEXT NOT NULL,
    stage_name TEXT NOT NULL REFERENCES task_stages_registry(name) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (task_type, stage_name)
);
CREATE INDEX IF NOT EXISTS idx_task_type_default_stages_position
    ON task_type_default_stages (task_type, position);

CREATE TABLE IF NOT EXISTS task_stage_states (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage_name TEXT NOT NULL REFERENCES task_stages_registry(name) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'needs_doing'
        CHECK (state IN ('needs_doing','in_progress','done')),
    entered_at TEXT,
    entered_by_session_id TEXT,
    completed_at TEXT,
    completed_by_session_id TEXT,
    completed_commit_sha TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    artifact_refs TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (task_id, stage_name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_stage_states_position
    ON task_stage_states (task_id, position);
CREATE INDEX IF NOT EXISTS idx_task_stage_states_state
    ON task_stage_states (stage_name, state);
CREATE INDEX IF NOT EXISTS idx_task_stage_states_open
    ON task_stage_states (task_id, position) WHERE state != 'done';
```

`artifact_refs` is a JSON-encoded object (`json.dumps`) of pointers into `task_artifacts` (e.g. `{"plan_file": "plan_file_path", "expansion_run": "expansion_run_id"}`). The unique `(task_id, position)` index enforces the position-uniqueness invariant per task. The partial index `idx_task_stage_states_open` accelerates the "leftmost non-done" current-stage projection.

Columns to add to `task_artifacts` (mirror `_add_task_artifact_evidence_columns` rebuild pattern: rename old → create new with allowlisted columns → INSERT SELECT → drop old):

- `pr_review_report TEXT`
- `structured_pr_verdict TEXT` (JSON-encoded)
- `merge_campaign_report TEXT`
- `is_escalated INTEGER NOT NULL DEFAULT 0` — promoted from projection (separate Phase 5 work; this column is created here to keep migration churn in one place but stays at default 0; Phase 5 owns the backfill from `escalated_at IS NOT NULL` and the cutover).

Update `_default_task_artifact_column` (`src/gobby/storage/migrations.py:177-182`) to include defaults for the three new TEXT columns (`NULL`) and `is_escalated` (`0`). Update `_task_artifacts_create_sql` (`src/gobby/storage/migrations.py:75-108`) to include the new columns so fresh installs match.

**Acceptance:**

- A1.1.1 — Migration version 221 exists in `MIGRATIONS`. file: `src/gobby/storage/migrations.py`. symbol: `gobby.storage.migrations.MIGRATIONS`.
- A1.1.2 — Three new tables created with declared schema, CHECK constraints, indexes, and partial index on open rows. test: `tests/storage/test_migration_221.py::test_creates_registry_tables`.
- A1.1.3 — `task_artifacts` gains four new columns with declared defaults; rebuild path preserves existing rows. test: `tests/storage/test_migration_221.py::test_artifact_columns_added`.
- A1.1.4 — Fresh-install `_task_artifacts_create_sql` includes the new columns so a blank DB skips the rebuild path. behavior: "fresh install schema matches migration end state" verified in `tests/storage/test_migration_221.py::test_fresh_install_matches`.

### 1.2 Bundle stages.yaml + sync loader [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/registry/stages.yaml`, `src/gobby/storage/tasks/_stage_registry_loader.py` (new)

Author the bundled YAML as the source of truth for the 14 canonical stages. Mirror the bundled-template pattern from `src/gobby/install/shared/{rules,workflows,agents}` — the file is hashed at startup, drift triggers an upsert, user overrides are detected by hash comparison.

YAML shape:

```yaml
# src/gobby/install/shared/registry/stages.yaml
version: 1
stages:
  - name: ideation
    display_label: Ideation
    description: Early problem framing; capture motivating questions and constraints.
    category: discovery
    default_agent: analyst                    # placeholder shim — Phase 1.3
    position_hint: 10
    requires_human: false
    is_terminal: false
  - name: research
    display_label: Research
    description: Targeted investigation; produce findings consumable by architecture/PRD.
    category: discovery
    default_agent: researcher                 # placeholder shim — Phase 1.3
    position_hint: 20
  - name: architecture
    display_label: Architecture
    description: Cross-cutting design decisions and component shape.
    category: design
    default_agent: architect                  # placeholder shim — Phase 1.3
    position_hint: 30
  - name: prd
    display_label: PRD
    description: Productized requirements; bridges discovery and planning.
    category: design
    default_agent: product-manager            # placeholder shim — Phase 1.3
    position_hint: 40
  - name: planning
    display_label: Planning
    description: Implementation plan authoring (interactive or autonomous).
    category: design
    default_agent: planner
    position_hint: 50
  - name: adversarial_review
    display_label: Adversarial Review
    description: Plan-adversary critiques the plan and emits the typed manifest.
    category: verification
    default_agent: plan-adversary
    position_hint: 60
  - name: test_arch
    display_label: Test Architecture
    description: Test scaffolding and contract test design before expansion.
    category: verification
    default_agent: test-architect
    position_hint: 70
  - name: expansion
    display_label: Expansion
    description: Decompose plan into TDD-wrapped leaf tasks.
    category: implementation
    position_hint: 80
  - name: expansion_qa
    display_label: Expansion QA
    description: Verify the expanded tree against the plan's coverage contract.
    category: verification
    default_agent: expansion-qa
    position_hint: 90
  - name: development
    display_label: Development
    description: Leaf implementation work; drives TDD sandwiches.
    category: implementation
    default_agent: backend-developer          # primary fallback; build-time may override per-task
    position_hint: 100
  - name: code_review_qa
    display_label: Code Review QA
    description: Automated and human code review of leaf changes.
    category: verification
    default_agent: qa-reviewer
    position_hint: 110
  - name: holistic_qa
    display_label: Holistic QA
    description: Whole-epic review after every leaf is parked.
    category: verification
    default_agent: holistic-reviewer
    position_hint: 120
  - name: pr
    display_label: Pull Request
    description: Open/update PR, capture verdict, gate on external review.
    category: delivery
    # default_agent left blank — owned by #13552 (PR/merge skill epic)
    position_hint: 130
  - name: merge
    display_label: Merge
    description: Land approved PR; resolve conflicts; close terminal task.
    category: delivery
    default_agent: merge-orchestrator
    position_hint: 140
    is_terminal: true
```

`default_agent` is populated for every stage with a real or placeholder bundled agent. The four discovery stages point at placeholder shims landed in 1.3; `pr` is left blank because #13552 owns it; `expansion` is left blank because expansion runs as a pipeline action, not an agent spawn.

Sync loader (`src/gobby/storage/tasks/_stage_registry_loader.py`):

```python
class StageRegistryLoader:
    """Sync bundled stages.yaml into task_stages_registry on startup.

    Mirrors the workflow loader's hash-drift detection. Bundled rows are
    upserted whenever the file hash changes; rows whose name is missing
    from the bundled YAML are NOT deleted (operator-added stages are
    permitted but not part of the supported contract).
    """

    BUNDLED_PATH = Path("src/gobby/install/shared/registry/stages.yaml")

    def sync(self, db: DatabaseProtocol) -> StageRegistrySyncResult: ...
    def detect_override(self, db_row: dict, bundled_row: dict) -> bool: ...
```

Wire `StageRegistryLoader().sync(db)` into the daemon startup sequence next to existing template-sync calls (search `WorkflowLoader().sync(`, mirror placement). Sync runs after the migration applier so the table exists.

**Acceptance:**

- A1.2.1 — `src/gobby/install/shared/registry/stages.yaml` exists with all 14 stages, every required field set, and `merge` flagged `is_terminal: true`. file: `src/gobby/install/shared/registry/stages.yaml`.
- A1.2.2 — `StageRegistryLoader.sync()` upserts bundled rows on first startup and on hash drift. symbol: `gobby.storage.tasks._stage_registry_loader.StageRegistryLoader`. test: `tests/storage/test_stage_registry_loader.py::test_sync_seeds_on_first_startup`.
- A1.2.3 — Daemon startup wiring invokes the loader after migrations. file: `src/gobby/runner.py` (or wherever `WorkflowLoader` is invoked — match the placement). test: `tests/test_startup_seeds_stage_registry.py::test_registry_populated_after_startup`.
- A1.2.4 — Operator-added stages survive bundled-YAML re-sync; bundled stages get re-upserted. behavior: "user-added stage rows persist across sync" verified in `tests/storage/test_stage_registry_loader.py::test_user_added_stage_preserved`.

### 1.3 Placeholder agent YAMLs for discovery stages [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/analyst.yaml`, `researcher.yaml`, `architect.yaml`, `product-manager.yaml` (all new)

Ship four bundled agent definitions as **disabled placeholders** so the registry's `default_agent` foreign key resolves to a real bundled row even before real agent behavior lands. Each YAML is functionally inert (`enabled: false`) and carries explicit placeholder language at the top so future maintainers cannot mistake it for a working agent.

Stage → agent slug mapping (load-bearing, used in stages.yaml from 1.2):

| Stage | Agent slug |
|-------|-----------|
| `ideation` | `analyst` |
| `research` | `researcher` |
| `architecture` | `architect` |
| `prd` | `product-manager` |

Canonical placeholder template (use verbatim, varying only `name`, `description`, target stage, and the placeholder banner specifics):

```yaml
# src/gobby/install/shared/workflows/agents/analyst.yaml
#
# PLACEHOLDER — disabled stub for the `ideation` stage. Replace with a real
# implementation before enabling. Tracked by the agent-followup task created
# by the stage-manifest-cutover plan (deliverable 5.4); see
# `agent-followup:analyst` task in gobby-tasks for ownership and context.

name: analyst
description: |
  PLACEHOLDER — Disabled stub for the `ideation` stage of the task-stage
  manifest model. This file exists so `task_stages_registry.default_agent`
  resolves to a real bundled row. The real agent must be authored in a
  follow-up plan; see `agent-followup:analyst` task for tracking.

  When this YAML is replaced with a real agent, set `enabled: true`, fill in
  `instructions`, and remove the PLACEHOLDER banners.

version: "0.1"
enabled: false                 # Load-bearing: disabled until real impl lands.
priority: 1                    # Lowest priority so any real agent overrides.
surfaces: [spawn]
provider: claude
model: haiku
isolation: none

instructions: |
  PLACEHOLDER AGENT — IDEATION STAGE

  You are a placeholder stub. The real `analyst` agent has not been
  implemented yet. If a dispatcher accidentally enabled this YAML and spawned
  you, your job is to immediately escalate the task with a clear reason so
  the operator can investigate.

  Action: call escalate_task(task_id=<your task>, reason="placeholder_agent:analyst:not_implemented")
  and exit. Do not attempt to do ideation work.
```

The other three follow the same pattern with stage-appropriate name/description/escalation reason:
- `researcher.yaml` for `research` (escalation reason: `placeholder_agent:researcher:not_implemented`)
- `architect.yaml` for `architecture` (escalation reason: `placeholder_agent:architect:not_implemented`)
- `product-manager.yaml` for `prd` (escalation reason: `placeholder_agent:product-manager:not_implemented`)

Because every placeholder is `enabled: false`, the bundled-template sync will install the row but not register the agent for spawning. The dispatcher's `_has_<stage>_agent(context)` check (Phase 4.1, 4.2) returns `False` for disabled agents and surfaces the existing `<stage>_no_agent` escalation, surfacing the gap loudly rather than silently doing nothing.

CLAUDE.md retired-agent allowlist must NOT block these names. Verify nothing in `src/gobby/workflows/loader.py` or template sync logic soft-deletes them; if a retired-name pattern matches, exempt the placeholders explicitly (the four slugs are not in the retired list, but confirm during implementation).

**Acceptance:**

- A1.3.1 — Four YAML files exist with declared `name`, `enabled: false`, `priority: 1`, and PLACEHOLDER banners. file: `src/gobby/install/shared/workflows/agents/analyst.yaml`, `researcher.yaml`, `architect.yaml`, `product-manager.yaml`.
- A1.3.2 — Each file's `instructions` block tells the agent to escalate with reason `placeholder_agent:<slug>:not_implemented` if accidentally spawned. test: `tests/agents/test_placeholder_agents.py::test_each_placeholder_escalates_on_spawn`.
- A1.3.3 — Bundled-template sync installs the rows with `enabled: false`. test: `tests/agents/test_placeholder_agents.py::test_sync_installs_disabled`.
- A1.3.4 — `task_stages_registry.default_agent` foreign-key resolves for all four discovery stages after Phase 1.2 sync. test: `tests/storage/tasks/test_stage_registry_default_agent_fk.py::test_discovery_stage_default_agents_resolve`.
- A1.3.5 — Dispatcher's missing-agent check treats `enabled: false` as missing and escalates with the stage-specific `<stage>_no_agent` reason rather than spawning the placeholder. test: `tests/dispatch/test_no_agent_paths.py::test_disabled_placeholder_treated_as_missing`.

## Phase 2: Stage-Native Storage + API Surface
`kind: framing`

**Goal**: Land the storage managers, the migration script that backfills `task_stage_states` from `(lifecycle, status, labels)`, and the MCP/HTTP/CLI surfaces. After Phase 2, every read and write of stage state goes through the new APIs; the dispatcher still uses the old code (Phase 3 swaps it).

### 2.1 Stage registry + stage states storage managers [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/storage/tasks/_stage_registry.py`, `src/gobby/storage/tasks/_stage_states.py` (both new)

Two new manager modules under the same package as `_artifacts.py`, `_lifecycle_events.py`, `_dispatch_mutex.py`. Wire both into `LocalTaskManager` (`src/gobby/storage/tasks/_manager.py`) as composed sub-managers, mirroring how `TaskArtifactManager` is exposed.

`_stage_registry.py`:

```python
@dataclass(frozen=True, slots=True)
class StageRegistryEntry:
    name: str
    display_label: str
    description: str
    category: Literal["discovery","design","verification","implementation","delivery"]
    default_agent: str | None
    position_hint: int
    requires_human: bool
    is_terminal: bool


class StageRegistryManager:
    def __init__(self, db: DatabaseProtocol) -> None: ...

    def list_all(self) -> list[StageRegistryEntry]: ...
    def get(self, name: str) -> StageRegistryEntry | None: ...
    def upsert(self, entry: StageRegistryEntry, *, bundled_hash: str | None = None) -> None: ...
    def list_default_stages(self, task_type: str) -> list[tuple[str, int]]:
        """Return [(stage_name, position)] sorted by position from task_type_default_stages."""
    def set_default_stages(self, task_type: str, stages: Sequence[tuple[str, int]]) -> None: ...
```

`_stage_states.py`:

```python
@dataclass(frozen=True, slots=True)
class StageState:
    task_id: str
    stage_name: str
    position: int
    state: Literal["needs_doing","in_progress","done"]
    entered_at: str | None
    entered_by_session_id: str | None
    completed_at: str | None
    completed_by_session_id: str | None
    completed_commit_sha: str | None
    attempt_count: int
    artifact_refs: dict[str, str] | None
    notes: str | None


class StageStatesManager:
    def __init__(self, db: DatabaseProtocol, events: TaskLifecycleEventManager) -> None: ...

    # Reads
    def list_for_task(self, task_id: str) -> list[StageState]:
        """Sorted by position. Empty list if task has no manifest yet."""
    def get(self, task_id: str, stage_name: str) -> StageState | None: ...
    def current_stage(self, task_id: str) -> StageState | None:
        """Leftmost row by position whose state != 'done'. None if all done or no manifest."""
    def list_tasks_at_stage(
        self, *, stage_name: str, state: str | None = None,
        project_id: str | None = None,
    ) -> list[str]:
        """Drives kanban column queries."""

    # Writes — every mutator records a task_lifecycle_events row
    def initialize_manifest(
        self, task_id: str, stages: Sequence[tuple[str, int]], *, by_session_id: str | None,
    ) -> list[StageState]:
        """Insert manifest rows; all start at needs_doing. Idempotent only if the
        target manifest matches existing rows exactly; otherwise raises
        ManifestAlreadyInitializedError."""

    def add_stage(
        self, task_id: str, stage_name: str, position: int, *, by_session_id: str | None,
    ) -> StageState:
        """Insert a row. Reorders affected positions. Errors if stage_name not in registry
        or task already has the stage."""

    def remove_stage(
        self, task_id: str, stage_name: str, *, by_session_id: str | None,
    ) -> None:
        """Delete a row; reorder positions to remain dense (1..N)."""

    def start_stage(
        self, task_id: str, stage_name: str, *,
        by_session_id: str | None, notes: str | None = None,
    ) -> StageState:
        """Transition needs_doing → in_progress. Only allowed when this row's position
        equals current_stage().position (no skipping)."""

    def complete_stage(
        self, task_id: str, stage_name: str, *,
        by_session_id: str | None, commit_sha: str | None = None,
        artifact_updates: Mapping[str, str] | None = None,
    ) -> StageState:
        """Transition in_progress → done. Persists commit_sha + artifact_refs."""

    def fail_stage(
        self, task_id: str, stage_name: str, *,
        reason: str, needs_human: bool = False, by_session_id: str | None,
    ) -> StageState:
        """Transition in_progress → needs_doing with attempt_count + 1, OR triggers
        escalate_task(needs_human=True). Escalation wiring goes through the existing
        escalate_task helper; do not write is_escalated directly here."""
```

Invariants enforced in `StageStatesManager` (raise `ValueError` or a typed error class on violation; cover with tests):

1. `position` is unique per `task_id` (DB unique index plus pre-flight check for clearer errors).
2. `stage_name` must exist in `task_stages_registry`.
3. Transitions are: `needs_doing → in_progress`, `in_progress → done`, `in_progress → needs_doing` (fail). No skipping. No reverse from `done`.
4. `start_stage` requires the target row to be the current `current_stage()` (leftmost non-done row).
5. `attempt_count` increments on every `start_stage` call (replaces `planning-round:N` / `qa-attempts:N` labels).

Every mutator emits a `task_lifecycle_events` row via the injected `TaskLifecycleEventManager`. `from_state` is `f"{stage_name}:{prev_state}"`, `to_state` is `f"{stage_name}:{new_state}"`, `reason` is the caller-supplied reason or a derived one (e.g. `"start_stage:planning"`), `by_actor` is `by_session_id` or `"system"`.

**Acceptance:**

- A2.1.1 — `_stage_registry.py` provides `StageRegistryManager` with the listed read/write methods. file: `src/gobby/storage/tasks/_stage_registry.py`. symbol: `gobby.storage.tasks._stage_registry.StageRegistryManager`.
- A2.1.2 — `_stage_states.py` provides `StageStatesManager` with the listed reads, writes, and invariants. symbol: `gobby.storage.tasks._stage_states.StageStatesManager`. test: `tests/storage/tasks/test_stage_states.py::test_position_uniqueness_enforced`.
- A2.1.3 — Every mutator emits a `task_lifecycle_events` row with the documented `from_state`/`to_state` shape. test: `tests/storage/tasks/test_stage_states.py::test_transitions_emit_events`.
- A2.1.4 — `LocalTaskManager` exposes both managers as `.stages_registry` and `.stage_states`. file: `src/gobby/storage/tasks/_manager.py`. test: `tests/storage/tasks/test_manager_exposes_stage_managers.py::test_managers_accessible`.
- A2.1.5 — Forbidden transitions (skipping, going backwards from done) raise typed errors. test: `tests/storage/tasks/test_stage_states.py::test_invalid_transitions_raise`.

### 2.2 One-shot backfill: derive `task_stage_states` from existing `(lifecycle, status, labels)` [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/storage/migrations.py` (new helper `_backfill_task_stage_states_from_legacy`), invoked from migration version 222.

The backfill runs once during the migration to 222. For every task in `tasks`:

1. Resolve task's manifest from `task_type_default_stages` for that `task_type` minus any `stage-:<name>` skip labels (so existing skip labels are honored exactly once).
2. Walk the resolved manifest in position order and assign `(state, attempt_count)` per the mapping table below, derived from the task's current `(lifecycle, status, labels)`.
3. Populate `entered_at` / `completed_at` from the task's `updated_at` and `created_at` as a coarse approximation; `entered_by_session_id` and `completed_by_session_id` use `claimed_by_session_id` if available, else `closed_in_session_id`, else `NULL`.
4. Populate `attempt_count` from `planning-round:N` and `qa-attempts:N` labels (numeric suffix); fall back to `0`.
5. Drop the `stage-:<name>` skip labels (already encoded as "stage absent from manifest").

Mapping table (`(lifecycle, status)` → manifest result):

| lifecycle | status | Resulting per-row state |
|-----------|--------|-------------------------|
| `open` | `open` | All manifest rows `needs_doing` |
| `open` | any other | All `needs_doing` (`status` overrides handled below) |
| `plan_review` | `open` or `in_progress` | `planning` row `in_progress`, predecessors `done`, successors `needs_doing` |
| `plan_review` | `needs_review` | `planning` row `done`, `adversarial_review` `in_progress`, predecessors `done`, successors `needs_doing` |
| `plan_review` | `review_approved` | `planning` and `adversarial_review` `done`, successors `needs_doing` |
| `test_arch` | any | `test_arch` row `in_progress`, predecessors `done`, successors `needs_doing` |
| `expanding` | `open` or `in_progress` | `expansion` row `in_progress`, predecessors `done`, successors `needs_doing` |
| `expanding` | `needs_review` | `expansion` row `done`, `expansion_qa` `in_progress`, predecessors `done`, successors `needs_doing` |
| `in_development` | `open` or `in_progress` | `development` row `in_progress`, predecessors `done`, successors `needs_doing` |
| `in_development` | `needs_review` | `development` row `done`, `code_review_qa` `in_progress`, predecessors `done`, successors `needs_doing` |
| `in_development` | `review_approved` | `development` and `code_review_qa` `done`, successors `needs_doing` (leaf-park; epics scan children) |
| `holistic_review` | any non-terminal | `holistic_qa` row `in_progress`, predecessors `done`, successors `needs_doing` |
| `holistic_review` | `review_approved` | `holistic_qa` row `done`, successors `needs_doing` |
| `pr` | `open` | `pr` row `in_progress`, predecessors `done`, `merge` `needs_doing` |
| `pr` | `needs_review` | `pr` row `in_progress` with `pr_url` populated, predecessors `done`, `merge` `needs_doing` |
| `merging` | any non-terminal | `merge` row `in_progress`, predecessors `done` |
| `merged` | `closed` | All rows `done` |

`status='escalated'` overrides the per-row state mapping with `is_escalated=true` (Phase 5 handles the column promotion); the active stage row stays at whatever the lifecycle component dictates.

`status='closed'` with non-`merged` lifecycle: terminal-close-without-merge case (e.g., abandoned tasks). All rows up to and including the row implied by `lifecycle` are `done`; `closed_at` is already populated on the task itself.

Pre-migration audit: emit a `(lifecycle, status, count)` census to `src/gobby/storage/migrations.py` log output. If the census includes a `(lifecycle, status)` tuple not in the mapping table, fail the migration with a clear message — this forces the operator (or the implementing agent) to extend the table rather than silently produce wrong rows.

`task_type` defaults at migration time:
- `epic` → full 14-stage pipeline
- `feature` → `[planning, adversarial_review, test_arch, expansion, expansion_qa, development, code_review_qa, holistic_qa, pr, merge]`
- `bug` → `[development, code_review_qa, pr, merge]`
- `refactor` → `[planning, development, code_review_qa, pr, merge]`
- `chore` / `task` → `[development, pr, merge]`

These defaults are written to `task_type_default_stages` as part of this same migration (idempotent upsert; Phase 5 adds the new task types).

After backfill, drop the `stage-:<name>` labels from every task. Do not drop `planning-round:` or `qa-attempts:` labels in this migration — they're still readable for diagnostics; Phase 7 cleans them up.

**Acceptance:**

- A2.2.1 — Migration version 222 in `MIGRATIONS` performs the backfill in a single transaction. file: `src/gobby/storage/migrations.py`. symbol: `gobby.storage.migrations.MIGRATIONS` (entry 222).
- A2.2.2 — Every observed `(lifecycle, status)` tuple in a fixture DB produces rows matching the mapping table. test: `tests/storage/test_migration_222_backfill.py::test_mapping_exhaustive`.
- A2.2.3 — Unmapped `(lifecycle, status)` tuples cause migration failure with a message naming the offending tuple. test: `tests/storage/test_migration_222_backfill.py::test_unmapped_tuple_fails_loudly`.
- A2.2.4 — `attempt_count` populated from `planning-round:N` and `qa-attempts:N` labels. test: `tests/storage/test_migration_222_backfill.py::test_attempt_count_from_labels`.
- A2.2.5 — `task_type_default_stages` populated for `epic`, `feature`, `bug`, `refactor`, `chore`, `task` matching declared lists. test: `tests/storage/test_migration_222_backfill.py::test_task_type_defaults_seeded`.
- A2.2.6 — `stage-:<name>` labels removed from every task post-backfill. test: `tests/storage/test_migration_222_backfill.py::test_skip_labels_dropped`.

### 2.3 New gobby-tasks MCP tools for stage manifest [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/tasks/_stages.py` (new), registered via `_factory.py` and exposed by `gobby-tasks-ops` as well where mutation is involved.

Add nine new tools. Each tool has its own subsection — implementing agents see only one subsection at a time, so signatures must be repeated where they appear in dependent tools.

| Tool | Server | Purpose |
|------|--------|---------|
| `get_task_stages(task_id)` | `gobby-tasks` | Return manifest in position order. |
| `list_stages_registry()` | `gobby-tasks` | Return all registry entries. |
| `get_task_type_defaults(task_type)` | `gobby-tasks` | Return the default manifest for a task type. |
| `start_stage(task_id, stage_name, notes?)` | `gobby-tasks-ops` | Transition `needs_doing → in_progress`. |
| `complete_stage(task_id, stage_name, commit_sha?, artifact_updates?)` | `gobby-tasks-ops` | Transition `in_progress → done`. |
| `fail_stage(task_id, stage_name, reason, needs_human?)` | `gobby-tasks-ops` | Increment attempt or escalate. |
| `add_stage(task_id, stage_name, position)` | `gobby-tasks-ops` | Insert a row mid-manifest. |
| `remove_stage(task_id, stage_name)` | `gobby-tasks-ops` | Delete a row from manifest. |
| `record_pr_verdict(task_id, verdict, findings, report_ref?)` | `gobby-tasks-ops` | Store `structured_pr_verdict` + `pr_review_report` on task_artifacts; advances `pr` stage state per verdict. |

Each tool delegates to `LocalTaskManager.stages_registry` or `.stage_states`. Schema for the verdict tool:

```python
def record_pr_verdict(
    task_id: str,
    verdict: Literal["approved", "rejected", "needs_changes"],
    findings: str,
    report_ref: str | None = None,
) -> dict[str, Any]:
    """Persist structured PR verdict on task_artifacts and advance pr stage.

    On verdict='approved': complete_stage(task_id, 'pr'). On 'rejected' or
    'needs_changes': fail_stage(task_id, 'pr', reason=findings, needs_human=False).
    Stores findings in task_artifacts.pr_review_report and a JSON-encoded
    {verdict, findings, report_ref} in task_artifacts.structured_pr_verdict.
    """
```

Block legacy lifecycle merge tools (`mark_task_pr_opened`, `mark_task_merged`, `mark_task_merge_failed`) for the duration of this epic by leaving them in place AND surfacing their usage in a deprecation logger; Phase 6 / 7 deletes them. Implementing agents must use stage-native operations only.

**Acceptance:**

- A2.3.1 — Nine new tools registered, each with `inputSchema`, `outputSchema`, and a real handler. file: `src/gobby/mcp_proxy/tools/tasks/_stages.py`. symbol: `gobby.mcp_proxy.tools.tasks._stages`.
- A2.3.2 — Tool registration adds them to the `gobby-tasks` and `gobby-tasks-ops` registries. file: `src/gobby/mcp_proxy/tools/tasks/_factory.py`, `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py`. test: `tests/mcp_proxy/tools/tasks/test_stage_tools_registered.py::test_tools_visible_in_listing`.
- A2.3.3 — `record_pr_verdict` writes `structured_pr_verdict` and `pr_review_report`, then advances stage. test: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_completes_stage`.
- A2.3.4 — `start_stage` errors on out-of-order start (skipping ahead). test: `tests/mcp_proxy/tools/tasks/test_stage_tools.py::test_start_stage_skipping_errors`.

### 2.4 New HTTP routes for stage manifest [category: code] (depends: 2.1)
`kind: deliverable`

Target: `src/gobby/servers/routes/tasks.py`, `src/gobby/servers/routes/stages.py` (new)

Add new endpoints and extend the list endpoint with stage filters.

```python
# stages.py
@router.get("/api/stages/registry")
async def list_stages_registry() -> StagesRegistryResponse: ...

@router.get("/api/task-types/{task_type}/default-stages")
async def get_task_type_defaults(task_type: str) -> TaskTypeDefaultsResponse: ...

# tasks.py — new sub-routes
@router.get("/api/tasks/{task_id}/stages")
async def get_task_stages(task_id: str) -> TaskStagesResponse: ...

@router.patch("/api/tasks/{task_id}/stages/{stage_name}")
async def patch_task_stage(
    task_id: str,
    stage_name: str,
    request: StagePatchRequest,
) -> TaskStageResponse: ...

# tasks.py list endpoint extension
@router.get("/api/tasks")
async def list_tasks(
    # existing params: skip, limit, status, lifecycle, ...
    stage: str | None = None,
    stage_state: str | None = None,
    # ...
) -> TaskListResponse: ...
```

`StagePatchRequest` body schema:

```python
class StagePatchRequest(BaseModel):
    action: Literal["start", "complete", "fail", "add", "remove"]
    notes: str | None = None
    reason: str | None = None  # required for action='fail'
    needs_human: bool = False
    commit_sha: str | None = None
    artifact_updates: dict[str, str] | None = None
    position: int | None = None  # required for action='add'
```

The list endpoint's `stage` and `stage_state` filters JOIN `tasks` to `task_stage_states` and filter `WHERE task_stage_states.stage_name = :stage [AND task_stage_states.state = :stage_state]`. The response gains an optional `stages` field per task containing the denormalized manifest (a single SQL query that LEFT JOINs and groups; no N+1).

`TaskListResponse.tasks[i].stages: list[StageStateView]` is added; existing fields stay backward compatible. Existing `?status=...` and `?lifecycle=...` params remain functional through Phase 5 (they're consumed by the legacy projection helpers); Phase 5 removes them.

**Acceptance:**

- A2.4.1 — Five new endpoints registered with declared paths, methods, and schemas. file: `src/gobby/servers/routes/stages.py`, `src/gobby/servers/routes/tasks.py`. test: `tests/servers/routes/test_stage_routes.py::test_routes_registered`.
- A2.4.2 — `PATCH /api/tasks/{id}/stages/{name}` action='start' moves the row to `in_progress`. test: `tests/servers/routes/test_stage_routes.py::test_patch_start_stage`.
- A2.4.3 — `GET /api/tasks?stage=development&stage_state=in_progress` returns only tasks with that exact `(stage_name, state)` row. test: `tests/servers/routes/test_stage_routes.py::test_list_filter_by_stage_state`.
- A2.4.4 — Denormalized `stages` field returned on each task in the list response when stage filters are active or when an explicit `?include_stages=1` flag is set. test: `tests/servers/routes/test_stage_routes.py::test_list_includes_denormalized_manifest`.
- A2.4.5 — `task_event` WebSocket events fire on every stage state transition. behavior: "broadcaster emits stage_changed event" verified in `tests/servers/websocket/test_stage_broadcast.py::test_stage_transition_broadcasts`.

### 2.5 New CLI commands and build flags [category: code] (depends: 2.1, 2.3)
`kind: deliverable`

Target: `src/gobby/cli/tasks/crud.py`, `src/gobby/cli/tasks/_utils.py`, `src/gobby/cli/build.py` (or wherever `gobby build` lives)

Add new `gobby tasks` subcommands and extend `gobby build` and `gobby tasks list`.

```text
gobby tasks stages <task_ref>                    # render manifest table
gobby tasks advance <task_ref> [--stage <name>]  # complete current stage; auto-start next
gobby tasks list --stage <name> [--state <state>]
gobby build <ref> --stages <a,b,c>               # explicit manifest
gobby build <ref> --add-stage <name>[@<position>]
gobby build <ref> --skip-stage <name>            # opt-out of a default-manifest stage
```

`gobby tasks list` currently has `--status` and `--lifecycle` options (`src/gobby/cli/tasks/crud.py`). Add `--stage` and `--state` flags; they call the new HTTP endpoint with the new filter params. Keep `--status` and `--lifecycle` working through Phase 5; Phase 6 removes them.

`gobby tasks advance`: if `--stage` is omitted, advance the current stage; if specified, validate it equals the current stage's name (else error). On success, automatically advance the next manifest row to `in_progress` if it's eligible (no human gate, no agent missing). This is the CLI counterpart of Phase 3's dispatcher behavior.

`gobby build` flag resolution order: `--stages` (explicit list, replaces default); else type defaults + `--add-stage` insertions + `--skip-stage` removals. Profiles (`quick`, `full`, `full-yolo`) become named bundles of `--skip-stage` arguments resolved at build time.

CLI output for `gobby tasks stages`:

```text
$ gobby tasks stages #13482
#13482  Lifecycle + status enum alignment for kanban visibility
Stage              State        Attempts  Updated
─────────────────  ───────────  ────────  ──────────
planning           done         3         2026-04-30
adversarial_review done         2         2026-04-30
expansion          in_progress  1         2026-04-30
…
```

**Acceptance:**

- A2.5.1 — `gobby tasks stages` Click command renders the manifest table sorted by position. file: `src/gobby/cli/tasks/crud.py`. test: `tests/cli/test_tasks_stages_command.py::test_renders_manifest`.
- A2.5.2 — `gobby tasks advance` advances the current stage and auto-starts the next when eligible. test: `tests/cli/test_tasks_advance_command.py::test_auto_advance_next_stage`.
- A2.5.3 — `gobby tasks list --stage development --state in_progress` filters to that exact `(stage_name, state)` row. test: `tests/cli/test_tasks_list_stage_filter.py::test_stage_state_filter`.
- A2.5.4 — `gobby build <ref> --stages a,b,c` writes exactly that manifest; `--add-stage` inserts at requested position; `--skip-stage` omits a default stage. test: `tests/cli/test_build_stage_flags.py::test_build_flag_resolution`.

## Phase 3: Dispatcher Refactor
`kind: framing`

**Goal**: Rewrite the dispatcher's rule evaluation, candidate scan, and build-time manifest resolution to use `task_stage_states` instead of `(lifecycle, status)` tuples. After Phase 3, the daemon dispatches purely from the manifest model.

### 3.1 Rewrite `dispatch/rules.py` to query stage manifest [category: code] (depends: 2.1, 2.2)
`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`

Replace string-checking helpers and rule bodies with manifest-aware reads. Existing helpers to retire or rewrite:

- `_stage_skipped(task, stage)` — delete; "skipped" means "stage absent from manifest". Replaced by `task_has_stage(task, stage)`.
- `_state(task)` (lifecycle, status tuple) — delete; rules read `current_stage(task)` and the row's `state`.
- `_advance(task, lifecycle, status, reason)` — replaced by `_complete_current_stage(task, reason)` and `_fail_current_stage(task, reason, needs_human)` helpers that delegate to `StageStatesManager`.

New helpers:

```python
def task_has_stage(task: Task, stage_name: str) -> bool:
    """True iff task.stages contains a row for stage_name (in any state)."""

def current_stage(task: Task) -> StageState | None:
    """Leftmost manifest row by position whose state != 'done'."""

def _spawn_stage_agent(
    task: Task, stage: StageState, context: RuleContext, agent_slug: str,
) -> SpawnAgentAction: ...

def _advance_to_next_stage(task: Task, reason: str) -> AdvanceStageAction: ...
```

`Task` gains a denormalized `.stages: tuple[StageState, ...]` field populated by `reload_candidate` via a single LEFT JOIN; rules never re-query.

Rule rewrite (1:1 mapping from existing rules to new stage-native form):

| Old rule | New rule | Gates on | Action |
|----------|----------|----------|--------|
| `plan_review_rule` | `planning_rule` | `current_stage.name == 'planning'` AND `state == 'in_progress'` | spawn `planner` (already speced) |
| (new) | `adversarial_review_rule` | `current_stage.name == 'adversarial_review'` AND `state == 'in_progress'` | spawn `plan-adversary` |
| `test_arch_rule` | `test_arch_rule` | `current_stage.name == 'test_arch'` AND `state == 'in_progress'` | spawn `test-architect` |
| `expansion_rule` | `expansion_rule` | `current_stage.name == 'expansion'` AND `state == 'in_progress'` | `StartExpansionAction` |
| (new) | `expansion_qa_rule` | `current_stage.name == 'expansion_qa'` AND `state == 'in_progress'` | escalate (no agent yet — flag with `EscalateAction(reason='expansion_qa_no_agent')`) |
| `isolation_rule` | `development_isolation_rule` | `current_stage.name == 'development'` AND `state == 'needs_doing'` | `CreateIsolationAction` |
| `dev_rule` | `development_rule` | `current_stage.name == 'development'` AND `state == 'in_progress'` AND `_is_leaf(task)` | spawn `dev-agent` |
| `qa_rule` | `code_review_qa_rule` | `current_stage.name == 'code_review_qa'` AND `state == 'in_progress'` AND `_is_leaf(task)` | spawn `qa-reviewer` |
| `leaf_park_rule` | `leaf_park_rule` | `current_stage.name == 'code_review_qa'` AND `state == 'done'` AND `_is_leaf(task)` | advance to `holistic_qa needs_doing` (parent only) |
| `all_leaves_holistic_rule` | `all_leaves_holistic_rule` | epic with all children parked/terminal | advance epic's `holistic_qa` to `in_progress` |
| `holistic_rule` | `holistic_qa_rule` | `current_stage.name == 'holistic_qa'` AND `state == 'in_progress'` | spawn `holistic-qa` (Phase 4 wires `pr` advance) |
| `pr_rule` | (Phase 4) | — | (Phase 4) |
| `merge_rule` | (Phase 4) | — | (Phase 4) |

For each retained rule, port the existing attempt-count helpers to read `StageState.attempt_count` instead of artifact counters (`qa_attempts`, etc.). The artifact counter columns (`max_qa_rounds`, etc.) stay as caps; only the per-attempt counter is moved into the manifest row.

`_is_unattended(task)` continues to read `task.assigned_agent`; that field is retained and still drives the unattended-fallback branch in `_fallback`.

**Acceptance:**

- A3.1.1 — `task_has_stage` and `current_stage` helpers added; `_stage_skipped` and `_state` deleted. file: `src/gobby/dispatch/rules.py`. symbol: `gobby.dispatch.rules.task_has_stage`, `gobby.dispatch.rules.current_stage`.
- A3.1.2 — Each rule in the table above is renamed and rewritten to query the manifest; old `_advance(...)` calls replaced with `StageStatesManager` writes. test: `tests/dispatch/test_rules_stage_native.py::test_rule_table_complete`.
- A3.1.3 — `Task.stages` denormalized field populated by `reload_candidate`. file: `src/gobby/dispatch/dispatcher.py`. test: `tests/dispatch/test_reload_candidate_includes_stages.py::test_stages_loaded`.
- A3.1.4 — Attempt-count helpers read from `StageState.attempt_count`, with `max_qa_rounds`-style caps still honored. test: `tests/dispatch/test_rules_stage_native.py::test_attempt_caps_honored`.
- A3.1.5 — Pass-through escalate-no-agent rule for `expansion_qa` until an agent is registered (no silent wait). test: `tests/dispatch/test_rules_stage_native.py::test_no_agent_stage_escalates`.

### 3.2 Manifest resolution at build time + readiness projections rewrite [category: code] (depends: 2.1, 2.2, 3.1)
`kind: deliverable`

Target: `src/gobby/build/service.py`, `src/gobby/storage/tasks/_blocking.py`, `src/gobby/storage/tasks/_aggregates.py`, `src/gobby/dispatch/dispatcher.py`

`gobby build` flow rewrite. Build resolves the task's manifest before the dispatcher ever sees it:

1. Read `task.task_type`, fetch defaults via `StageRegistryManager.list_default_stages(task_type)`.
2. Apply CLI/MCP/HTTP flag overrides (`--stages`, `--add-stage`, `--skip-stage`, profiles `quick|full|full-yolo`).
3. Call `StageStatesManager.initialize_manifest(task_id, resolved_stages, ...)`.
4. Set `allow_automation=True`, `yolo` per profile, `isolation` per profile.
5. Return `BuildResult` with the resolved manifest in `manifest` field for caller display.

Profile → flag bundle resolution:

```python
PROFILE_BUNDLES: dict[str, ProfileBundle] = {
    "quick":     ProfileBundle(skip=["adversarial_review", "expansion_qa", "holistic_qa"]),
    "review":    ProfileBundle(skip=[]),  # default
    "full":      ProfileBundle(skip=[]),
    "full-yolo": ProfileBundle(skip=[], yolo=True),
}
```

Profile bundles resolve to `--skip-stage` arguments that get applied alongside any explicit `--skip-stage` flags.

Readiness projections rewrite:

- `list_ready_tasks` (storage layer) — old: `WHERE status='open' AND ...`. New: `WHERE NOT is_closed AND NOT is_escalated AND NO unresolved blocker AND current_stage IS NOT NULL AND current_stage.state IN ('needs_doing','in_progress')`. Implementation: subquery against `task_stage_states` for `current_stage`; filter by `closed_at IS NULL`, `is_escalated = 0` (Phase 5 backfills the column), and the existing blocker join.
- `list_blocked_tasks` — old: relied on `status='escalated'`-as-block plus dependency checks. New: `is_escalated = 1 OR active_blocked_by IS NOT EMPTY`. Excludes parent tasks blocked only by their own descendants (existing behavior; preserve the `_filter_completion_blocks` helper).
- `suggest_next_task` — same readiness criteria as `list_ready_tasks`, sorted by priority + age.
- `list_automation_candidates` — old: `WHERE allow_automation=true AND status IN ('open','in_progress','needs_review','review_approved')`. New: `WHERE allow_automation=true AND NOT is_closed AND NOT is_escalated AND current_stage.state IN ('needs_doing','in_progress')`.

For each rewritten projection, write a contract test that runs the OLD model on a fixture DB, runs the NEW model on the same fixture DB after backfill, and asserts identical task ID sets. This is the load-bearing equivalence guarantee from the strategy plan.

`reload_candidate` (`src/gobby/dispatch/dispatcher.py:54-138` area) loads `Task.stages` via a JOIN against `task_stage_states ORDER BY position` and packs into `Task.stages` tuple.

**Acceptance:**

- A3.2.1 — `gobby build` writes the resolved manifest via `initialize_manifest` and returns it in `BuildResult`. file: `src/gobby/build/service.py`. test: `tests/build/test_build_resolves_manifest.py::test_default_manifest`.
- A3.2.2 — Profile bundles `quick`, `review`, `full`, `full-yolo` resolve to declared skip lists. test: `tests/build/test_build_profiles.py::test_quick_skips_adversarial_and_qa`.
- A3.2.3 — `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates` rewritten to manifest reads. file: `src/gobby/storage/tasks/_blocking.py`, `src/gobby/storage/tasks/_aggregates.py`. test: `tests/storage/tasks/test_readiness_equivalence.py::test_old_vs_new_identical`.
- A3.2.4 — `reload_candidate` populates `Task.stages` in a single SQL round-trip. test: `tests/dispatch/test_reload_candidate_n1.py::test_no_n1_query`.

## Phase 4: PR / Merge / Review Stage Cutover
`kind: framing`

**Goal**: Land the stage-native PR and merge rules with their delivery artifacts. After Phase 4, #13552 (PR-Agent) and #13560-class merge work can target the new stage contract.

### 4.1 PR stage rule + delivery artifacts [category: code] (depends: 3.1)
`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`, `src/gobby/storage/tasks/_artifacts.py`, `src/gobby/mcp_proxy/tools/tasks/_stages.py`

Add `pr_rule`. Until #13552 lands the PR-Agent, this rule escalates `pr.state == 'in_progress'` with reason `pr_no_agent` so the work surfaces to a human; once the agent is wired (out of scope here), the rule spawns it.

Stage transitions during PR work:

1. `holistic_qa.state` becomes `done` → `pr.state` transitions `needs_doing → in_progress` (via `_advance_to_next_stage`).
2. PR opened: agent or operator calls `record_pr_verdict(task_id, verdict='needs_changes', findings='PR opened', report_ref=pr_url)` to seed `pr_url` artifact, OR more naturally: a new tool `record_pr_opened(task_id, pr_url)` writes `pr_url` and `github_pr_number` artifacts without changing stage state.
3. PR review verdict: `record_pr_verdict(task_id, verdict='approved'|'rejected'|'needs_changes', findings, report_ref?)`. Writes `structured_pr_verdict` (JSON) and `pr_review_report`. On `approved`, completes `pr` stage; on `rejected` or `needs_changes`, fails the stage with `attempt_count + 1`.
4. `pr.state` becomes `done` → `merge.state` transitions `needs_doing → in_progress`.

`record_pr_opened` tool (added on top of the Phase 2.3 set):

```python
def record_pr_opened(task_id: str, pr_url: str, github_pr_number: int | None = None) -> dict[str, Any]:
    """Persist PR metadata on task_artifacts without changing pr stage state.
    
    The pr stage stays at in_progress; verdict capture happens via
    record_pr_verdict. Idempotent: re-recording the same pr_url is a no-op.
    """
```

`pr_rule` body:

```python
def pr_rule(task: Task, context: RuleContext) -> Action | None:
    stage = current_stage(task)
    if stage is None or stage.name != "pr":
        return None
    if stage.state != "in_progress":
        return None
    if not _has_pr_agent(context):
        return EscalateAction(task_id=task.id, reason="pr_no_agent")
    return _spawn_stage_agent(task, stage, context, "pr-agent")
```

`_has_pr_agent` checks the agent registry for a stage-aware `pr-agent`; if missing, escalates so #13552's owner can pick up the work.

**Acceptance:**

- A4.1.1 — `pr_rule` registered in the rules list at the right position. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_pr_rule.py::test_pr_rule_in_list`.
- A4.1.2 — `record_pr_verdict` with `verdict='approved'` completes `pr` stage and writes both artifacts. test: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_writes_artifacts`.
- A4.1.3 — `record_pr_opened` writes `pr_url` and `github_pr_number` without changing stage state. file: `src/gobby/mcp_proxy/tools/tasks/_stages.py`. test: `tests/mcp_proxy/tools/tasks/test_record_pr_opened.py::test_idempotent`.
- A4.1.4 — `pr.state == done` triggers `merge.state == needs_doing → in_progress` advance via the rule chain on next heartbeat. test: `tests/dispatch/test_pr_to_merge_advance.py::test_pr_done_advances_merge`.
- A4.1.5 — Without a registered `pr-agent`, `pr_rule` escalates with reason `pr_no_agent`. test: `tests/dispatch/test_pr_rule.py::test_escalates_when_no_agent`.

### 4.2 Merge stage rule + delivery artifacts [category: code] (depends: 4.1)
`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`, `src/gobby/mcp_proxy/tools/tasks/_stages.py`

Add `merge_rule`. Mirrors `pr_rule`'s escalate-without-agent fallback. The terminal close is the merge rule's responsibility: once `merge.state` becomes `done`, the rule advances no further (manifest exhausted) and triggers a task close via `close_task(task_id, reason='merge_complete', commit_sha=<merge_sha>)`.

```python
def merge_rule(task: Task, context: RuleContext) -> Action | None:
    stage = current_stage(task)
    if stage is None or stage.name != "merge":
        return None
    if stage.state != "in_progress":
        return None
    if not _has_merge_agent(context):
        return EscalateAction(task_id=task.id, reason="merge_no_agent")
    return _spawn_stage_agent(task, stage, context, "merge-orchestrator")
```

`record_merge_result` tool (Phase 2.3 stub; full implementation here):

```python
def record_merge_result(
    task_id: str,
    *,
    merge_sha: str | None = None,
    report_ref: str | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Persist merge outcome and advance/fail the merge stage.

    Success path: merge_sha required. Writes merge_commit_sha and
    merge_campaign_report to task_artifacts; completes merge stage; calls
    close_task(reason='merge_complete'). Cascades close per existing
    mark_task_merged behavior.

    Failure path: failure_reason required. Writes merge_campaign_report;
    fails merge stage with attempt_count + 1; if attempt_count exceeds
    max_merge_attempts artifact cap, escalates with reason 'merge_failed:max'.
    """
```

The cascade-close behavior from `mark_task_merged` (`src/gobby/storage/tasks/_transitions.py:655-674`) MUST be preserved. The new `record_merge_result` tool delegates close-with-cascade to the existing close helper.

`expansion_qa_rule`, `code_review_qa_rule`, `holistic_qa_rule` — each checks for its agent in the context and either spawns or escalates with `<stage>_no_agent`. These rules already exist in skeleton form from Phase 3.1; this section extends them to use the same `_has_<stage>_agent(context)` pattern as `pr_rule`/`merge_rule` so missing agents surface uniformly.

**Acceptance:**

- A4.2.1 — `merge_rule` registered in the rules list. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_merge_rule.py::test_merge_rule_in_list`.
- A4.2.2 — `record_merge_result(merge_sha=...)` completes `merge` stage, writes `merge_commit_sha` + `merge_campaign_report`, and closes the task with cascade. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_success_closes_task`.
- A4.2.3 — `record_merge_result(failure_reason=...)` fails the stage; over the cap, escalates. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_failure_path`.
- A4.2.4 — `expansion_qa_rule`, `code_review_qa_rule`, `holistic_qa_rule` all surface missing-agent escalations with stage-specific reason codes. test: `tests/dispatch/test_qa_rules_no_agent.py::test_each_qa_rule_escalates_specifically`.
- A4.2.5 — End-to-end stage chain `holistic_qa.done → pr.in_progress → pr.done → merge.in_progress → merge.done → task closed` walks correctly across heartbeats. test: `tests/dispatch/test_delivery_chain.py::test_full_delivery_chain`.

## Phase 5: Task Type Expansion + Legacy Removal
`kind: framing`

**Goal**: Add the new task types, promote `is_escalated` to a first-class column, and rip out the legacy `lifecycle`/`status`/`lifecycle_stage` columns and the projection helpers. This phase closes the legacy model.

### 5.1 New task types + default-stages seed [category: code] (depends: 2.1, 2.2)
`kind: deliverable`

Target: `src/gobby/install/shared/registry/stages.yaml` (extension), `src/gobby/storage/tasks/_models.py`, `src/gobby/cli/tasks/crud.py`, `src/gobby/storage/migrations.py`

Add four new task types and their default-stages bundles. Migration version 223:

```python
NEW_TASK_TYPE_DEFAULTS = {
    "simple_fix":       ["development", "pr", "merge"],
    "research_spike":   ["ideation", "research", "prd"],            # terminal at prd, no merge
    "architecture_doc": ["research", "architecture"],               # terminal at architecture
    "prd_doc":          ["ideation", "prd"],                        # terminal at prd
}
```

Update `Task.task_type` validation in `_models.py` to accept the new values. The current docstring (`src/gobby/storage/tasks/_models.py:148`) lists `bug, feature, task, epic, chore, refactor`; extend with the four new types. Add a `VALID_TASK_TYPES` module constant alongside existing validations (or inline as a frozenset literal — match nearby patterns).

Update `gobby tasks create --type <type>` Click choices in `src/gobby/cli/tasks/crud.py` to include the new types. Same for `TaskCreateRequest.task_type` validation in HTTP route models and the `create_task` MCP tool's `inputSchema`.

For research-terminal types (`research_spike`, `prd_doc`): terminal-stage detection is "manifest does not include `merge`". The dispatcher's terminal-close logic (Phase 4.2) handles non-merge terminal cases by closing the task when the last manifest row (whatever its name) becomes `done`. Add a unit test fixture with a `research_spike` task that walks `ideation → research → prd → closed`.

**Acceptance:**

- A5.1.1 — Four new task types accepted by `Task.task_type` validation. file: `src/gobby/storage/tasks/_models.py`. test: `tests/storage/tasks/test_task_type_validation.py::test_new_types_accepted`.
- A5.1.2 — `task_type_default_stages` seeded with the four new defaults via migration 223. test: `tests/storage/test_migration_223.py::test_new_task_type_defaults`.
- A5.1.3 — CLI, HTTP, and MCP creation surfaces all accept the new types. test: `tests/cli/test_tasks_create_new_types.py::test_create_simple_fix`, `tests/servers/routes/test_tasks_create.py::test_post_simple_fix`, `tests/mcp_proxy/tools/tasks/test_create_task.py::test_simple_fix_type`.
- A5.1.4 — A `research_spike` task with manifest `[ideation, research, prd]` walks to `prd.done` and the task closes terminally. test: `tests/dispatch/test_terminal_non_merge.py::test_research_spike_closes_at_prd`.

### 5.2 Promote `is_escalated` to first-class column [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/storage/migrations.py` (migration 224), `src/gobby/storage/tasks/_models.py`, `src/gobby/storage/tasks/_transitions.py`, `src/gobby/tasks/state_semantics.py`

Migration 224: backfill `tasks.is_escalated` from existing data. The `is_escalated` column was created in migration 221 (Phase 1.1) on `task_artifacts`; this migration moves it onto `tasks` proper.

Rationale for placement: escalation is task-level, not artifact-level. `task_artifacts` is sparse evidence; `tasks` is the row that gets read on every list. Add `is_escalated INTEGER NOT NULL DEFAULT 0` to the `tasks` table and drop it from `task_artifacts` in the same migration.

Backfill: `UPDATE tasks SET is_escalated = 1 WHERE escalated_at IS NOT NULL`.

Update `Task` dataclass: add `is_escalated: bool = False` field. Update every read site that currently calls `is_task_escalated(task)` (`src/gobby/tasks/state_semantics.py:98-105`) to read `task.is_escalated` directly. Keep the old helper only as a one-line `return task.is_escalated`; Phase 5.3 deletes it.

`escalate_task` and `de_escalate_task` (`src/gobby/storage/tasks/_transitions.py:391-451`) update `is_escalated` alongside `escalated_at` / `escalation_reason`. Atomic single transaction.

**Acceptance:**

- A5.2.1 — Migration 224 adds `is_escalated` to `tasks`, drops it from `task_artifacts`, and backfills from `escalated_at`. file: `src/gobby/storage/migrations.py`. test: `tests/storage/test_migration_224_escalated.py::test_backfill_correct`.
- A5.2.2 — `Task.is_escalated` field present and populated on read. file: `src/gobby/storage/tasks/_models.py`. test: `tests/storage/tasks/test_task_dataclass.py::test_is_escalated_field`.
- A5.2.3 — `escalate_task` sets `is_escalated=1`; `de_escalate_task` sets `is_escalated=0`. test: `tests/storage/tasks/test_transitions_is_escalated.py::test_escalate_round_trip`.
- A5.2.4 — Readers in dispatcher, projections, and HTTP responses use `task.is_escalated` directly. test: `tests/dispatch/test_is_escalated_first_class.py::test_no_helper_calls`.

### 5.3 Drop `lifecycle`, `lifecycle_stage`, active `status` semantics [category: code] (depends: 3.1, 3.2, 4.1, 4.2, 5.2)
`kind: deliverable`

Target: `src/gobby/storage/migrations.py` (migration 225), `src/gobby/storage/tasks/_models.py`, `src/gobby/storage/tasks/_transitions.py`, `src/gobby/tasks/state_semantics.py`, `src/gobby/storage/tasks/_lifecycle.py`, `src/gobby/mcp_proxy/tools/tasks/_lifecycle_merge.py`, `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py`, `src/gobby/cli/tasks/crud.py`, `src/gobby/servers/routes/tasks.py`

Migration 225 drops legacy columns. Pre-flight: assert no rule, MCP tool, HTTP route, or CLI command writes `lifecycle`, `lifecycle_stage`, or active `status` values (Phases 3 and 4 must have completed). The migration runs in a transaction:

1. `ALTER TABLE tasks DROP COLUMN lifecycle;`
2. `ALTER TABLE tasks DROP COLUMN lifecycle_stage;`
3. Active `status` values become forbidden. The `status` column itself stays (storing only `'closed'` for closed tasks; `closed_at IS NOT NULL` is the canonical read), OR the column is dropped entirely if the audit confirms no read sites remain. Choose drop unless an audit surfaces a hard reader.

If `status` column is dropped: `tasks.status` reads in MCP/HTTP responses are replaced with computed `is_closed` from `closed_at`. The `Task.status` Literal field is removed; its callers are updated.

Tools and helpers to delete (after final rule-rewrite checks):

- `mark_task_pr_opened` (storage `_transitions.py:636-652`, MCP `_lifecycle_merge.py:23-34`)
- `mark_task_merged` (storage `_transitions.py:655-674`, MCP `_lifecycle_merge.py:60-80`)
- `mark_task_merge_failed` (storage `_transitions.py:677-720` area, MCP `_lifecycle_merge.py:115-138`)
- `advance_lifecycle` (`_transitions.py:214-280`)
- `Lifecycle` StrEnum (`_models.py:42-51`)
- `TaskLifecycleStage` Literal (`state_semantics.py:7`)
- `lifecycle_stage_from_status` (`state_semantics.py:45-49`)
- `normalize_lifecycle_stage` (`state_semantics.py:52-63`)
- `project_legacy_status` (`state_semantics.py:66-85`)
- `_coerce_task_lifecycle_stage` (`state_semantics.py:175-192`)
- `serialize_task_state` returns no `lifecycle_stage` field; rewrite to expose `current_stage`, `is_closed`, `is_escalated`, `is_blocked`, `owner_session_id` only.

CLI flag removals: `gobby tasks list --status` and `--lifecycle` are deleted (Phase 2.5 added their replacements; the old flags are now removed). HTTP filter param removals: `?status=...` and `?lifecycle=...` query params are deleted from the list endpoint.

`mark_task_review_approved`, `mark_task_review_rejected`, `mark_task_needs_review` MCP tools (`src/gobby/storage/tasks/_transitions.py:454-536` and friends) — these wrote `status` values for review state. Replace each with a thin shim that calls `complete_stage` / `fail_stage` on the current stage. Keep the tool surface; rewire the implementation. This preserves the agent-facing API contract (the `_agent_blocked_mcp_tools` rule already gates these for spawned agents).

`escalated` is preserved as a first-class flag (Phase 5.2). `closed` continues to be the only `status` write that survives if the column is retained, OR the column is dropped entirely.

**Acceptance:**

- A5.3.1 — Migration 225 drops `lifecycle` and `lifecycle_stage` from `tasks`. file: `src/gobby/storage/migrations.py`. test: `tests/storage/test_migration_225_drop_legacy.py::test_columns_dropped`.
- A5.3.2 — `Lifecycle` StrEnum, `TaskLifecycleStage` Literal, and the projection helpers in `state_semantics.py` are deleted. file: `src/gobby/tasks/state_semantics.py`, `src/gobby/storage/tasks/_models.py`. test: grep-based regression `tests/test_legacy_symbols_removed.py::test_no_lifecycle_imports`.
- A5.3.3 — Legacy lifecycle MCP tools (`mark_task_pr_opened`, `mark_task_merged`, `mark_task_merge_failed`, `advance_lifecycle`) are removed. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_merge.py`, `src/gobby/storage/tasks/_transitions.py`. test: `tests/mcp_proxy/tools/tasks/test_legacy_tools_removed.py::test_tools_absent`.
- A5.3.4 — `mark_task_review_approved` / `mark_task_review_rejected` / `mark_task_needs_review` rewired to stage-native calls; surface contract preserved. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_review_approved_completes_stage`.
- A5.3.5 — CLI `--status`/`--lifecycle` flags and HTTP `?status=`/`?lifecycle=` filters are removed. test: `tests/cli/test_legacy_flags_removed.py::test_status_flag_unknown`, `tests/servers/routes/test_legacy_filters_removed.py::test_status_filter_400`.
- A5.3.6 — `serialize_task_state` returns the new shape without `lifecycle_stage`. file: `src/gobby/tasks/state_semantics.py`. test: `tests/tasks/test_serialize_task_state.py::test_new_shape`.

### 5.4 Discovery-stage agent follow-up tracking [category: planning] (depends: 1.3)
`kind: deliverable`

Target: gobby-tasks (no source files; this deliverable creates real tracked tasks via the gobby-tasks MCP server during plan execution)

The four placeholder agents from 1.3 are explicit shims; real implementations are out of scope for this epic. Create one parent epic plus four child tracking tasks so the work is visible in the task tree, picked up by future planning sessions, and re-discoverable from the registry's `default_agent` slot.

Parent epic:

```text
title: "Discovery-stage agent registry"
task_type: epic
category: planning
priority: 2
labels:
  - "deferred-from:task-13482-stage-manifest-cutover:5.4"
description: |
  Owns the four discovery-stage agents (analyst, researcher, architect,
  product-manager) shipped as disabled placeholders by the stage-manifest
  cutover (#13482). Each child task replaces one placeholder YAML with a real
  implementation. Stages affected:

  - ideation     → analyst         (placeholder at src/gobby/install/shared/workflows/agents/analyst.yaml)
  - research     → researcher      (placeholder at src/gobby/install/shared/workflows/agents/researcher.yaml)
  - architecture → architect       (placeholder at src/gobby/install/shared/workflows/agents/architect.yaml)
  - prd          → product-manager (placeholder at src/gobby/install/shared/workflows/agents/product-manager.yaml)

  Acceptance for the parent: every child closed; every placeholder YAML
  replaced with `enabled: true` real impl; `tests/dispatch/test_no_agent_paths.py`
  no longer needs the placeholder fixture.
```

Each of the four children has the shape:

```text
title:       "Implement <agent-slug> agent for <stage> stage"
task_type:   feature
category:    planning
priority:    3
parent_task_id: <parent epic ref>
labels:
  - "deferred-from:task-13482-stage-manifest-cutover:5.4"
  - "agent-followup:<agent-slug>"
description: |
  Replace the disabled placeholder at
  `src/gobby/install/shared/workflows/agents/<agent-slug>.yaml` with a real
  agent implementation. Acceptance:

  - YAML has `enabled: true` and a real `instructions` block.
  - PLACEHOLDER banners removed.
  - Stage `<stage>` no longer escalates with reason
    `<stage>_no_agent` or `placeholder_agent:<slug>:not_implemented` when
    a task reaches it.
  - At least one fixture or e2e test exercises the agent end-to-end.
```

The four `(stage, agent-slug)` pairs to create:

| Stage | Agent slug |
|-------|-----------|
| `ideation` | `analyst` |
| `research` | `researcher` |
| `architecture` | `architect` |
| `prd` | `product-manager` |

Implementation note for the executing agent: use the `create_task` MCP tool on `gobby-tasks-ops` for each task. Create the parent first, capture its ref/id, then create each child with `parent_task_id` set. Apply labels via `add_label` (or include in initial creation if the schema supports it). Verify all five tasks land in the same project (`d45545c5-ded5-4335-b115-0245752edacf`) and surface the parent ref to the operator on completion.

This deliverable does **not** open or implement any of the agents — only creates the tracking tasks. Real implementation work happens in later planning rounds spawned from the new parent epic.

**Acceptance:**

- A5.4.1 — One parent epic exists in gobby-tasks titled `Discovery-stage agent registry` with the declared labels and description. behavior: "epic exists with deferred-from label and references all four placeholders" verified in `tests/dispatch/test_agent_followup_tasks.py::test_parent_epic_exists` (post-execution fixture seeded by the executing agent).
- A5.4.2 — Four child tasks exist under the parent, one per `(stage, agent-slug)` pair, each carrying the `agent-followup:<slug>` and `deferred-from:` labels. test: `tests/dispatch/test_agent_followup_tasks.py::test_four_children_with_labels`.
- A5.4.3 — Every child task references the exact placeholder YAML path in its description. behavior: "each child description names src/gobby/install/shared/workflows/agents/<slug>.yaml verbatim" verified in `tests/dispatch/test_agent_followup_tasks.py::test_descriptions_reference_placeholders`.
- A5.4.4 — Children are open (`is_closed=false`) so future planning rounds can pick them up. test: `tests/dispatch/test_agent_followup_tasks.py::test_children_open`.

## Phase 6: Web UI — LifecycleBoard
`kind: framing`

**Goal**: Replace the 6-bucket `KanbanBoard` with a stage-manifest-driven `LifecycleBoard`. After Phase 6, the kanban view renders one column per registry stage with tri-state badges per task and drag-to-advance hooked into `PATCH /api/tasks/{id}/stages/{name}`.

### 6.1 New `LifecycleBoard.tsx` + `StageColumn.tsx` + `StageCard.tsx` [category: code] (depends: 2.4)
`kind: deliverable`

Target: `web/src/components/tasks/LifecycleBoard.tsx`, `web/src/components/tasks/StageColumn.tsx`, `web/src/components/tasks/StageCard.tsx` (all new)

Replicate the props pattern of `KanbanBoard` (`web/src/components/tasks/KanbanBoard.tsx`) but driven by registry stages instead of fixed buckets. Use the existing `@atlaskit/pragmatic-drag-and-drop` library — it's already wired (`web/package.json` v1.7.7) and handles draggable cards + drop targets in `KanbanBoard`.

```typescript
// LifecycleBoard.tsx
interface LifecycleBoardProps {
  tasks: GobbyTask[]
  registry: StageRegistryEntry[]
  onSelectTask: (id: string) => void
  onAdvanceStage?: (taskId: string, stageName: string) => void
  onFailStage?: (taskId: string, stageName: string, reason: string) => void
}

export function LifecycleBoard({
  tasks, registry, onSelectTask, onAdvanceStage, onFailStage,
}: LifecycleBoardProps) {
  // Filter columns to those any visible task has in its manifest (configurable
  // via showAllColumns prop; default off).
  const visibleStages = useMemo(
    () => registry.filter(s => tasks.some(t => t.stages?.some(r => r.stage_name === s.name))),
    [tasks, registry],
  )
  return (
    <div className="lifecycle-board">
      {visibleStages.map(stage => (
        <StageColumn
          key={stage.name}
          stage={stage}
          tasks={tasks.filter(t => taskAtStage(t, stage.name))}
          onSelectTask={onSelectTask}
          onAdvanceStage={onAdvanceStage}
        />
      ))}
    </div>
  )
}
```

```typescript
// StageColumn.tsx
function StageColumn({ stage, tasks, ... }: StageColumnProps) {
  // Group tasks by tri-state within the column.
  const grouped = {
    needs_doing: tasks.filter(t => taskStateAt(t, stage.name) === 'needs_doing'),
    in_progress: tasks.filter(t => taskStateAt(t, stage.name) === 'in_progress'),
    done:        tasks.filter(t => taskStateAt(t, stage.name) === 'done'),
  }
  // needs_doing top (pale), in_progress middle (accent), done bottom (collapsed
  // by default via toggle).
}
```

```typescript
// StageCard.tsx
function StageCard({ task, stageName, columnState, ... }: StageCardProps) {
  const isBlocked = task.state?.is_blocked
  // Blocked tasks render with a blocked badge/overlay; they stay in their
  // current stage column and do NOT move to a synthetic blocked column.
  // Drag right = onAdvanceStage(task.id, stageName).
}
```

Helpers (in `web/src/lib/taskState.ts` — extending it; Phase 6.3 retires the legacy parts):

```typescript
export function taskAtStage(task: GobbyTask, stageName: string): boolean {
  return task.stages?.some(r => r.stage_name === stageName) ?? false
}

export function taskStateAt(task: GobbyTask, stageName: string): StageRowState | undefined {
  return task.stages?.find(r => r.stage_name === stageName)?.state
}

export function currentStage(task: GobbyTask): { name: string; state: StageRowState } | null {
  // Leftmost row by position whose state != 'done'.
}
```

Swimlanes by `task_type`: render one row per distinct `task_type` in the visible task set. Within each lane, render the columns. Empty lanes are hidden.

The `done` group within each column collapses by default to one summary row showing the count; click to expand. Reuses the `details/summary` HTML pattern or a small toggle component — match nearby disclosure patterns in `web/src/components/`.

**Acceptance:**

- A6.1.1 — Three new components exist with the declared prop shapes. file: `web/src/components/tasks/LifecycleBoard.tsx`, `web/src/components/tasks/StageColumn.tsx`, `web/src/components/tasks/StageCard.tsx`. symbol: `LifecycleBoard`, `StageColumn`, `StageCard`.
- A6.1.2 — Columns render only the stages present in any visible task's manifest. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_visible_stage_filtering`.
- A6.1.3 — Tri-state grouping renders within each column with `done` collapsed by default. test: `web/src/components/tasks/__tests__/StageColumn.test.tsx::test_tri_state_grouping`.
- A6.1.4 — Blocked tasks render in their current column with a blocked badge. test: `web/src/components/tasks/__tests__/StageCard.test.tsx::test_blocked_badge`.
- A6.1.5 — Drag right on a card calls `onAdvanceStage(task.id, stageName)`. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_drag_advance`.
- A6.1.6 — Swimlanes by `task_type` render with empty lanes hidden. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_swimlanes`.

### 6.2 useTasks denormalized stage manifest + new filters [category: code] (depends: 2.4, 6.1)
`kind: deliverable`

Target: `web/src/hooks/useTasks.ts`, `web/src/hooks/useStagesRegistry.ts` (new)

Extend `GobbyTask` (`web/src/hooks/useTasks.ts:10-45` area) with:

```typescript
export interface StageStateView {
  stage_name: string
  position: number
  state: 'needs_doing' | 'in_progress' | 'done'
  attempt_count: number
  artifact_refs: Record<string, string> | null
}

export interface GobbyTask {
  // existing fields...
  stages?: StageStateView[]  // populated by GET /api/tasks?include_stages=1
}
```

Update `fetchTasks` in `useTasks` to pass `include_stages=1` whenever the kanban view is mounted. Add `stage` and `stage_state` query params to `buildParams` for filtered fetches. Mutation helpers gain:

```typescript
async function advanceStage(taskId: string, stageName: string): Promise<void> {
  await fetch(`${baseUrl}/api/tasks/${taskId}/stages/${stageName}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'complete' }),
  })
}

async function failStage(taskId: string, stageName: string, reason: string): Promise<void> { ... }
async function startStage(taskId: string, stageName: string): Promise<void> { ... }
```

New hook `useStagesRegistry` fetches `GET /api/stages/registry` once on mount, caches in module-level state, and returns `{registry, isLoading, error}`.

WebSocket `task_event` handler (`useTasks` line ~280-295 area) re-fetches on `stage_changed` events as well as existing `task_event` types. Backend already broadcasts these per A2.4.5.

**Acceptance:**

- A6.2.1 — `GobbyTask.stages` field populated when `include_stages=1` query param is set. file: `web/src/hooks/useTasks.ts`. test: `web/src/hooks/__tests__/useTasks.test.ts::test_stages_populated`.
- A6.2.2 — `advanceStage`, `failStage`, `startStage` mutators call the correct PATCH endpoints. test: `web/src/hooks/__tests__/useTasks.test.ts::test_stage_mutators`.
- A6.2.3 — `useStagesRegistry` fetches once and caches. file: `web/src/hooks/useStagesRegistry.ts`. test: `web/src/hooks/__tests__/useStagesRegistry.test.ts::test_caches_response`.
- A6.2.4 — `stage_changed` WS events trigger task re-fetch. test: `web/src/hooks/__tests__/useTasks.test.ts::test_ws_stage_changed_refetches`.

### 6.3 Mount LifecycleBoard, retire `taskState.ts` legacy types [category: code] (depends: 6.1, 6.2)
`kind: deliverable`

Target: `web/src/components/tasks/TasksPage.tsx`, `web/src/lib/taskState.ts`, `web/src/components/tasks/KanbanBoard.tsx`, `web/src/components/tasks/__tests__/KanbanBoard.test.tsx`

Replace the `viewMode === 'kanban'` branch in `TasksPage.tsx` (lines ~476-482) with `LifecycleBoard`. Pass `tasks`, the registry from `useStagesRegistry`, and the new `advanceStage`/`failStage` mutators from `useTasks`.

```typescript
} : viewMode === 'kanban' ? (
  <LifecycleBoard
    tasks={subtreeRootId ? kanbanTasks : displayTasks}
    registry={registry}
    onSelectTask={setSelectedTaskId}
    onAdvanceStage={advanceStage}
    onFailStage={failStage}
  />
) : ...
```

Delete from `web/src/lib/taskState.ts`:

- `TaskLifecycleStage` (line ~1)
- `TaskBucket` (lines ~28-34)
- `TASK_BUCKET_LABELS` (lines ~48-55)
- `TASK_BUCKET_ORDER` (lines ~36-46)
- `getTaskBucket` (lines ~163-173)
- `_resolveLifecycleStage` and any other lifecycle helpers

Keep `CanonicalTaskState` minus the `lifecycle_stage` field; rename to make the omission obvious if useful (`TaskState` works). Keep `getCanonicalTaskState` for reading task state for badges.

Delete `web/src/components/tasks/KanbanBoard.tsx` and its test file `KanbanBoard.test.tsx`. The new `LifecycleBoard` test file from 6.1 replaces them.

The `moveTaskToBucket` function in `TasksPage.tsx` (lines ~142-190) is replaced by inline `advanceStage` / `failStage` calls bound to drag handlers in `LifecycleBoard`. Existing per-bucket transition functions (`reopenTask`, `deEscalateTask`, `claimTask`, `markTaskNeedsReview`, `markTaskReviewApproved`, `escalateTask`, `closeTask`) are no longer wired to drag — their other callers stay (sidebar buttons, modals, etc.).

**Acceptance:**

- A6.3.1 — `TasksPage.tsx` mounts `LifecycleBoard` for `viewMode === 'kanban'`. file: `web/src/components/tasks/TasksPage.tsx`. test: `web/src/components/tasks/__tests__/TasksPage.test.tsx::test_kanban_mode_renders_lifecycle_board`.
- A6.3.2 — `taskState.ts` legacy symbols deleted. file: `web/src/lib/taskState.ts`. test: TypeScript compile passes; grep regression `web/src/__tests__/test_legacy_symbols_removed.test.ts::test_no_task_bucket_imports`.
- A6.3.3 — `KanbanBoard.tsx` and `KanbanBoard.test.tsx` are deleted. behavior: "old kanban component absent" verified by `git status`/`grep -r "KanbanBoard"` returning no source matches in `web/src/`.
- A6.3.4 — `pnpm build` succeeds; `pnpm test` runs `LifecycleBoard.test.tsx` instead of the deleted `KanbanBoard.test.tsx`. test: CI pipeline output shows new file in coverage.

## Phase 7: Cleanup
`kind: framing`

**Goal**: Remove deprecated `stage-:<name>` label handling, temporary migration helpers, and dead lifecycle/status code. Documentation pass.

### 7.1 Remove `stage-:<name>` label handling and migration helpers [category: refactor] (depends: 5.3, 6.3)
`kind: deliverable`

Target: `src/gobby/build/service.py`, `src/gobby/dispatch/rules.py`, `src/gobby/storage/migrations.py`, anywhere else `stage-:` appears

Grep `stage-:` across the codebase. Every read site that interpreted these labels (build profile resolution, dispatcher skip checks, CLI/HTTP introspection) must be deleted; the data was migrated to `task_stage_states` in Phase 2.2 and the labels were dropped per A2.2.6.

Specific call sites to scrub (results from current grep, point of departure for the implementing agent):

- `src/gobby/dispatch/rules.py:20` (`_SKIP_PREFIX = "stage-:"`) — delete the constant; ripple through.
- Any helper in `src/gobby/build/service.py` that translated profiles to labels — replace with manifest skip lists (Phase 3.2 already does this; this task removes the legacy fallback).
- Migration helpers in `src/gobby/storage/migrations.py` that read `stage-:` labels (only the backfill helper from Phase 2.2 — keep that one as it's a frozen historical record).

`planning-round:N` and `qa-attempts:N` labels are now redundant (replaced by `attempt_count`). Drop these from every task in a final cleanup migration (version 226). Read sites: any `_front_half.py` references to `PLANNING_ROUND_LABEL_PREFIX` are deleted.

**Acceptance:**

- A7.1.1 — `_SKIP_PREFIX` constant and all `stage-:` reads deleted. file: `src/gobby/dispatch/rules.py`. test: `tests/test_no_stage_skip_labels.py::test_grep_returns_empty`.
- A7.1.2 — Migration 226 drops `planning-round:N` and `qa-attempts:N` labels from every task. file: `src/gobby/storage/migrations.py`. test: `tests/storage/test_migration_226_label_cleanup.py::test_legacy_labels_dropped`.
- A7.1.3 — `PLANNING_ROUND_LABEL_PREFIX` constant deleted; readers updated to read `attempt_count`. file: `src/gobby/mcp_proxy/tools/tasks/_front_half.py`. test: `tests/mcp_proxy/tools/tasks/test_front_half_attempt_count.py::test_no_label_reads`.

### 7.2 Documentation pass [category: docs] (depends: 7.1)
`kind: deliverable`

Target: `CLAUDE.md`, `src/gobby/install/shared/skills/plan-draft/SKILL.md`, `docs/contracts/plan-coverage.md`, `docs/architecture/dispatch.md` (new or extend)

Update written documentation to reflect the manifest model.

`CLAUDE.md` "Dispatch Architecture" section: replace any mention of `lifecycle` / `status` axes with stage-manifest semantics. Specifically the list of fields (`allow_automation`, `yolo`, `isolation`) gains `stages` (manifest) as a peer. Profile bundles documented as Phase 3.2.

`plan-draft/SKILL.md`: refresh the canonical stage list in its "Phasing" guidance to match the registry's 14 stages.

`docs/contracts/plan-coverage.md`: no changes for the coverage contract grammar itself, but if the doc references retired status values (`needs_review` → `code_review_qa.in_progress`, etc.), update those examples to match.

`docs/architecture/dispatch.md` (new file if absent; extend if present): one-page architecture diagram + prose covering: registry → manifest → rule → action chain. Include the canonical stage list and the readiness/blocking projection definition.

Update tests to read documentation references (no tests for prose, but the verification phase below cross-checks).

**Acceptance:**

- A7.2.1 — `CLAUDE.md` "Dispatch Architecture" section reflects the manifest model with no remaining `lifecycle`/`status` semantics. file: `CLAUDE.md`. behavior: "doc names task_stage_states and registry; no `(lifecycle, status)` tuple references in dispatcher prose" verified by manual review noted in PR description.
- A7.2.2 — `plan-draft` skill canonical stage list matches the registry. file: `src/gobby/install/shared/skills/plan-draft/SKILL.md`. behavior: "skill stage list = registry stage list" verified in `tests/skills/test_plan_draft_stage_list.py::test_matches_registry`.
- A7.2.3 — `docs/architecture/dispatch.md` exists and covers registry, manifest, rule chain, readiness projection. file: `docs/architecture/dispatch.md`.

## Verification
`kind: verification`

End-to-end acceptance covers:

- **Schema integrity**: every migration runs forward on a fresh DB and on a fixture DB with representative `(lifecycle, status, labels)` tuples; resulting `task_stage_states` rows match the mapping table A2.2.2.
- **Storage invariants**: position uniqueness, registry FK, transition state machine, attempt-count semantics — all enforced by `StageStatesManager` tests.
- **Type→default-stages resolution**: every existing and new task type resolves to declared defaults via `get_task_type_defaults`.
- **Build-time override merge**: `--stages`, `--add-stage`, `--skip-stage`, profile bundles all compose as documented in 3.2 and 2.5.
- **Readiness equivalence**: contract tests run old vs. new `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates`, and `state.is_blocked` against the same fixture DB and assert identical task ID sets.
- **Dispatcher chain**: full delivery walk `holistic_qa.done → pr.in_progress → pr.done → merge.in_progress → merge.done → task closed` covered end-to-end on a fresh DB.
- **API surface**: `GET /api/tasks?stage=development&stage_state=in_progress` returns expected set; `PATCH /api/tasks/{id}/stages/{name}` enforces tri-state transitions; PR verdict and merge result tools store artifacts and transition stages correctly.
- **Terminal non-merge types**: `research_spike` and `prd_doc` walk to their terminal stage and close cleanly without ever reaching `merge`.
- **UI**: LifecycleBoard renders with seeded registry, drag-to-advance updates state via PATCH, swimlane filter by task_type hides empty rows, and pre-existing migrated tasks render from their stage rows; blocked tasks render with badges in their current column.
- **Performance**: kanban board fetch SQL keeps p99 under existing `KanbanBoard` baseline (denormalized stage manifest in single query, indexed on `(task_id, position)` and `(stage_name, state)`).
- **Dead-code regression**: grep/static tests fail if code writes old `status` / `lifecycle` values or calls removed lifecycle PR/merge tools after the cutover.
- **No regressions**: targeted runs of `tests/dispatch/`, `tests/tasks/`, `tests/storage/`, `tests/servers/routes/`, `tests/mcp_proxy/tools/tasks/`, plus `pnpm test` and `pnpm build` for the web bundle.

## Out of scope
`kind: framing`

- **Real agent behavior for the four discovery stages.** This epic ships disabled placeholder YAMLs (1.3) and tracking tasks (5.4); it does NOT author working `analyst`, `researcher`, `architect`, or `product-manager` agents. That work is owned by the `Discovery-stage agent registry` epic created in 5.4.
- **PR-Agent / rizzler-style PR review behavior** — owned by #13552, which targets the stage contract this epic delivers.
- **Re-implementing existing agents.** `planner`, `plan-adversary`, `test-architect`, `expansion-qa`, `qa-reviewer`, `holistic-reviewer`, `merge-orchestrator`, `merge-worker`, `backend-developer`, `frontend-developer`, `default`, `developer` already exist; they are referenced by the registry's `default_agent` slot but their YAMLs are not modified beyond, at most, comment updates referencing the new stage names.
- Cross-project / multi-tenant kanban.
- Per-stage time tracking, SLAs, due dates.
- Drag-and-drop reordering of stages within a task's manifest. Drag-to-advance state is in scope; drag-to-reorder positions is not.
