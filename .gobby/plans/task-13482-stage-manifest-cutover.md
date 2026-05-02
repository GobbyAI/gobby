<!-- markdownlint-disable MD013 MD033 MD060 -->

# Stage Manifest Cutover — Implementation Plan for #13482

`plan_kind: implementation` — deliverable manifest emitted by plan-adversary on approval.

## Overview

`kind: framing`

Replace gobby's dual-enum task state model (`status` + `lifecycle` + `lifecycle_stage`) with a registry-backed, **5-state-per-stage** manifest model. Every task carries an ordered, task-type-specific manifest of `(stage_name, state)` rows where `state ∈ {ready, in_progress, needs_review, review_approved, done}` (global enum on `task_stage_states.state`); per-row legality is determined by `review_policy ∈ {none, required, optional}` mirrored from the registry at manifest-init. The 11-stage registry is bundled YAML synced to a new `task_stages_registry` table. The dispatcher, MCP/HTTP/CLI surfaces, and the web kanban all migrate to the manifest model. Legacy lifecycle/status columns and active status values are dropped in the same epic — no compatibility shims, no shadow model.

This is the implementation companion to the strategy plan at `.gobby/plans/task-13482-lifecycle-status-kanban.md`. The strategy plan defines the target model; this plan defines the executable steps. Read the strategy plan first if you need the *why* — every section here assumes that context.

## Constraints

`kind: framing`

- **Pre-launch clean cutover.** Do not build compatibility facades or long-lived legacy write paths. Callers move to the stage manifest APIs directly within this epic; old `lifecycle`, `lifecycle_stage`, and active `status` semantics are removed by Phase 5 close.
- **5-state vocabulary, policy-driven legality.** Per-stage state is the global 5-value enum `ready | in_progress | needs_review | review_approved | done` enforced by a CHECK constraint on every `task_stage_states` row regardless of policy. Whether a given transition is *legal* on a given row is determined by the row's `review_policy ∈ {none, required, optional}`. `needs_review` and `review_approved` are rejected on `policy=none` rows; the typed `IllegalStageTransitionError` carries `(stage_name, current_state, attempted_transition, review_policy)` so callers can recover or surface the constraint clearly. `closed` is **never** a stage state — task closure is task-level via `closed_at IS NOT NULL`, fired when the terminal manifest row reaches `done`. Blocking and escalation are orthogonal: a blocked task still has a `current_stage` and that row stays in any of the five states; a blocked-or-escalated task is filtered out of `is_ready` projections by the readiness check, not by injecting a sixth stage-state value. There is **no** `blocked` value in the stage-state enum.
- **`review_approved` is durable, not transient.** A row's `state = 'review_approved'` is a real queue position the dispatcher reads (PR approved-before-merge, expansion-QA approved-before-dev-fanout, planning-approved-before-test-arch). The `review_approved → done` advance is a separate transition (dispatcher-driven for automated stages, operator-driven for human-gated stages); it is NOT collapsed into the review-approval call.
- **Escalation preserves stage state.** `escalate_task` flips `is_escalated=1` and writes `escalated_at`/`escalation_reason`; it does NOT mutate `task_stage_states`. `de_escalate_task` flips `is_escalated=0` and clears the escalation fields; it also does NOT mutate `task_stage_states`. A task that escalates from `development.in_progress`, then de-escalates, resumes at `development.in_progress` with the same `work_attempt_count` / `review_round_count` / `entered_at`. Load-bearing invariant — covered by acceptance 5.2.4.
- **Counter split.** Replace single `attempt_count` with `work_attempt_count` (incremented on `start_stage` / fail-loop reentry) and `review_round_count` (incremented on `mark_task_review_rejected`). Per-stage caps are nullable columns on `task_stage_states` (`max_work_attempts`, `max_review_rounds`); null inherits the registry defaults (`default_max_work_attempts`, `default_max_review_rounds`) at evaluation time.
- **No new agents — but placeholder shims are in scope.** Agents for `expansion-qa`, `qa-reviewer`, `holistic-reviewer`, `merge-orchestrator` already exist as bundled YAMLs and only need rewiring against new stage names. Four discovery-stage agents have no surviving YAML or follow-up task after the #12725 cascade-delete (stage → agent slug mapping: `ideation → analyst`, `research → researcher`, `architecture → architect`, `prd → product-manager`), and `pr` is owned by #13552 (already open). This epic ships **disabled placeholder YAMLs** for the four missing discovery agents (clearly marked as such) and creates a parent epic plus four tracking tasks for the real implementation work. Real agent behavior remains out of scope. **No `test_arch` reviewer agent exists or is planned in this epic** — `test_arch.review_policy=none` is the deliberate contract; adding review later is the documented two-step extension path (registry edit + migration decision).
- **Single project.** No cross-project / multi-tenant kanban work.
- **`escalated` is preserved** as the human-in-the-loop flag — promoted from a `status` value to first-class `is_escalated` column. Every other active `status` value is subsumed by per-stage 5-state.
- **Readiness/blocking semantics stay equivalent.** `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates`, and task `state.is_blocked` must return the same results as the old model for equivalent fixtures after cutover. `review_approved` rows do NOT satisfy upstream-completion checks (only `done` does); the dispatcher's tail rule advances them to `done` once review is satisfied.
- **Schema baseline before this epic = 232** (`BASELINE_VERSION = 220` at `src/gobby/storage/migrations.py:65`, plus the 12 in-tree migrations 221–232 already populated with unrelated work). New migrations for this epic begin at **233**. The existing in-worktree migration 233 (commit `52bf01b65` on `agent/13482-lifecycle-status-enum-alignment`) was authored against the discarded tri-state contract; it is amended in place by Phase 1.1 — the migration version stays 233 because it has not merged.
- **No explicit test tasks anywhere in this plan.** TDD sandwiches are auto-inserted by `/gobby expand` for every `category: code` and `category: config` task.

## P1 Registry + Manifest Schema

`kind: framing`

**Goal**: Land the three new tables, the bundled stages YAML, the artifact-column extensions, and the four discovery-agent placeholder YAMLs. After Phase 1, the database can store a stage manifest, the registry seeds itself on startup, and the registry's `default_agent` slot resolves to a real (if disabled) bundled agent for every stage.

### 1.1 Schema migration + bundled stages.yaml: registry, defaults, manifest, and PR/merge artifact columns [category: code]

`kind: deliverable`

Target: `src/gobby/storage/migrations.py`, `src/gobby/install/shared/registry/stages.yaml` (new — authored here so the migration's inline seed has its source on disk)

**Migration 233 is amended in place.** The existing in-worktree migration 233 (commit `52bf01b65` on `agent/13482-lifecycle-status-enum-alignment`) was authored against the discarded tri-state contract. Because that commit has not merged, this deliverable amends migration 233 in place — same version number, new schema. The amendment adds three new tables and three new TEXT columns to `task_artifacts`, plus one new column on `tasks`, AND seeds the 11 canonical `task_stages_registry` rows plus the six `task_type_default_stages` bundles inline within the same transaction. Use `db.transaction()` around all schema changes and the inline seed; follow the existing `_add_task_artifact_evidence_columns` pattern (`src/gobby/storage/migrations.py:111-159`) for the artifact-column additions.

**Bundled `stages.yaml` lands in this deliverable (load-bearing for sequencing).** This deliverable owns BOTH the migration code AND the bundled YAML file the migration reads. An expanded worker receiving this leaf authors `src/gobby/install/shared/registry/stages.yaml` and `src/gobby/storage/migrations.py` together; the migration cannot land without the YAML, and the YAML has no other consumer until the loader in §1.2. §1.2 retains ownership of `StageRegistryLoader` (the loader/hash-drift detector) and the daemon startup wiring; §1.2 does NOT (re-)author the YAML — it points at the file landed here.

**The §1.1 "Bundled `stages.yaml` body" block below is the SOLE authoritative source for the on-disk YAML body.** The implementing agent for §1.1 transcribes the YAML verbatim from that §1.1 block — and only that block — into `src/gobby/install/shared/registry/stages.yaml` as part of this deliverable. The parser-shape section that follows (later in this plan) carries no full 11-stage body and is not a YAML body source. Acceptance 1.1.6a includes a plan-prose regression grep that fails if the §1.1 narrative (the prose between this section's heading and its `**Acceptance:**` line) names any other section as the YAML body owner; the grep's scan boundary deliberately excludes the `**Acceptance:**` block so the listed forbidden-pattern strings inside that block do not self-match.

**Inline registry + default-stages seed (load-bearing for migration ordering):** Migration 233 reads the bundled `stages.yaml` (authored in this same deliverable per the paragraph above) by resolving the file path through `gobby.paths.get_install_dir() / "shared" / "registry" / "stages.yaml"` — the canonical install-directory resolver at `src/gobby/paths.py:39-66` that handles both development mode (returns `src/gobby/install/`) and installed-package mode (returns `<site-packages>/gobby/install/`). The migration MUST NOT use a repo-root-relative `Path("src/gobby/install/...")` literal nor a `Path(__file__).parent.parent / 'install/...'` literal — both are CWD- or layout-dependent and break under installed-daemon, non-root startup, or a packaged distribution. `get_install_dir()` is the only acceptable path source in the migration code.

After reading the YAML bytes, migration 233 computes `bundled_hash = hashlib.sha256(yaml_bytes).hexdigest()` ONCE and inserts all 11 registry rows + the six `task_type_default_stages` bundles in the SAME transaction as the schema creation. Every seeded `task_stages_registry` row carries that same `bundled_hash` value in the `bundled_hash` column (the column was declared nullable in the §1.1 schema for forward compatibility, but migration 233 MUST persist a non-NULL value on every row it seeds). This is load-bearing for §1.2's hash-drift detector: on a fresh DB, the loader's first run reads the on-disk YAML, recomputes the hash, compares it to the stored `bundled_hash` rows, observes a match, and is a no-op. Without this guarantee, the loader would observe `bundled_hash IS NULL` on every seeded row and treat the first sync as a drift event.

This ensures the FK targets exist before migration 234 (Phase 2.2 backfill) writes any `task_type_default_stages` lookups or `task_stage_states.stage_name` references. The startup `StageRegistryLoader.sync()` (Phase 1.2) becomes a hash-drift detector for subsequent edits to the bundled YAML — it is NOT the only seed path. The six `task_type_default_stages` bundles seeded here are the same six bundles documented in §2.2 (`epic`, `feature`, `bug`, `refactor`, `chore`, `task` — five distinct manifests; `chore` and `task` share the leaves-only manifest). On a fresh DB, migration 233 writes registry + defaults in one transaction; migration 234 then reads them when backfilling `task_stage_states` from `(lifecycle, status, labels)` for existing tasks.

**On `category`** (called out because the field can read like decoration): the five values `discovery | design | verification | implementation | delivery` come from the strategy plan and have one functional consumer in this epic — the kanban category filter wired in Phase 6.1 (6.1.7). The dispatcher does NOT read `category`; rule routing is purely by `stage_name` and registry `position_hint`. If Phase 6.1's filter is later removed, this column should be dropped in the same change. Do not add other consumers without revisiting that decision.

Tables to create (each guarded by `IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS task_stages_registry (
    name TEXT PRIMARY KEY,
    display_label TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('discovery','design','verification','implementation','delivery')),  -- drives kanban category filter (Phase 6.1 6.1.7); not used by the dispatcher

    default_agent TEXT,
    reviewer_agent TEXT,                          -- nullable; populated only when review_policy != 'none'
    review_policy TEXT NOT NULL DEFAULT 'none'
        CHECK (review_policy IN ('none','required','optional')),
    position_hint INTEGER NOT NULL,
    requires_human INTEGER NOT NULL DEFAULT 0,
    is_terminal INTEGER NOT NULL DEFAULT 0,
    default_max_work_attempts INTEGER NOT NULL DEFAULT 3,
    default_max_review_rounds INTEGER NOT NULL DEFAULT 5,
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
    state TEXT NOT NULL DEFAULT 'ready'
        CHECK (state IN ('ready','in_progress','needs_review','review_approved','done')),  -- global 5-value enum on every row regardless of policy; legality enforced per-row by review_policy
    review_policy TEXT NOT NULL DEFAULT 'none'
        CHECK (review_policy IN ('none','required','optional')),  -- mirrored from registry at manifest-init; later registry edits do NOT retroactively change in-flight rows
    reviewer_agent TEXT,                          -- mirrored from registry at manifest-init; nullable
    entered_at TEXT,
    entered_by_session_id TEXT,
    completed_at TEXT,
    completed_by_session_id TEXT,
    completed_commit_sha TEXT,
    work_attempt_count INTEGER NOT NULL DEFAULT 0,    -- incremented on start_stage and fail-loop reentry
    review_round_count INTEGER NOT NULL DEFAULT 0,    -- incremented on mark_task_review_rejected
    max_work_attempts INTEGER,                        -- nullable; null inherits registry default at evaluation time
    max_review_rounds INTEGER,                        -- nullable; null inherits registry default at evaluation time
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

`review_policy` and `reviewer_agent` are **mirrored** from the registry onto each `task_stage_states` row at manifest-init time. Mirroring is load-bearing: a later registry edit (e.g., flipping `research.review_policy: none → required`) does NOT retroactively change the legality of transitions on already-created rows. The new policy applies only to manifests created after the registry edit, unless an explicit migration backfills existing rows. The CHECK constraint on `state` is the global 5-value enum applied uniformly to every row; per-row legality is enforced in `StageStatesManager` by branching on `review_policy` (Phase 2.1).

`artifact_refs` is a JSON-encoded object (`json.dumps`) of pointers into `task_artifacts` (e.g. `{"plan_file": "plan_file_path", "expansion_run": "expansion_run_id"}`). The unique `(task_id, position)` index enforces the position-uniqueness invariant per task. The partial index `idx_task_stage_states_open` accelerates the "leftmost non-done" current-stage projection.

Columns to add to `task_artifacts` (mirror `_add_task_artifact_evidence_columns` rebuild pattern: rename old → create new with allowlisted columns → INSERT SELECT → drop old):

- `pr_review_report TEXT`
- `structured_pr_verdict TEXT` (JSON-encoded)
- `merge_campaign_report TEXT`

**Authoritative legacy-cap inventory** (every cap column currently on `task_artifacts` per `src/gobby/storage/baseline_schema.sql:385-389`, with the per-stage row destination):

| Legacy `task_artifacts` column | Per-stage destination | Stage axis | Notes |
|---|---|---|---|
| `max_expansion_attempts` | `expansion.max_work_attempts` | work | Expansion is a `policy=required` stage; work attempts capped per-row |
| `max_qa_rounds` | `development.max_review_rounds` | review | `development` subsumes the legacy `code_review_qa` review rounds |
| `max_merge_attempts` | `merge.max_work_attempts` | work | `merge.review_policy=none`; only the work-attempt cap is meaningful |
| `max_holistic_rounds` | `holistic_qa.max_review_rounds` | review | Holistic review uses the review-round counter (the agent's internal `submit_for_review`/`approve_review` cycle counts as a review round) |
| `max_review_rounds` | `pr.max_review_rounds` | review | Generic legacy "review rounds" column was always the PR review cap; renamed to per-stage column on `pr` |

Stages with no legacy cap column (`planning`, `test_arch`, `ideation`, `research`, `architecture`, `prd`) inherit the registry defaults (`default_max_work_attempts=3`, `default_max_review_rounds=5`) on backfill; `task_stage_states.max_work_attempts` and `.max_review_rounds` are left NULL for those rows and resolve via `state_row.max_<X> ?? registry_row.default_max_<X>` at evaluation time. Per-task overrides land via `gobby build <ref> --stage <name>:max_review_rounds=N` (or `--stage <name>:max_work_attempts=N`).

`max_planning_rounds` is **not** a real `task_artifacts` column — earlier drafts of this plan referenced it speculatively; remove from any inventory still mentioning it. The planning stage's review-round cap inherits the registry default unless overridden via the build flag.

These five legacy columns are migrated into the new per-stage cap columns during the Phase 2.2 backfill (acceptance 2.2.4) and dropped in Phase 5.3 cleanup (acceptance 5.3.11 — the dedicated cap-column drop acceptance; the legacy `task_artifacts.max_*` columns are NOT covered by 5.3.7, which is the post-cutover review-tool call-site audit). After migration 234 commits, runtime code paths read effective caps exclusively via `state_row.max_<X> ?? registry_row.default_max_<X>` — no runtime reader touches `task_artifacts` cap columns. The columns survive on the schema until Phase 5.3's migration 236 drops them so the historical migration helper can replay against pre-cutover databases.

Update `_default_task_artifact_column` (`src/gobby/storage/migrations.py:177-182`) to include defaults for the three new TEXT columns (`NULL`). Update `_task_artifacts_create_sql` (`src/gobby/storage/migrations.py:75-108`) to include the three new TEXT columns so fresh installs match.

Column to add to `tasks` (separate `ALTER TABLE tasks ADD COLUMN`; SQLite tolerates this as a no-rebuild change):

- `is_escalated INTEGER NOT NULL DEFAULT 0` — first-class human-in-the-loop flag promoted from `status='escalated'`. Created here at default 0; backfilled from `escalated_at IS NOT NULL` in migration 234 (Phase 2.2). Placement on `tasks` (not `task_artifacts`) is load-bearing: escalation is task-level state read on every list, while `task_artifacts` is sparse evidence. Phase 5.2 wires `Task` dataclass and reader call sites; no migration is needed in 5.2.

**Bundled `stages.yaml` body** (this exact file is written to disk by this deliverable; migration 233's inline seed reads it; §1.2's `StageRegistryLoader` parses it on startup for hash-drift detection):

```yaml
# src/gobby/install/shared/registry/stages.yaml
version: 1
stages:
  - name: ideation
    display_label: Ideation
    description: Early problem framing; capture motivating questions and constraints.
    category: discovery
    default_agent: analyst                    # placeholder shim — Phase 1.3
    review_policy: none
    position_hint: 10
    requires_human: false
    is_terminal: false
  - name: research
    display_label: Research
    description: Targeted investigation; produce findings consumable by architecture/PRD.
    category: discovery
    default_agent: researcher                 # placeholder shim — Phase 1.3
    review_policy: none
    position_hint: 20
    requires_human: false
    is_terminal: false
  - name: architecture
    display_label: Architecture
    description: Cross-cutting design decisions and component shape.
    category: design
    default_agent: architect                  # placeholder shim — Phase 1.3
    review_policy: none
    position_hint: 30
    requires_human: false
    is_terminal: false
  - name: prd
    display_label: PRD
    description: Productized requirements; bridges discovery and planning.
    category: design
    default_agent: product-manager            # placeholder shim — Phase 1.3
    review_policy: none
    position_hint: 40
    requires_human: false
    is_terminal: false
  - name: planning
    display_label: Planning
    description: Implementation plan authoring (interactive or autonomous).
    category: design
    default_agent: planner
    reviewer_agent: plan-adversary
    review_policy: required
    position_hint: 50
    requires_human: false
    is_terminal: false
  - name: test_arch
    display_label: Test Architecture
    description: Test scaffolding and contract test design before expansion. No reviewer agent exists; one-shot work stage.
    category: verification
    default_agent: test-architect
    review_policy: none                       # deliberate: no test-architecture reviewer agent in this epic
    position_hint: 60
    requires_human: false
    is_terminal: false
  - name: expansion
    display_label: Expansion
    description: Decompose plan into TDD-wrapped leaf tasks.
    category: implementation
    # default_agent left blank — expansion runs as a pipeline action, not an agent spawn
    reviewer_agent: expansion-qa
    review_policy: required
    position_hint: 70
    requires_human: false
    is_terminal: false
  - name: development
    display_label: Development
    description: Leaf implementation work; drives TDD sandwiches.
    category: implementation
    default_agent: backend-developer          # primary fallback; build-time may override per-task
    reviewer_agent: qa-reviewer
    review_policy: required
    position_hint: 80
    requires_human: false
    is_terminal: false
  - name: holistic_qa
    display_label: Holistic QA
    description: Whole-epic review after every leaf is parked. Epic-level only — leaf manifests omit this stage. Aggregate review; agent produces review_approved internally.
    category: verification
    default_agent: holistic-reviewer
    reviewer_agent: holistic-reviewer         # same agent does work and review (aggregate)
    review_policy: required
    position_hint: 90
    requires_human: false
    is_terminal: false
  - name: pr
    display_label: Pull Request
    description: Open/update PR, capture verdict, gate on external review.
    category: delivery
    # default_agent left blank — owned by #13552 (PR/merge skill epic)
    # reviewer_agent left blank — external (PR-Agent or human) review; pr_no_agent escalation until #13552 lands
    review_policy: required
    position_hint: 100
    requires_human: false
    is_terminal: false
  - name: merge
    display_label: Merge
    description: Land approved PR; resolve conflicts; close terminal task.
    category: delivery
    default_agent: merge-orchestrator
    review_policy: none
    position_hint: 110
    requires_human: false
    is_terminal: true
```

Field semantics: `default_agent` is populated for every stage with a real or placeholder bundled agent except `expansion` (pipeline action, not an agent spawn) and `pr` (owned by #13552). The four discovery stages point at placeholder shims landed in §1.3. `reviewer_agent` is populated only when `review_policy != none`; `pr.reviewer_agent` is left blank pending #13552 — Phase 4.1 wires the `pr_no_agent` escalation reason. `holistic_qa` carries the same agent in both `default_agent` and `reviewer_agent` slots because aggregate review is internal to the agent run. The dropped stages `adversarial_review`, `expansion_qa`, `code_review_qa` are absent — review is now a state on `planning`, `expansion`, and `development` respectively, with `reviewer_agent` pointing at the same reviewer agents that previously owned the dedicated review stages. `default_max_work_attempts` and `default_max_review_rounds` are NOT carried per-stage in this YAML (a future change can add them); the registry table's column defaults (`3` and `5` respectively, per the §1.1 schema) apply uniformly to every stage — per-task overrides land via `gobby build <ref> --stage <name>:max_work_attempts=N` (and `:max_review_rounds=N`).

**Inline `task_type_default_stages` seed body (load-bearing for §1.1 self-containment).** Migration 233 seeds `task_type_default_stages` with **30 rows** total — one row per `(task_type, stage_name, position)` per the table schema declared above (`PRIMARY KEY (task_type, stage_name)`). The six task types resolve to five distinct manifests; `chore` and `task` share the `[development, pr, merge]` leaves-only manifest but each gets its own three rows in the table because `task_type` is part of the primary key. `position` is 0-indexed and dense within each `task_type`. The exact 30 rows the migration MUST insert, expanded from the §2.2 task-type → manifest table reproduced here for §1.1 self-containment:

| task_type | stage_name | position |
|---|---|---|
| `epic` | `ideation` | 0 |
| `epic` | `research` | 1 |
| `epic` | `architecture` | 2 |
| `epic` | `prd` | 3 |
| `epic` | `planning` | 4 |
| `epic` | `test_arch` | 5 |
| `epic` | `expansion` | 6 |
| `epic` | `development` | 7 |
| `epic` | `holistic_qa` | 8 |
| `epic` | `pr` | 9 |
| `epic` | `merge` | 10 |
| `feature` | `planning` | 0 |
| `feature` | `test_arch` | 1 |
| `feature` | `expansion` | 2 |
| `feature` | `development` | 3 |
| `feature` | `pr` | 4 |
| `feature` | `merge` | 5 |
| `bug` | `development` | 0 |
| `bug` | `pr` | 1 |
| `bug` | `merge` | 2 |
| `refactor` | `planning` | 0 |
| `refactor` | `development` | 1 |
| `refactor` | `pr` | 2 |
| `refactor` | `merge` | 3 |
| `chore` | `development` | 0 |
| `chore` | `pr` | 1 |
| `chore` | `merge` | 2 |
| `task` | `development` | 0 |
| `task` | `pr` | 1 |
| `task` | `merge` | 2 |

Per-task-type row counts: `epic` 11, `feature` 6, `bug` 3, `refactor` 4, `chore` 3, `task` 3 — total **30 rows**. None of the seeded manifests reference the dropped review stages (`adversarial_review`, `expansion_qa`, `code_review_qa`); review is now state on the work stage, not a separate stage. `holistic_qa` appears only in `epic` (epic-level only — leaf manifests omit it; the dispatcher's `all_leaves_holistic_rule` advances the epic's `holistic_qa.ready → in_progress` once every direct child is parked). The §2.2 task-type → manifest table is the human-readable mirror of this seed; §1.1 owns the seed contract authoritatively, §2.2 references it for backfill validation only.

**Acceptance:**

- 1.1.1 — Migration version 233 exists in `MIGRATIONS` (amended in place from the discarded tri-state version on `agent/13482-lifecycle-status-enum-alignment` commit `52bf01b65`). file: `src/gobby/storage/migrations.py`. symbol: `gobby.storage.migrations.MIGRATIONS`.
- 1.1.2 — Three new tables created with declared schema. `task_stages_registry` carries `review_policy`, `reviewer_agent`, `default_max_work_attempts`, `default_max_review_rounds`. `task_stage_states.state` CHECK accepts the 5-value enum `('ready','in_progress','needs_review','review_approved','done')`. `task_stage_states` carries `review_policy`, `reviewer_agent`, `work_attempt_count`, `review_round_count`, `max_work_attempts`, `max_review_rounds`; the dropped tri-state `attempt_count` column is **not** present. Indexes and partial index on open rows verified. test: `tests/storage/test_migration_233.py::test_creates_registry_tables`, `tests/storage/test_migration_233.py::test_state_check_accepts_five_values`, `tests/storage/test_migration_233.py::test_review_policy_columns_present`.
- 1.1.3 — `task_artifacts` gains three new TEXT columns (`pr_review_report`, `structured_pr_verdict`, `merge_campaign_report`) with `NULL` defaults; rebuild path preserves existing rows. test: `tests/storage/test_migration_233.py::test_artifact_columns_added`.
- 1.1.4 — Fresh-install `_task_artifacts_create_sql` includes the new TEXT columns so a blank DB skips the rebuild path. behavior: "fresh install schema matches migration end state" verified in `tests/storage/test_migration_233.py::test_fresh_install_matches`.
- 1.1.5 — `tasks` table gains `is_escalated INTEGER NOT NULL DEFAULT 0`; existing rows default to 0 with backfill deferred to migration 234 (Phase 2.2). test: `tests/storage/test_migration_233.py::test_tasks_is_escalated_added`.
- 1.1.6 — Migration 233 seeds all 11 `task_stages_registry` rows from the bundled `stages.yaml` inline in the same transaction as the schema creation. The migration resolves the YAML file path via `gobby.paths.get_install_dir() / "shared" / "registry" / "stages.yaml"` (the canonical install-directory resolver at `src/gobby/paths.py:39-66`); it MUST NOT use a repo-root-relative `Path("src/gobby/install/...")` literal nor a `Path(__file__).parent.parent / 'install/...'` literal. After reading YAML bytes, the migration computes `bundled_hash = hashlib.sha256(yaml_bytes).hexdigest()` ONCE and persists that exact value into the `bundled_hash` column on every seeded `task_stages_registry` row (no row left at NULL). On a fresh DB the table contains exactly 11 rows after migration 233 runs and before migration 234 starts; every row's `bundled_hash` equals the SHA-256 of the on-disk file. Each row's `review_policy` matches the strategy plan's per-stage policy table (`planning|expansion|development|holistic_qa|pr` are `required`; the other 6 are `none`). test: `tests/storage/test_migration_233.py::test_registry_seeded_inline`, `tests/storage/test_migration_233.py::test_seeded_review_policy_matches_strategy_table`, `tests/storage/test_migration_233.py::test_seeded_bundled_hash_matches_on_disk_yaml_sha256`, `tests/storage/test_migration_233.py::test_seeded_bundled_hash_is_uniform_across_all_11_rows`, `tests/storage/test_migration_233.py::test_seeded_bundled_hash_is_not_null_on_any_row`, `tests/storage/test_migration_233.py::test_migration_resolves_yaml_via_get_install_dir`, `tests/storage/test_migration_233.py::test_migration_yaml_path_independent_of_cwd` (changes CWD to a tmpdir before running migration; asserts the seed succeeds and reads the same bytes).
- 1.1.6a — `src/gobby/install/shared/registry/stages.yaml` exists on disk as part of this deliverable, with the EXACT body shown in the "Bundled `stages.yaml` body" block above. All 11 stages (`ideation, research, architecture, prd, planning, test_arch, expansion, development, holistic_qa, pr, merge`) are declared with the field set documented in that block (`name`, `display_label`, `description`, `category`, `position_hint`, `review_policy`, `requires_human`, `is_terminal`, plus `default_agent` and `reviewer_agent` where applicable). The dropped stages `adversarial_review`, `expansion_qa`, `code_review_qa` are **not** declared. The migration's inline seed (1.1.6) reads this exact file; the parser-shape section later in this plan parses the same file on startup for hash-drift detection (loader-only — does not author the file). The byte-equality invariant against the plan body is owned and enforced by THIS acceptance (1.1.6a), NOT by the runtime loader — the loader has no access to plan-file content at runtime. **Plan-prose regression**: a plan-prose grep test scopes its scan to the §1.1 narrative window — the byte range from the §1.1 heading line to the line containing `**Acceptance:**` (exclusive) — and fails if that window matches any of three forbidden-pattern regexes carried as raw bytes in the test source: `r"copy[^\n]{0,40}§\s*1\.2"`, `r"documented[^\n]{0,40}§\s*1\.2"`, and `r"§\s*1\.2[^\n]{0,80}YAML body"`. The acceptance block is excluded from the scan window deliberately so the literal forbidden-pattern strings inside this acceptance item do not self-match. The test ALSO asserts a positive condition: the scanned window names only the §1.1 inline "Bundled `stages.yaml` body" block as the on-disk YAML source, and contains no other "YAML body" or "authoritative" reference pointing at any other section. file: `src/gobby/install/shared/registry/stages.yaml`. test: `tests/storage/test_migration_233.py::test_bundled_stages_yaml_present_with_11_stages`, `tests/storage/test_migration_233.py::test_yaml_omits_dropped_review_stages`, `tests/storage/test_migration_233.py::test_yaml_body_matches_plan_specified_content` (compares the on-disk file to the §1.1 inline body byte-for-byte modulo trailing newlines), `tests/test_plan_yaml_body_singleton.py::test_section_1_1_narrative_window_has_no_cross_section_yaml_authority_phrases` (negative — runs the three forbidden-pattern regexes against the narrative window only, asserts zero matches), `tests/test_plan_yaml_body_singleton.py::test_section_1_1_narrative_window_names_only_inline_body_as_yaml_source` (positive — asserts the narrative window contains exactly one "SOLE authoritative source" claim and that claim points at the §1.1 inline body).
- 1.1.7 — Cross-cutting aggregate invariants over the §1.1 inline `task_type_default_stages` seed body table (per-row claims live in 1.1.7a–1.1.7ad below). Migration 233 seeds `task_type_default_stages` inline in the same transaction as the schema creation. Per-task-type row counts: `epic` 11, `feature` 6, `bug` 3, `refactor` 4, `chore` 3, `task` 3 (total 30). `position` is 0-indexed and dense within each `task_type` (no gaps, starting at 0). None of the seeded manifests reference the dropped review stages (`adversarial_review`, `expansion_qa`, `code_review_qa`); `holistic_qa` appears only in `epic`'s manifest. Migration 234 (Phase 2.2 backfill) finds the rows already present when resolving per-task manifests. test: `tests/storage/test_migration_233.py::test_default_stages_seeded_inline_30_rows`, `tests/storage/test_migration_233.py::test_default_stages_per_task_type_row_counts` (asserts 11/6/3/4/3/3 across the six task_type values), `tests/storage/test_migration_233.py::test_default_stages_positions_are_zero_indexed_and_dense` (parameterized over the six task_type values; asserts each manifest's positions form `range(N)` exactly), `tests/storage/test_migration_233.py::test_default_stages_exact_rows_match_section_1_1_seed_table` (asserts the 30 inserted rows equal the §1.1 inline seed table set-equally as `(task_type, stage_name, position)` tuples), `tests/storage/test_migration_233.py::test_default_stages_omit_dropped_review_stages` (asserts no row references `adversarial_review`, `expansion_qa`, or `code_review_qa`), `tests/storage/test_migration_233.py::test_holistic_qa_only_in_epic_manifest`.
- 1.1.7a — Migration 233 inserts row (`epic`, `ideation`, 0) into `task_type_default_stages`. behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_ideation_0`.
- 1.1.7b — Migration 233 inserts row (`epic`, `research`, 1). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_research_1`.
- 1.1.7c — Migration 233 inserts row (`epic`, `architecture`, 2). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_architecture_2`.
- 1.1.7d — Migration 233 inserts row (`epic`, `prd`, 3). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_prd_3`.
- 1.1.7e — Migration 233 inserts row (`epic`, `planning`, 4). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_planning_4`.
- 1.1.7f — Migration 233 inserts row (`epic`, `test_arch`, 5). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_test_arch_5`.
- 1.1.7g — Migration 233 inserts row (`epic`, `expansion`, 6). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_expansion_6`.
- 1.1.7h — Migration 233 inserts row (`epic`, `development`, 7). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_development_7`.
- 1.1.7i — Migration 233 inserts row (`epic`, `holistic_qa`, 8). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_holistic_qa_8`.
- 1.1.7j — Migration 233 inserts row (`epic`, `pr`, 9). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_pr_9`.
- 1.1.7k — Migration 233 inserts row (`epic`, `merge`, 10). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_epic_merge_10`.
- 1.1.7l — Migration 233 inserts row (`feature`, `planning`, 0). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_feature_planning_0`.
- 1.1.7m — Migration 233 inserts row (`feature`, `test_arch`, 1). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_feature_test_arch_1`.
- 1.1.7n — Migration 233 inserts row (`feature`, `expansion`, 2). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_feature_expansion_2`.
- 1.1.7o — Migration 233 inserts row (`feature`, `development`, 3). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_feature_development_3`.
- 1.1.7p — Migration 233 inserts row (`feature`, `pr`, 4). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_feature_pr_4`.
- 1.1.7q — Migration 233 inserts row (`feature`, `merge`, 5). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_feature_merge_5`.
- 1.1.7r — Migration 233 inserts row (`bug`, `development`, 0). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_bug_development_0`.
- 1.1.7s — Migration 233 inserts row (`bug`, `pr`, 1). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_bug_pr_1`.
- 1.1.7t — Migration 233 inserts row (`bug`, `merge`, 2). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_bug_merge_2`.
- 1.1.7u — Migration 233 inserts row (`refactor`, `planning`, 0). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_refactor_planning_0`.
- 1.1.7v — Migration 233 inserts row (`refactor`, `development`, 1). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_refactor_development_1`.
- 1.1.7w — Migration 233 inserts row (`refactor`, `pr`, 2). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_refactor_pr_2`.
- 1.1.7x — Migration 233 inserts row (`refactor`, `merge`, 3). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_refactor_merge_3`.
- 1.1.7y — Migration 233 inserts row (`chore`, `development`, 0). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_chore_development_0`.
- 1.1.7z — Migration 233 inserts row (`chore`, `pr`, 1). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_chore_pr_1`.
- 1.1.7aa — Migration 233 inserts row (`chore`, `merge`, 2). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_chore_merge_2`.
- 1.1.7ab — Migration 233 inserts row (`task`, `development`, 0). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_task_development_0`.
- 1.1.7ac — Migration 233 inserts row (`task`, `pr`, 1). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_task_pr_1`.
- 1.1.7ad — Migration 233 inserts row (`task`, `merge`, 2). behavior: row present after migration commits. test: `tests/storage/test_migration_233.py::test_default_stages_row_task_merge_2`.
- 1.1.8 — On a fresh DB, the migration runner reaches version 234 with the registry table populated; FK references from `task_type_default_stages.stage_name` and `task_stage_states.stage_name` resolve cleanly because migration 233 seeded the parent rows in the same transaction. test: `tests/storage/test_migration_233.py::test_fresh_db_fk_resolution_into_234`.
- 1.1.9 — **Position-base regression (plan-prose level).** A plan-prose grep test scans the entire `.gobby/plans/task-13482-stage-manifest-cutover.md` file for forbidden 1-based position phrasing. Forbidden-pattern regexes carried as raw bytes in the test source: `r"\bdense\s*\(?1\.\.N\)?"` (the legacy `dense (1..N)` shorthand) and `r"\bposition\s*==\s*1\b"` (the legacy first-row predicate). The scan window covers the entire plan file. Both forbidden-pattern strings appear inline in THIS acceptance item only (1.1.9) carried as raw bytes; the test treats matches inside its own acceptance scope as the carrier strings and ignores them by excluding the byte range from the start of the `## §1.1.9` heading anchor (or, equivalently, from the line containing the literal acceptance-id token `1.1.9 —`) through the end of that acceptance item's bullet — but matches anywhere ELSE in the file fail the test. The test ALSO asserts a positive condition: the file contains the canonical 0-indexed phrasing `range(N)` and `position == 0` somewhere in the §1.1 / §2.1 / §2.3 / §3.1 narrative windows. test: `tests/test_plan_position_base_singleton.py::test_no_one_based_position_phrasing_outside_acceptance_1_1_9`, `tests/test_plan_position_base_singleton.py::test_zero_indexed_phrasing_present_in_canonical_sections`.

### 1.2 Sync loader for the bundled stages.yaml [category: config] (depends: 1.1)

`kind: deliverable`

Target: `src/gobby/storage/tasks/_stage_registry_loader.py` (new), `src/gobby/runner_init.py` (wiring), `src/gobby/install/shared/registry/stages.yaml` (read-only input — authored by §1.1, parsed at startup by this deliverable's loader; NOT modified here)

Implement the `StageRegistryLoader` that reads the bundled YAML landed by §1.1 (`src/gobby/install/shared/registry/stages.yaml`) and upserts on hash drift. Mirror the bundled-template pattern from `src/gobby/install/shared/{rules,workflows,agents}` — the file is hashed at startup, drift triggers an upsert, user overrides are detected by hash comparison.

**This deliverable does NOT author or modify `stages.yaml`.** The authoritative file body lives in §1.1's "Bundled `stages.yaml` body" block; §1.1 acceptance 1.1.6a writes it to disk and §1.1 acceptance 1.1.7 (and 1.1.6's inline seed) reads it. §1.2 ONLY adds the loader code and daemon startup wiring; it parses the file already written by §1.1 and never opens the file in write mode. Implementing agents must NOT reproduce, paraphrase, or partially copy the YAML body in this deliverable — the §1.1 inline body is the single source of truth, and §1.1 acceptance 1.1.6a includes a byte-equality regression test that fails if the on-disk file drifts from the §1.1 inline body.

**Sync loader role:** Migration 233 (§1.1) seeds all 11 registry rows + six `task_type_default_stages` bundles INLINE within the schema transaction by reading the bundled YAML file directly. The startup `StageRegistryLoader.sync()` is therefore a hash-drift detector only — it upserts when the bundled YAML hash changes between releases. It is NOT the only seed path; on a fresh DB the registry is populated by migration 233 before the daemon ever calls the loader. This split keeps migration ordering atomic (FK targets exist before any backfill that references them) while still letting bundled-YAML edits propagate without a new migration.

**Parser shape** (used by `StageRegistryLoader` to validate the file written by §1.1; non-authoritative — the file body is in §1.1 only):

```yaml
# Top-level shape only; see §1.1 for the actual 11-stage content.
version: 1
stages:
  - name:           <str>            # required, unique per file
    display_label:  <str>            # required
    description:    <str>            # required
    category:       discovery | design | verification | implementation | delivery
    default_agent:  <str | null>     # optional; null for stages with no agent (e.g. expansion, pr)
    reviewer_agent: <str | null>     # optional; required when review_policy != none, except documented blanks (pr)
    review_policy:  none | required | optional
    position_hint:  <int>            # ascending across the 11 stages
    requires_human: <bool>           # default false
    is_terminal:    <bool>           # default false; only `merge` sets true
```

The runtime loader's job is **schema validation, dropped-name rejection, and hash-drift detection** — NOT byte equality against the plan file. Specifically the parser: (a) rejects malformed YAML and missing required fields with typed errors; (b) rejects rows naming the dropped stages `adversarial_review`, `expansion_qa`, `code_review_qa`; (c) computes the bundled-file hash and compares it to the stored `bundled_hash` to drive upserts on drift; (d) enforces field-set semantics (e.g., `review_policy != none` requires `reviewer_agent` outside the documented blank-reviewer allowlist). Byte equality between the on-disk file and the §1.1 plan body is a **plan-time** invariant owned by §1.1 acceptance 1.1.6a's test, which compares the on-disk file to the §1.1 inline body modulo trailing newlines; the runtime loader has no access to plan-file content and does not perform this comparison. There is a regression grep in §1.2's acceptance 1.2.1 ensuring this deliverable carries no full YAML body of its own (the only YAML body in the plan lives in §1.1).

`default_agent` is populated for every stage with a real or placeholder bundled agent except `expansion` (pipeline action, not an agent spawn) and `pr` (owned by #13552). The four discovery stages point at placeholder shims landed in 1.3. `reviewer_agent` is populated only when `review_policy != none`; `pr.reviewer_agent` is left blank pending #13552 — Phase 4.1 wires the `pr_no_agent` escalation reason. `holistic_qa` carries the same agent in both `default_agent` and `reviewer_agent` slots because aggregate review is internal to the agent run (the agent itself produces the `in_progress → review_approved` transition). The dropped stages `adversarial_review`, `expansion_qa`, `code_review_qa` are absent — review is now a state on `planning`, `expansion`, and `development` respectively, with `reviewer_agent` pointing at the same reviewer agents that previously owned the dedicated review stages.

Sync loader (`src/gobby/storage/tasks/_stage_registry_loader.py`):

```python
from gobby.paths import get_install_dir


class StageRegistryLoader:
    """Sync bundled stages.yaml into task_stages_registry on startup.

    Mirrors the workflow loader's hash-drift detection. Bundled rows are
    upserted whenever the file hash changes; rows whose name is missing
    from the bundled YAML are NOT deleted (operator-added stages are
    permitted but not part of the supported contract).

    Path resolution is CWD-independent: the bundled YAML is resolved via
    `get_install_dir() / "shared" / "registry" / "stages.yaml"`, which
    handles both development mode (`src/gobby/install/`) and installed-
    package mode (`<site-packages>/gobby/install/`). The loader MUST NOT
    pin a repo-root-relative literal — that breaks on installed-daemon
    or non-root startup.
    """

    def __init__(self) -> None:
        # Resolve path lazily through get_install_dir so the loader works
        # under both development and installed-package layouts. Path is
        # captured at construction so each sync() call uses a stable
        # value; in tests the install dir can be patched before
        # constructing the loader.
        self._bundled_path: Path = get_install_dir() / "shared" / "registry" / "stages.yaml"

    @property
    def bundled_path(self) -> Path:
        """The resolved on-disk YAML path. Read-only; useful for test diagnostics."""
        return self._bundled_path

    def sync(self, db: DatabaseProtocol) -> StageRegistrySyncResult: ...
    def detect_override(self, db_row: dict, bundled_row: dict) -> bool: ...
```

Wire `StageRegistryLoader().sync(db)` into the daemon startup sequence next to the existing template-sync call `sync_bundled_content_to_db(runner.database)` at `src/gobby/runner_init.py:257-259`. Sync runs after the migration applier so the table exists.

**Hash protocol contract.** On every `sync()` call, the loader: (a) reads the YAML bytes from `self._bundled_path`; (b) computes `current_hash = hashlib.sha256(bytes).hexdigest()`; (c) reads the existing `bundled_hash` value(s) from `task_stages_registry`. If every existing row's `bundled_hash` equals `current_hash`, the sync is a no-op (no UPDATEs, no INSERTs, returns an `unchanged` result). If any row's hash differs, OR any bundled stage is missing from the DB, OR any DB row's `bundled_hash IS NULL`, the loader upserts every bundled row and writes the new hash. Migration 233 (§1.1 acceptance 1.1.6) seeds `bundled_hash` non-NULL for every initial row, so on a fresh DB the loader's first run after migrations observes a clean match and no-ops — this is the contract that ties §1.2.2's no-op-on-fresh-DB acceptance to §1.1.6's hash-seed requirement.

**Acceptance:**

- 1.2.1 — `StageRegistryLoader` parses the bundled `stages.yaml` landed by §1.1 (acceptance 1.1.6a) into a list of `StageRegistryEntry`-shaped records carrying `review_policy` and `reviewer_agent` fields; the loader rejects malformed YAML with a typed error, surfaces missing required fields, and rejects rows with `review_policy != 'none'` that lack a `reviewer_agent` unless the stage is in the documented blank-reviewer allowlist (`pr` pending #13552). It also rejects any of the dropped stage names (`adversarial_review`, `expansion_qa`, `code_review_qa`). The file-existence and 11-stage-completeness invariants are owned by §1.1 acceptance 1.1.6a; this acceptance only verifies the parser. **No-full-YAML-body regression**: a plan-prose grep test asserts that this deliverable's section in the plan file does NOT contain a full `stages: ...` YAML body — the file body lives in §1.1 only. symbol: `gobby.storage.tasks._stage_registry_loader.StageRegistryLoader`. test: `tests/storage/test_stage_registry_loader.py::test_parses_bundled_yaml`, `tests/storage/test_stage_registry_loader.py::test_malformed_yaml_raises`, `tests/storage/test_stage_registry_loader.py::test_required_policy_without_reviewer_outside_allowlist_raises`, `tests/storage/test_stage_registry_loader.py::test_dropped_stage_names_rejected`, `tests/test_plan_yaml_body_singleton.py::test_section_1_2_has_no_full_yaml_body`, `tests/test_plan_yaml_body_singleton.py::test_section_1_1_is_sole_yaml_body_owner`.
- 1.2.2 — `StageRegistryLoader.sync()` is a hash-drift detector that upserts bundled rows when the bundled YAML hash differs from the stored `bundled_hash`. On a fresh DB the registry is already populated by migration 233 with a non-NULL `bundled_hash` matching the on-disk file SHA-256 (per §1.1 acceptance 1.1.6); the loader's first run observes the seeded rows and is a no-op (no UPDATEs, no INSERTs, no hash drift). The loader resolves the YAML file path via `get_install_dir() / "shared" / "registry" / "stages.yaml"` (the canonical install-directory resolver at `src/gobby/paths.py:39-66`); the loader MUST NOT pin a repo-root-relative `Path("src/gobby/install/...")` literal. Path resolution is CWD-independent and works under both development mode (`src/gobby/install/`) and installed-package mode (`<site-packages>/gobby/install/`). symbol: `gobby.storage.tasks._stage_registry_loader.StageRegistryLoader`. test: `tests/storage/test_stage_registry_loader.py::test_sync_no_op_when_hash_matches_seed` (runs migration 233 then sync; asserts zero UPDATEs/INSERTs), `tests/storage/test_stage_registry_loader.py::test_sync_upserts_on_hash_drift` (mutates a bundled row's `bundled_hash` in DB to a stale value; asserts sync upserts), `tests/storage/test_stage_registry_loader.py::test_sync_upserts_when_bundled_hash_is_null` (manually NULLs a row's `bundled_hash`; asserts sync upserts to populate), `tests/storage/test_stage_registry_loader.py::test_loader_resolves_via_get_install_dir` (asserts `loader.bundled_path` equals `get_install_dir() / "shared" / "registry" / "stages.yaml"`), `tests/storage/test_stage_registry_loader.py::test_loader_path_resolution_independent_of_cwd` (changes CWD to a tmpdir before constructing loader; asserts `loader.bundled_path` is unchanged and `sync()` succeeds), `tests/storage/test_stage_registry_loader.py::test_loader_resolves_via_packaged_install_dir_when_dev_dir_absent` (patches `gobby.paths.get_install_dir` to return a packaged-style install dir; asserts sync reads from that path).
- 1.2.3 — Daemon startup wiring invokes the loader after migrations, adjacent to `sync_bundled_content_to_db(runner.database)` at `src/gobby/runner_init.py:257-259`. file: `src/gobby/runner_init.py`. test: `tests/test_startup_seeds_stage_registry.py::test_registry_populated_after_startup`.
- 1.2.4 — Operator-added stages survive bundled-YAML re-sync; bundled stages get re-upserted. behavior: "user-added stage rows persist across sync" verified in `tests/storage/test_stage_registry_loader.py::test_user_added_stage_preserved`.

### 1.3 Placeholder agent YAMLs for discovery stages [category: config]

`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/analyst.yaml`, `src/gobby/install/shared/workflows/agents/researcher.yaml`, `src/gobby/install/shared/workflows/agents/architect.yaml`, `src/gobby/install/shared/workflows/agents/product-manager.yaml` (all new)

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

- 1.3.1 — Four YAML files exist with declared `name`, `enabled: false`, `priority: 1`, and PLACEHOLDER banners. file: `src/gobby/install/shared/workflows/agents/analyst.yaml`, `researcher.yaml`, `architect.yaml`, `product-manager.yaml`.
- 1.3.2 — Each file's `instructions` block tells the agent to escalate with reason `placeholder_agent:<slug>:not_implemented` if accidentally spawned. test: `tests/agents/test_placeholder_agents.py::test_each_placeholder_escalates_on_spawn`.
- 1.3.3 — Bundled-template sync installs the rows with `enabled: false`. test: `tests/agents/test_placeholder_agents.py::test_sync_installs_disabled`.
- 1.3.4 — `task_stages_registry.default_agent` foreign-key resolves for all four discovery stages after Phase 1.2 sync. test: `tests/storage/tasks/test_stage_registry_default_agent_fk.py::test_discovery_stage_default_agents_resolve`.
- 1.3.5 — Dispatcher's missing-agent check treats `enabled: false` as missing and escalates with the stage-specific `<stage>_no_agent` reason rather than spawning the placeholder. The actual escalation is emitted by `disabled_agent_escalation_rule` (Phase 3.1, acceptance 3.1.23), which fires on `current_stage.state == 'ready'` AND `default_agent` missing/disabled. test: `tests/dispatch/test_no_agent_paths.py::test_disabled_placeholder_treated_as_missing`, `tests/dispatch/test_no_agent_paths.py::test_disabled_placeholder_routes_to_disabled_agent_escalation_rule`.

Per-row coverage (one acceptance per data row of the §1.3 Stage|Agent-slug table, per the plan-coverage contract's table-row decomposition rule):

- 1.3.6 — Stage `ideation` → agent slug `analyst`: file `src/gobby/install/shared/workflows/agents/analyst.yaml` exists with `name: analyst`, `enabled: false`, `priority: 1`, PLACEHOLDER banner, `instructions` block telling the agent to escalate with reason `placeholder_agent:analyst:not_implemented` if accidentally spawned; `task_stages_registry.default_agent` for stage `ideation` resolves to this row after sync. file: `src/gobby/install/shared/workflows/agents/analyst.yaml`. test: `tests/agents/test_placeholder_agents.py::test_analyst_placeholder_for_ideation_stage`.
- 1.3.7 — Stage `research` → agent slug `researcher`: file `src/gobby/install/shared/workflows/agents/researcher.yaml` exists with `name: researcher`, `enabled: false`, `priority: 1`, PLACEHOLDER banner, `instructions` block telling the agent to escalate with reason `placeholder_agent:researcher:not_implemented` if accidentally spawned; `task_stages_registry.default_agent` for stage `research` resolves to this row after sync. file: `src/gobby/install/shared/workflows/agents/researcher.yaml`. test: `tests/agents/test_placeholder_agents.py::test_researcher_placeholder_for_research_stage`.
- 1.3.8 — Stage `architecture` → agent slug `architect`: file `src/gobby/install/shared/workflows/agents/architect.yaml` exists with `name: architect`, `enabled: false`, `priority: 1`, PLACEHOLDER banner, `instructions` block telling the agent to escalate with reason `placeholder_agent:architect:not_implemented` if accidentally spawned; `task_stages_registry.default_agent` for stage `architecture` resolves to this row after sync. file: `src/gobby/install/shared/workflows/agents/architect.yaml`. test: `tests/agents/test_placeholder_agents.py::test_architect_placeholder_for_architecture_stage`.
- 1.3.9 — Stage `prd` → agent slug `product-manager`: file `src/gobby/install/shared/workflows/agents/product-manager.yaml` exists with `name: product-manager`, `enabled: false`, `priority: 1`, PLACEHOLDER banner, `instructions` block telling the agent to escalate with reason `placeholder_agent:product-manager:not_implemented` if accidentally spawned; `task_stages_registry.default_agent` for stage `prd` resolves to this row after sync. file: `src/gobby/install/shared/workflows/agents/product-manager.yaml`. test: `tests/agents/test_placeholder_agents.py::test_product_manager_placeholder_for_prd_stage`.

## P2 Stage-Native Storage + API Surface

`kind: framing`

**Goal**: Land the storage managers, the migration script that backfills `task_stage_states` from `(lifecycle, status, labels)`, and the MCP/HTTP/CLI surfaces. After Phase 2, every read and write of stage state goes through the new APIs; the dispatcher still uses the old code (Phase 3 swaps it).

### 2.1 Stage registry + stage states storage managers [category: code] (depends: 1.1)

`kind: deliverable`

Target: `src/gobby/storage/tasks/_stage_registry.py` (new), `src/gobby/storage/tasks/_stage_states.py` (new), `src/gobby/storage/tasks/_lifecycle.py` (existing — `close_task` at lines 22–67), `src/gobby/storage/tasks/_transitions.py` (existing — `close_task` at lines 716–756, `_cascade_merged_close` at lines 145–176), `src/gobby/storage/tasks/_manager.py` (existing — `LocalTaskManager.close_task` at lines 491–523)

Two new manager modules under the same package as `_artifacts.py`, `_lifecycle_events.py`, `_dispatch_mutex.py`. Wire both into `LocalTaskManager` (`src/gobby/storage/tasks/_manager.py`) as composed sub-managers, mirroring how `TaskArtifactManager` is exposed. The existing close-path files are in scope because invariant 8 (acceptance 2.1.10) requires extracting `_close_task_in_txn` from `_lifecycle.close_task` and rewiring `_transitions.close_task`, `_transitions._cascade_merged_close`, and `LocalTaskManager.close_task` to delegate through the shared helper.

`_stage_registry.py`:

```python
ReviewPolicy = Literal["none", "required", "optional"]


@dataclass(frozen=True, slots=True)
class StageRegistryEntry:
    name: str
    display_label: str
    description: str
    category: Literal["discovery","design","verification","implementation","delivery"]
    default_agent: str | None
    reviewer_agent: str | None             # nullable; populated only when review_policy != 'none'
    review_policy: ReviewPolicy
    position_hint: int
    requires_human: bool
    is_terminal: bool
    default_max_work_attempts: int         # default 3
    default_max_review_rounds: int         # default 5


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
StageState5 = Literal["ready", "in_progress", "needs_review", "review_approved", "done"]


class IllegalStageTransitionError(ValueError):
    """Raised when a transition is rejected by the row's review_policy.

    Carries (stage_name, current_state, attempted_transition, review_policy)
    so callers can recover or surface the constraint clearly. Distinct from
    generic ValueError so MCP/HTTP/CLI surfaces can map it to a typed error
    response rather than a 500.
    """
    def __init__(
        self,
        stage_name: str,
        current_state: StageState5,
        attempted_transition: str,
        review_policy: ReviewPolicy,
    ) -> None: ...


class IllegalManifestMutationError(ValueError):
    """Raised when a structural manifest mutation (`add_stage` / `remove_stage`)
    targets a position the manifest mutation contract forbids.

    Carries (task_id, target_stage_name, target_position, current_stage_name,
    current_stage_state, mutation, reason) so callers can map the rejection
    to a typed MCP/HTTP/CLI error rather than a generic 500. Distinct from
    `IllegalStageTransitionError` because it covers manifest shape changes
    (add/remove rows), not state transitions (start/submit/approve/reject/
    complete/fail) on existing rows.
    """
    def __init__(
        self,
        task_id: str,
        target_stage_name: str,
        target_position: int | None,
        current_stage_name: str | None,
        current_stage_state: StageState5 | None,
        mutation: Literal["add_stage", "remove_stage"],
        reason: Literal[
            "position_at_or_before_current",
            "current_row_not_removable",
            "done_row_not_removable",
            "would_exhaust_terminal_position",
            "stage_already_in_manifest",
            "stage_not_in_manifest",
            "manifest_exhausted",
        ],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class StageState:
    task_id: str
    stage_name: str
    position: int
    state: StageState5                     # 5-value enum; per-row legality enforced by review_policy
    review_policy: ReviewPolicy            # mirrored from registry at manifest-init; stable across registry edits
    reviewer_agent: str | None             # mirrored from registry at manifest-init
    entered_at: str | None
    entered_by_session_id: str | None
    completed_at: str | None
    completed_by_session_id: str | None
    completed_commit_sha: str | None
    work_attempt_count: int                # incremented on start_stage / fail-loop reentry
    review_round_count: int                # incremented on reject_review
    max_work_attempts: int | None          # null inherits registry default at evaluation time
    max_review_rounds: int | None          # null inherits registry default at evaluation time
    artifact_refs: dict[str, str] | None
    notes: str | None
    updated_at: str  # ISO-8601 UTC; surfaces the DB column added in migration 233 — load-bearing for the §3.3 RuntimeDispatchMutex stale-candidate snapshot check.


@dataclass(frozen=True, slots=True)
class StageManifestSpec:
    """Structured manifest-row input. Replaces the bare (stage_name, position)
    tuple so per-row cap overrides from §2.5 build flags / §3.2 manifest
    resolution flow through the manager API at manifest-init time.

    `max_work_attempts` and `max_review_rounds` are nullable; None means
    "inherit registry default at evaluation time" (the row's column stays
    NULL and §2.1 invariant 5's `state_row.max_<X> ?? registry_row.default_max_<X>`
    fallback applies). A non-None value persists onto the row's per-stage cap
    column and overrides the registry default for this task only.
    """
    stage_name: str
    position: int
    max_work_attempts: int | None = None
    max_review_rounds: int | None = None

    @classmethod
    def from_position_tuple(cls, t: tuple[str, int]) -> "StageManifestSpec":
        """Adapter for callers that still build (stage_name, position) pairs;
        per-row caps default to None (registry-inherited). Used by §1.1's
        seed loader and §2.2 backfill."""
        return cls(stage_name=t[0], position=t[1])


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
        """Drives kanban column queries. `state` accepts any of the 5 values."""

    # Writes — every mutator records a task_lifecycle_events row
    def initialize_manifest(
        self, task_id: str, specs: Sequence[StageManifestSpec], *, by_session_id: str | None,
    ) -> list[StageState]:
        """Insert manifest rows; all start at ready. Each row's review_policy
        and reviewer_agent are MIRRORED from the registry at insertion time
        (snapshot semantics). Per-row cap overrides (max_work_attempts,
        max_review_rounds) are persisted from the spec; None values stay NULL
        on the row and inherit registry defaults at evaluation time.
        Idempotent only if the target manifest matches existing rows exactly;
        otherwise raises ManifestAlreadyInitializedError."""

    def add_stage(
        self, task_id: str, spec: StageManifestSpec, *, by_session_id: str | None,
    ) -> StageState:
        """Insert a row at spec.position; reorder affected positions. Mirrors
        review_policy + reviewer_agent from the current registry row. Persists
        spec.max_work_attempts and spec.max_review_rounds (None means inherit
        registry default at evaluation time).

        Manifest-mutation legality (raises IllegalManifestMutationError on
        violation; the row's `state` is not relevant here — these checks
        guard structural shape only):

          - `spec.position` MUST be strictly greater than the current
            stage's `position` (i.e., the new row lands AFTER the current
            row in execution order). Inserting at, before, or equal to the
            current position is REJECTED with
            reason='position_at_or_before_current'. A manifest with no
            current stage (every row `done`, manifest exhausted) REJECTS
            all add_stage calls with reason='manifest_exhausted'; an
            operator may not extend a finished manifest.
          - The target `stage_name` MUST exist in `task_stages_registry`
            (raises ValueError, NOT IllegalManifestMutationError — this
            is a structural lookup error, not a contract violation).
          - The task MUST NOT already have a row for `stage_name` (raises
            IllegalManifestMutationError with reason='stage_already_in_manifest').
          - Successful insertion shifts every existing row whose
            `position >= spec.position` up by one to make room; the unique
            `(task_id, position)` index is preserved by ordering the
            UPDATEs from highest position downward in the same transaction.

        All checks run inside the per-task RuntimeDispatchMutex (invariant 6)
        so concurrent add_stage / remove_stage / start_stage calls cannot
        race past the contract."""

    def remove_stage(
        self, task_id: str, stage_name: str, *, by_session_id: str | None,
    ) -> None:
        """Delete a row; reorder positions to remain dense `range(N)` (i.e.,
        0-indexed and contiguous, `0..N-1` after the removal).

        Manifest-mutation legality (raises IllegalManifestMutationError on
        violation):

          - The target row MUST exist (raises IllegalManifestMutationError
            with reason='stage_not_in_manifest').
          - The target row's `position` MUST be strictly greater than the
            current stage's `position` (raises IllegalManifestMutationError
            with reason='position_at_or_before_current'). A manifest with
            no current stage (every row `done`, manifest exhausted)
            REJECTS all remove_stage calls with reason='manifest_exhausted';
            removing a row from a closed/done manifest would erase audit
            history.
          - The target row's `state` MUST be `ready` (raises
            IllegalManifestMutationError with reason='current_row_not_removable'
            if state ∈ {`in_progress`, `needs_review`, `review_approved`} —
            those states represent in-flight work or review that the operator
            must explicitly resolve via `fail_stage`/`reject_review` first;
            with reason='done_row_not_removable' if state == `done` —
            completed rows are immutable audit records and must not be
            erased). Note: the `position_at_or_before_current` check above
            already rejects removal of the CURRENT row (whatever its state)
            because the current row's position equals the current position
            and the contract requires strictly-greater. The state-based
            checks here cover edge cases where a row's state legitimately
            sits at `in_progress`/etc. but its position is past the
            current pointer (which should not happen under the leftmost-
            non-done contract — defense in depth).
          - The remaining manifest MUST still contain at least one row
            whose `position` is greater than or equal to the current
            stage's position (raises IllegalManifestMutationError with
            reason='would_exhaust_terminal_position' if removal would
            leave the manifest with no future or current work — this
            stranding case must be handled by completing the current
            stage normally rather than by surgical removal).
          - Successful deletion shifts every existing row whose
            `position > removed.position` down by one to keep positions
            dense `range(N)` (0-indexed, `0..N-1` after the removal).

        All checks run inside the per-task RuntimeDispatchMutex (invariant 6)."""

    def start_stage(
        self, task_id: str, stage_name: str, *,
        by_session_id: str | None, notes: str | None = None,
    ) -> StageState:
        """Transition ready → in_progress. Increments work_attempt_count.
        Legal on ALL review_policy values. Only allowed when this row's
        position equals current_stage().position (no skipping).

        Raises IllegalStageTransitionError if current state is not 'ready'."""

    def submit_for_review(
        self, task_id: str, stage_name: str, *,
        by_session_id: str | None, notes: str | None = None,
    ) -> StageState:
        """Transition in_progress → needs_review. Legal only on
        review_policy ∈ {required, optional}; raises IllegalStageTransitionError
        on review_policy='none'. Used by `mark_task_needs_review` MCP tool
        (Phase 2.6) and operator-driven review submissions."""

    def approve_review(
        self, task_id: str, stage_name: str, *,
        by_session_id: str | None, notes: str | None = None,
    ) -> StageState:
        """Transition needs_review → review_approved. Legal only on
        review_policy ∈ {required, optional}; raises IllegalStageTransitionError
        on review_policy='none'. Used by `mark_task_review_approved` MCP tool
        (Phase 2.6). review_approved is a DURABLE holding state — the
        dispatcher's tail rule (Phase 3.1 `<stage>_advance_rule`) advances
        review_approved → done in a separate transition; this method does
        NOT close the task or auto-advance."""

    def reject_review(
        self, task_id: str, stage_name: str, *,
        reason: str, by_session_id: str | None, notes: str | None = None,
    ) -> StageState:
        """Transition needs_review → ready. Increments review_round_count
        (NOT work_attempt_count). Legal only on review_policy ∈ {required,
        optional}; raises IllegalStageTransitionError on review_policy='none'.
        When review_round_count >= max_review_rounds (resolved via the row's
        max_review_rounds, falling back to registry default_max_review_rounds),
        escalates the task with reason=f'<stage>_review_failed:max' instead
        of returning to ready. Used by `mark_task_review_rejected` MCP tool
        (Phase 2.6)."""

    def complete_stage(
        self, task_id: str, stage_name: str, *,
        by_session_id: str | None, commit_sha: str | None = None,
        artifact_updates: Mapping[str, str] | None = None,
        validation_override_reason: str | None = None,
    ) -> StageState:
        """Transition to done. Legality depends on review_policy:

          - review_policy='none': in_progress → done (direct).
          - review_policy='optional': either in_progress → done (direct)
            or review_approved → done.
          - review_policy='required': review_approved → done by default.
            Direct in_progress → done is REJECTED unless
            validation_override_reason is supplied (admin escape hatch
            for emergency cutover; the override reason is logged on the
            task_lifecycle_events row and surfaces in audit reads).

        Persists commit_sha + artifact_refs.

        Terminal-close contract (invariant 8 / acceptance 2.1.9 / 2.1.10):
        when this row is the highest-position row in the task's manifest
        (i.e. completing it leaves `current_stage(task) is None`),
        `complete_stage` ALSO closes the task atomically in the same DB
        transaction by calling `_close_task_in_txn(conn, task_id,
        reason='manifest_exhausted', commit_sha=commit_sha, closed_at=now,
        closed_in_session_id=by_session_id,
        cascade_descendants=(stage_name == 'merge'))` directly on the
        already-open connection. Cascade=True is passed ONLY when the
        terminal stage is `merge` (replacing the legacy `mark_task_merged`
        cascade); non-merge terminal types (research_spike, prd_doc,
        architecture_doc) pass cascade=False.

        `complete_stage` does NOT invoke the public `close_task(...)` API.
        Public `close_task` is a thin wrapper around the SAME
        `_close_task_in_txn` helper that always passes
        `cascade_descendants=False`."""

    def fail_stage(
        self, task_id: str, stage_name: str, *,
        reason: str, needs_human: bool = False, by_session_id: str | None,
    ) -> StageState:
        """Transition `in_progress → ready` (work-failure path). Legal ONLY
        when the row's current state is `in_progress`; legal on all three
        review policies (`none`, `required`, `optional`) so long as the source
        state is `in_progress`. Calling `fail_stage` from any other source
        state (`ready`, `needs_review`, `review_approved`, `done`) raises
        `IllegalStageTransitionError(stage_name, current_state, 'fail_stage',
        review_policy)` — review rejection MUST go through `reject_review`
        (the sole site that increments `review_round_count`); approved-state
        rollback is not a supported transition (open a new manifest cycle
        via re-`start_stage` after operator escalation if needed); `done` is
        terminal.

        Counter contract: does NOT change `work_attempt_count` or
        `review_round_count` — the subsequent `start_stage` is the sole
        work-attempt increment site, and rejected reviews use `reject_review`
        for review-round increments. Cap escalation uses the
        `work_attempt_count >= effective_max_work_attempts` predicate
        evaluated inside `fail_stage` (effective cap resolved as
        `state_row.max_work_attempts ?? registry_row.default_max_work_attempts`);
        when hit, escalates via `escalate_task` rather than returning to
        `ready`. Escalation wiring goes through the existing `escalate_task`
        helper; do not write `is_escalated` directly here."""
```

**Transition legality matrix** (enforced by raising `IllegalStageTransitionError` on violation):

| From state | To state | `policy=none` | `policy=required` | `policy=optional` | Method |
|---|---|---|---|---|---|
| `ready` | `in_progress` | ✅ | ✅ | ✅ | `start_stage` |
| `in_progress` | `needs_review` | ❌ rejected | ✅ | ✅ | `submit_for_review` |
| `in_progress` | `done` | ✅ direct | ❌ unless `validation_override_reason` | ✅ direct | `complete_stage` |
| `in_progress` | `ready` | ✅ (fail) | ✅ (fail) | ✅ (fail) | `fail_stage` |
| `needs_review` | `review_approved` | ❌ rejected | ✅ | ✅ | `approve_review` |
| `needs_review` | `ready` | ❌ rejected | ✅ (review fail) | ✅ (review fail) | `reject_review` |
| `review_approved` | `done` | n/a (unreachable) | ✅ | ✅ | `complete_stage` |
| `done` | * | ❌ terminal | ❌ terminal | ❌ terminal | — |

`IllegalStageTransitionError` is raised whenever a row's `review_policy` does not permit the attempted transition, OR when the source state does not match the method's expected source. The error carries `(stage_name, current_state, attempted_transition, review_policy)` so MCP/HTTP/CLI surfaces can map it to a typed error response.

Invariants enforced in `StageStatesManager` (raise `IllegalStageTransitionError` for policy/state violations or `ValueError` for structural violations; cover with tests):

1. `position` is unique per `task_id` (DB unique index plus pre-flight check for clearer errors).
2. `stage_name` must exist in `task_stages_registry`.
3. Transitions follow the policy-aware legality matrix above. No skipping (e.g., `ready → done` is never legal). No reverse from `done`. `IllegalStageTransitionError` carries `(stage_name, current_state, attempted_transition, review_policy)` on every rejection.
4. `start_stage` requires the target row to be the current `current_stage()` (leftmost non-done row). `submit_for_review` / `approve_review` / `reject_review` require their respective source states.
5. **Counter contract.** `work_attempt_count` increments ONLY on `start_stage` (replaces the dropped tri-state-era single `attempt_count` for work-attempt tracking; no legacy label corresponds — `planning-round:N` / `qa-attempts:N` were review-round counters, not work-attempt counters, and they are subsumed by `review_round_count` on the relevant work stage's row per §2.2 backfill). `review_round_count` increments ONLY on `reject_review` (subsumes `planning-round:N` on the `planning` row and `qa-attempts:N` on the `development` row). `fail_stage` does NOT change either counter — the subsequent `start_stage` (work-fail loop) or `submit_for_review` (no count change; reject is the count site) is the next event. A `start → fail → start → fail → start` cycle yields `work_attempt_count = 3` after the third `start_stage`, regardless of how many `fail_stage` calls intervened. Cap predicates: `work_attempt_count >= effective_max_work_attempts` (escalates from `fail_stage` work-fail path); `review_round_count >= effective_max_review_rounds` (escalates from `reject_review`). Effective caps are resolved per evaluation: `state_row.max_work_attempts ?? registry_row.default_max_work_attempts`; same for review.
6. Every mutator (`initialize_manifest`, `add_stage`, `remove_stage`, `start_stage`, `submit_for_review`, `approve_review`, `reject_review`, `complete_stage`, `fail_stage`) executes inside `with RuntimeDispatchMutex(storage=<TaskDispatchMutexManager>, task_id=task_id, holder=<session_id_or_"system">, action_kind="stage_state:<stage_name>:<verb>", ttl_seconds=30):` to serialize concurrent transitions across the dispatcher, MCP tools, HTTP routes, and CLI. Import `RuntimeDispatchMutex` from `gobby.dispatch.mutex` (defined at `src/gobby/dispatch/mutex.py:27`); it wraps `TaskDispatchMutexManager.acquire_mutex(task_id, holder, kind, ttl_seconds, ...)` (at `src/gobby/storage/tasks/_dispatch_mutex.py:78`) and releases on exit. Surfaces calling these methods do not need to acquire the mutex themselves — the manager owns that contract.
7. `updated_at` is bumped to `datetime.now(UTC).isoformat()` on every affected `task_stage_states` row by every mutator: `initialize_manifest` (every inserted row), `add_stage` (the inserted row + every row whose position shifts), `remove_stage` (every row whose position shifts), `start_stage`, `submit_for_review`, `approve_review`, `reject_review`, `complete_stage`, `fail_stage` (the affected row). The DB column carries `DEFAULT (datetime('now'))` per §1.1 schema, but the manager is the canonical writer — fresh DB-default values from new rows must be overwritten with the Python-computed timestamp on the same write so callers (`StageState.updated_at` consumers, the §3.3 mutex snapshot) see a consistent ISO-8601 string. Load-bearing for the §3.3 mutex stale-candidate check: a same-state-result cycle (e.g., `start_stage` then `fail_stage` returning `in_progress → ready`) must still produce a fresh `updated_at` so the snapshot mismatches and rejects the stale candidate.
8. **Terminal-close on manifest exhaustion.** `complete_stage(task_id, stage_name)` MUST also close the task when the just-completed row is the highest-position row in the task's manifest (i.e., after the transition `current_stage(task) is None`). The close runs in the SAME DB transaction as the stage UPDATE.

    **Closure source.** `tasks.closed_at IS NOT NULL` is the canonical SQL closure predicate (`src/gobby/storage/baseline_schema.sql:265-275`); there is no `tasks.is_closed` column. `Task.is_closed` is a read-only Python projection sourced from `is_task_closed(task)` at `src/gobby/tasks/state_semantics.py:88-95` (returns `closed_at IS NOT NULL OR status == 'closed'`). Every SQL write below targets `closed_at`; every SQL filter uses `closed_at IS NULL` / `closed_at IS NOT NULL`. Python read-sites (predicates, dispatcher, projections) read `Task.is_closed`.

    **Transaction composition.** Two surfaces must compose without ambiguity: `complete_stage` (opens its own transaction for the stage UPDATE) and the existing `close_task` public API (opens its own transaction; carries open-child checks, bootstrap-ledger validation, and `force` / `validation_override_reason` semantics). The existing cascade-close behavior is NOT in `close_task` itself — it lives in `mark_task_merged` via `advance_lifecycle(..., cascade_close=True)` / `_cascade_merged_close`. Resolution: extract close+cascade into a NEW transaction-aware private helper that runs on a supplied connection without opening or committing, with explicit parameters for every behavior the two callers need:

    ```python
    def _close_task_in_txn(
        conn,
        task_id: str,
        *,
        reason: str,
        commit_sha: str | None = None,
        closed_at: str,                          # ISO-8601 UTC
        closed_in_session_id: str | None = None,
        force: bool = False,                     # bypass open-child / validation checks
        cascade_descendants: bool = False,       # invoke _cascade_merged_close-style descendant close
        validation_override_reason: str | None = None,
    ) -> None: ...
    ```

    Open-child checks and bootstrap-ledger validation run INSIDE the helper (migrated from `close_task`'s current wrapper) so both callers go through the same gate; the public-API wrapper's only added behavior is opening `db.transaction()`. The public `close_task(task_id, ...)` becomes:

    ```python
    def close_task(task_id, *, reason, commit_sha=None, closed_in_session_id=None,
                   force=False, validation_override_reason=None) -> None:
        with db.transaction() as conn:
            _close_task_in_txn(
                conn, task_id,
                reason=reason,
                commit_sha=commit_sha,
                closed_at=datetime.now(UTC).isoformat(),
                closed_in_session_id=closed_in_session_id,
                force=force,
                cascade_descendants=False,       # public close NEVER cascades by default
                validation_override_reason=validation_override_reason,
            )
    ```

    `complete_stage`'s terminal-close body is:

    ```python
    with db.transaction() as conn:
        conn.execute("UPDATE task_stage_states SET state='done', ... WHERE ...")
        if _is_highest_position_row(conn, task_id, stage_name):
            _close_task_in_txn(
                conn, task_id,
                reason="manifest_exhausted",
                commit_sha=commit_sha,
                closed_at=datetime.now(UTC).isoformat(),
                closed_in_session_id=by_session_id,
                cascade_descendants=(stage_name == "merge"),  # ONLY merge-terminal cascades
            )
    # On helper raise, outer `with` already rolled back the entire transaction.
    ```

    `cascade_descendants=True` is passed ONLY when the just-completed stage is `merge` (replacing the legacy `mark_task_merged` cascade behavior). For non-merge terminal stages (`prd` for `research_spike` / `prd_doc`, `architecture` for `architecture_doc`), cascade is False — those task types have no descendants under this epic's scope. No nested-transaction or savepoint semantics are required because the helper does not open its own. Both `close_task` and `complete_stage`'s terminal branch share one cascade-aware implementation by construction.

    **Close failure → escalate idempotently, no auto-retry.** If `_close_task_in_txn` raises (e.g., parent-blocker constraint, cascade-close validation, DB constraint), the outer `with db.transaction()` rolls back: the stage row reverts to its **pre-complete source state** and the task is NOT closed. The source state depends on the legal `complete_stage` entry path (§2.1 legality matrix): for `policy=none` terminal stages (`merge`, `prd`, `architecture`), the pre-complete state is `in_progress` and rollback restores `in_progress`; for `policy=required` terminal stages reached via the standard `submit_for_review → approve_review → complete_stage` path (e.g., explicit `gobby build --stages a,b,c` ending at `pr`, `planning`, `expansion`, `development`, or `holistic_qa`), the pre-complete state is `review_approved` and rollback restores `review_approved`; for `policy=required` direct close via `validation_override_reason` (operator-asserted bypass), the pre-complete state is `in_progress` and rollback restores `in_progress`. Implementations MUST NOT swallow close failures. After rollback, in a SEPARATE committed transaction, `complete_stage` calls a new private helper:

    ```python
    def _emit_terminal_close_failed_escalation(
        task_id: str, *, stage_name: str, error: Exception,
    ) -> None:
        """Idempotent terminal-close-failure escalation. Runs in its own
        transaction. Returns success without writing if the task is already
        escalated. Otherwise writes via the existing `escalate_task` write
        path with reason='terminal_close_failed:<stage>:<error_type>' and
        the supported signature only — NO unsupported kwargs. If the
        escalation write itself raises, logs at ERROR level and re-raises;
        DB-write failure is the only documented uncapped failure mode (an
        operational concern surfaced via logging, not a logic bug)."""
        if is_task_escalated(load_task(task_id)):
            return  # idempotent: prior failure already escalated
        try:
            escalate_task(
                task_id,
                reason=f"terminal_close_failed:{stage_name}:{type(error).__name__}",
            )
        except Exception as escalation_error:
            logger.error(
                "Terminal-close-failed escalation write failed for task %s "
                "(original error: %s; escalation error: %s)",
                task_id, error, escalation_error, exc_info=True,
            )
            raise
    ```

    The helper uses ONLY the existing `escalate_task` signature (no `detail` kwarg). Idempotency means a second `complete_stage` attempt on an already-escalated task does NOT re-write escalation state — important because the original exception is then re-raised to the caller, who may re-invoke `complete_stage` after operator intervention. After successful escalation, `complete_stage` re-raises the original close-error to the caller.

    Because §3.2's `list_automation_candidates` filters `NOT is_escalated`, the dispatcher will NOT re-spawn an agent on the next heartbeat — the escalation IS the retry cap. There is no separate retry counter; an operator must `de_escalate_task` before another `complete_stage` attempt is possible. This breaks the "rollback → next heartbeat → spawn agent → retry close → fails again" loop the F2 finding identified.

    **Uncapped failure mode.** If `_emit_terminal_close_failed_escalation` itself raises (database-write failure during the second transaction), the task is left at `(stage = <pre-complete source state>, closed_at IS NULL, escalated_at IS NULL)` and the next heartbeat may re-attempt close. This is a documented operational concern, not a logic bug — DB-write failure during escalation is the same failure mode any other write might hit. The error is logged at ERROR level so operators can surface it via the daemon log; full-system DB unavailability is outside this contract's scope.

    **Atomicity guarantee.** A candidate scan can never observe `current_stage IS NULL AND closed_at IS NULL` for a task that has reached invariant 8 via `complete_stage`: either the transaction commits (both stage `done` and `closed_at IS NOT NULL`) or it rolls back (stage restored to its pre-complete source state per the legality matrix above, `closed_at IS NULL`, escalation flagged in the follow-up transaction). The only window where `current_stage IS NULL AND closed_at IS NULL` can briefly exist is the §2.2 migration-234 backfill itself, before the acceptance 2.2.31 close-pass commits — a single bounded transaction per database lifetime. The §3.1 `is_child_parked` predicate's `current_stage is None AND NOT child.is_closed` branch (Python read of the projection) is therefore reachable ONLY in that migration window or in synthetic test fixtures that bypass `complete_stage`.

    **Close-path reuse.** For merge-terminal task types (e.g., `feature` ending at `merge`), this is the same close path Phase 4.2's `record_merge_result` success branch delegates to (`commit_sha = merge_sha`). For non-merge terminal task types (`research_spike` ending at `prd`, `prd_doc` ending at `prd`, `architecture_doc` ending at `architecture`), this is the SOLE close path — there is no separate dispatcher rule. The cascade-close behavior of the legacy `mark_task_merged` is preserved by routing through `_close_task_in_txn`.

9. **Manifest-mutation legality (`add_stage` / `remove_stage`).** Structural mutators are restricted to FUTURE `ready` rows after the current stage's position; mutations targeting the current row, past rows, `done` rows, or rows whose removal would strand the manifest are REJECTED with `IllegalManifestMutationError` carrying `(task_id, target_stage_name, target_position, current_stage_name, current_stage_state, mutation, reason)`. The legality matrix:

    | Operation | Target state | Target position vs. current | Result |
    |---|---|---|---|
    | `add_stage` | n/a (new row) | `> current.position` | ✅ allowed |
    | `add_stage` | n/a | `<= current.position` | ❌ `position_at_or_before_current` |
    | `add_stage` | n/a | any | ❌ `stage_already_in_manifest` if row exists |
    | `add_stage` | n/a | any | ❌ `manifest_exhausted` if `current_stage(task) is None` |
    | `remove_stage` | `ready` | `> current.position` | ✅ allowed |
    | `remove_stage` | `ready` | `<= current.position` | ❌ `position_at_or_before_current` |
    | `remove_stage` | `in_progress` / `needs_review` / `review_approved` | any | ❌ `current_row_not_removable` |
    | `remove_stage` | `done` | any | ❌ `done_row_not_removable` |
    | `remove_stage` | any | row not in manifest | ❌ `stage_not_in_manifest` |
    | `remove_stage` | any | any | ❌ `manifest_exhausted` if `current_stage(task) is None` |
    | `remove_stage` | `ready` | `> current.position`, but no remaining future row | ❌ `would_exhaust_terminal_position` |

    The `position_at_or_before_current` reason already catches removal of the CURRENT row (its position equals current and the contract requires strictly-greater); the state-based reasons (`current_row_not_removable`, `done_row_not_removable`) provide defense-in-depth for any future code path that might attempt a surgical removal targeting an in-flight or completed row whose position is past the current pointer (which should not happen under the leftmost-non-done current-stage contract). The `would_exhaust_terminal_position` reason covers the case where a `remove_stage` call targeting a future `ready` row would empty the post-current portion of the manifest, leaving the task with no path to terminal close — operators must complete the current stage normally rather than removing every future row.

    Both mutators run inside the per-task `RuntimeDispatchMutex` (invariant 6) so concurrent `add_stage` / `remove_stage` / `start_stage` calls cannot race past the contract. Both emit a `task_lifecycle_events` row with `from_state=f"manifest:{prev_shape_signature}"`, `to_state=f"manifest:{new_shape_signature}"` where `shape_signature` is a stable digest of the row positions and stage names so the audit trail captures structural changes distinct from state transitions. Broader operator surgery (e.g., adding before the current row, rewriting a `done` row's position) is explicitly OUT OF SCOPE for this contract; if a future epic wants those semantics, it must define `current_stage` recalculation, audit-event semantics for history rewrites, and cascade-close ramifications separately.

Every mutator emits a `task_lifecycle_events` row via the injected `TaskLifecycleEventManager`. `from_state` is `f"{stage_name}:{prev_state}"`, `to_state` is `f"{stage_name}:{new_state}"`, `reason` is the caller-supplied reason or a derived one (e.g. `"start_stage:planning"`), `by_actor` is `by_session_id` or `"system"`.

**Acceptance:**

- 2.1.1 — `_stage_registry.py` provides `StageRegistryManager` with the listed read/write methods. file: `src/gobby/storage/tasks/_stage_registry.py`. symbol: `gobby.storage.tasks._stage_registry.StageRegistryManager`.
- 2.1.2 — `_stage_states.py` provides `StageStatesManager` with the listed reads, writes, and invariants. symbol: `gobby.storage.tasks._stage_states.StageStatesManager`. test: `tests/storage/tasks/test_stage_states.py::test_position_uniqueness_enforced`.
- 2.1.3 — Every mutator emits a `task_lifecycle_events` row with the documented `from_state`/`to_state` shape. test: `tests/storage/tasks/test_stage_states.py::test_transitions_emit_events`.
- 2.1.4 — `LocalTaskManager` exposes both managers as `.stages_registry` and `.stage_states`. file: `src/gobby/storage/tasks/_manager.py`. test: `tests/storage/tasks/test_manager_exposes_stage_managers.py::test_managers_accessible`.
- 2.1.5 — Forbidden transitions raise `IllegalStageTransitionError` with the four-tuple payload `(stage_name, current_state, attempted_transition, review_policy)`. Coverage spans every illegal `(state, transition, policy)` triple in the legality matrix: `submit_for_review` on `policy=none`, `approve_review`/`reject_review` on `policy=none`, `complete_stage(in_progress → done)` on `policy=required` without `validation_override_reason`, skipping (`ready → done` direct), reverse from `done`. test: `tests/storage/tasks/test_stage_states.py::test_invalid_transitions_raise`, `tests/storage/tasks/test_stage_states.py::test_illegal_transition_error_carries_full_payload`, parameterized over the matrix in `tests/storage/tasks/test_stage_state_machine.py`.
- 2.1.6 — Concurrent `start_stage` calls on the same task serialize via `RuntimeDispatchMutex` (backed by `TaskDispatchMutexManager.acquire_mutex` against the `task_dispatch_mutex` table); only one wins and increments `work_attempt_count`, the second observes post-mutex state and either errors (row already `in_progress`) or no-ops. Same contract for `submit_for_review`, `approve_review`, `reject_review`, `complete_stage`, `fail_stage`, and structural mutators (`initialize_manifest`, `add_stage`, `remove_stage`). test: `tests/storage/tasks/test_stage_states_concurrency.py::test_mutex_serializes_writes`.
- 2.1.7 — Counter-split contract is explicit and uniform: `start_stage` is the SOLE increment site for `work_attempt_count`; `reject_review` is the SOLE increment site for `review_round_count`; `fail_stage` does NOT change either. Cap escalation: `work_attempt_count >= effective_max_work_attempts` triggers escalation from `fail_stage` (work-fail path); `review_round_count >= effective_max_review_rounds` triggers escalation from `reject_review`. Effective cap = `state_row.max_<X> ?? registry.default_max_<X>` resolved at evaluation time. A `start → fail → start → fail → start` cycle yields `work_attempt_count = 3` after the third start; a `submit → reject → start → submit → reject` cycle yields `work_attempt_count = 2` (one per start) AND `review_round_count = 2` (one per reject), independent counters. PR-rejection (Phase 4.1) and code-review/holistic-review rejection paths all use `reject_review` (review-round counter); merge-failure (Phase 4.2) uses `fail_stage` (work-attempt counter). No path adds `+1` outside `start_stage` or `reject_review`. behavior: "counter split is uniform; cap predicate is `>= effective_cap`" verified in `tests/storage/tasks/test_stage_states.py::test_fail_does_not_change_either_counter`, `tests/storage/tasks/test_stage_states.py::test_reject_review_increments_review_rounds_only`, `tests/storage/tasks/test_stage_states.py::test_start_stage_increments_work_attempts_only`, `tests/storage/tasks/test_stage_states.py::test_cap_predicate_is_gte_with_inheritance`.
- 2.1.8 — `StageState.updated_at` field is populated on every read (sourced from the DB column added in §1.1) and bumped by every mutator: `start_stage`, `submit_for_review`, `approve_review`, `reject_review`, `complete_stage`, `fail_stage`, `initialize_manifest`, `add_stage`, `remove_stage`. A `start_stage` followed by a `fail_stage` (same-state-result cycle returning `in_progress → ready`) produces two strictly-increasing `updated_at` values on the affected row. Load-bearing for the §3.3 `RuntimeDispatchMutex` snapshot: a candidate scanned at `(name, ready, T0)` whose row cycles `ready → in_progress → ready` must produce `(name, ready, T1)` with `T1 != T0` so the snapshot mismatches and the dispatch is correctly aborted. test: `tests/storage/tasks/test_stage_states.py::test_updated_at_bumped_on_every_mutator`, `tests/storage/tasks/test_stage_states.py::test_same_state_cycle_bumps_updated_at`.
- 2.1.9 — Terminal-close on manifest exhaustion (invariant 8): `complete_stage(task_id, stage_name)` calls the transaction-aware private helper `_close_task_in_txn(conn, task_id, reason='manifest_exhausted', commit_sha=commit_sha, closed_at=now, closed_in_session_id=by_session_id, cascade_descendants=(stage_name == 'merge'))` inside the same `db.transaction()` as the stage UPDATE iff the just-completed row is the highest-position row in the task's manifest (post-transition `current_stage(task) is None`). The public `close_task(...)` API delegates to the same helper with `cascade_descendants=False`. The close path is the SOLE terminal close for non-merge-terminal task types (`research_spike`, `prd_doc`, `architecture_doc`) AND the cascade-aware close for merge-terminal types (Phase 4.2 `record_merge_result` success branch delegates here with `commit_sha = merge_sha`, cascade=True). Close failure rolls the ENTIRE transaction back (`closed_at` stays NULL; the stage row reverts to its **pre-complete source state** per the §2.1 legality matrix — `in_progress` for `policy=none` terminal stages and for `policy=required` direct-close-via-`validation_override_reason`; `review_approved` for `policy=required` terminal stages reached via the standard `submit_for_review → approve_review → complete_stage` path, which includes any explicit `gobby build --stages a,b,c` manifest ending at a required-policy stage such as `pr`, `planning`, `expansion`, `development`, or `holistic_qa`) and then calls `_emit_terminal_close_failed_escalation(task_id, stage_name=stage_name, error=error)` in a SEPARATE committed transaction; the helper is idempotent (no-op when already escalated) and uses ONLY the existing `escalate_task` signature with `reason=f'terminal_close_failed:{stage_name}:{type(error).__name__}'`. The original close-error re-raises. Because §3.2's `list_automation_candidates` filters `NOT is_escalated`, the dispatcher does not re-spawn agents to retry — the escalation IS the cap. behavior: "completing the highest-position manifest row closes the task atomically with reason='manifest_exhausted'" verified in `tests/storage/tasks/test_stage_states.py::test_complete_terminal_row_closes_task`, `tests/storage/tasks/test_stage_states.py::test_complete_non_terminal_row_does_not_close`, `tests/storage/tasks/test_stage_states.py::test_close_failure_rolls_back_stage_transition`, `tests/storage/tasks/test_stage_states.py::test_close_failure_rolls_back_to_in_progress_for_policy_none_terminal` (round-21 F2 — `merge`/`prd`/`architecture` close failure restores `in_progress`, `closed_at` IS NULL, escalation emitted idempotently), `tests/storage/tasks/test_stage_states.py::test_close_failure_rolls_back_to_review_approved_for_required_policy_terminal_via_review_path` (round-21 F2 — explicit manifest ending at `pr` walked through `submit_for_review → approve_review → complete_stage`; close-helper raise restores `review_approved`, `closed_at` IS NULL, escalation emitted idempotently; parameterized over `pr`, `planning`, `expansion`, `development`, `holistic_qa`), `tests/storage/tasks/test_stage_states.py::test_close_failure_rolls_back_to_in_progress_for_required_policy_terminal_via_validation_override` (round-21 F2 — required-policy terminal closed directly via `validation_override_reason`; close-helper raise restores `in_progress`, `closed_at` IS NULL, escalation emitted idempotently), `tests/storage/tasks/test_stage_states.py::test_close_failure_escalates_with_terminal_close_failed_reason`, `tests/storage/tasks/test_stage_states.py::test_close_failure_escalates_idempotently_on_already_escalated`, `tests/storage/tasks/test_stage_states.py::test_escalation_helper_uses_supported_signature_only`, `tests/storage/tasks/test_stage_states.py::test_escalation_helper_db_write_failure_logs_and_reraises`, `tests/storage/tasks/test_stage_states.py::test_escalated_task_not_re_attempted_by_heartbeat`, `tests/storage/tasks/test_stage_states.py::test_close_task_public_api_and_complete_stage_share_helper`, `tests/storage/tasks/test_stage_states.py::test_research_spike_closes_at_prd_done`, `tests/storage/tasks/test_stage_states.py::test_merge_terminal_close_via_record_merge_result_uses_same_path`.
- 2.1.10 — `_close_task_in_txn` helper signature and caller-cascade rules: helper accepts `reason`, `commit_sha`, `closed_at`, `closed_in_session_id`, `force`, `cascade_descendants`, `validation_override_reason` parameters (canonical spelling — matches §2.1 invariant 8 code block, the `complete_stage` docstring, acceptance 2.1.9, and §4.2's narrative; one canonical helper-side keyword `commit_sha`, NOT `closed_commit_sha`). The helper runs open-child checks and bootstrap-ledger validation INSIDE the helper (migrated from current `close_task` wrapper); does NOT open or commit its own transaction. Public `close_task(...)` is a thin wrapper that opens `db.transaction()` and delegates to `_close_task_in_txn(..., commit_sha=<from public arg>, cascade_descendants=False, ...)`; if the existing public API uses a `closed_commit_sha` parameter name, the wrapper maps it to the helper's `commit_sha` at the boundary so the helper-side spelling stays canonical. `complete_stage` passes `cascade_descendants=True` ONLY when `stage_name == 'merge'` (replacing legacy `mark_task_merged` cascade); non-merge terminal stages (`prd`, `architecture`) pass cascade=False because there are no descendants under those task types. symbol: `gobby.storage.tasks._stage_states._close_task_in_txn`. test: `tests/storage/tasks/test_close_task_in_txn.py::test_helper_signature_accepts_all_canonical_params`, `tests/storage/tasks/test_close_task_in_txn.py::test_helper_uses_commit_sha_keyword_not_closed_commit_sha`, `tests/storage/tasks/test_close_task_in_txn.py::test_open_child_check_runs_inside_helper`, `tests/storage/tasks/test_close_task_in_txn.py::test_bootstrap_ledger_validation_runs_inside_helper`, `tests/storage/tasks/test_close_task_in_txn.py::test_close_task_public_api_passes_cascade_false`, `tests/storage/tasks/test_close_task_in_txn.py::test_close_task_public_wrapper_maps_closed_commit_sha_to_commit_sha`, `tests/storage/tasks/test_close_task_in_txn.py::test_complete_stage_merge_terminal_passes_cascade_true`, `tests/storage/tasks/test_close_task_in_txn.py::test_complete_stage_non_merge_terminal_passes_cascade_false`, `tests/storage/tasks/test_close_task_in_txn.py::test_force_and_validation_override_pass_through`.
- 2.1.11 — Per-row `review_policy` and `reviewer_agent` are mirrored from the registry at `initialize_manifest` / `add_stage` time and persist on `task_stage_states`. A subsequent registry edit (e.g., `research.review_policy: none → required`) does NOT retroactively change the legality of transitions on already-created rows; only manifests created after the edit see the new policy. Verified by: (a) seed registry with `research.review_policy=none`; (b) initialize a task's manifest; (c) flip registry to `research.review_policy=required` via `StageRegistryManager.upsert`; (d) confirm the existing row's `review_policy` is still `none` and `submit_for_review` on it still raises `IllegalStageTransitionError`. test: `tests/storage/tasks/test_stage_states.py::test_review_policy_mirrored_at_init_not_retroactive`, `tests/storage/tasks/test_stage_states.py::test_reviewer_agent_mirrored_at_init`.
- 2.1.12 — `validation_override_reason` admin escape: `complete_stage(in_progress → done)` on a `policy=required` row is REJECTED with `IllegalStageTransitionError` unless `validation_override_reason` is supplied. When supplied, the override reason is logged on the emitted `task_lifecycle_events` row (in `reason` field as `validation_override:<reason>`) so the audit trail shows why review was bypassed. The override does NOT cascade to other rows. test: `tests/storage/tasks/test_stage_states.py::test_complete_stage_required_policy_rejects_without_override`, `tests/storage/tasks/test_stage_states.py::test_validation_override_reason_logged_on_event_row`.
- 2.1.13 — Manifest-mutation legality (invariant 9): `StageStatesManager.add_stage(task_id, spec, by_session_id=...)` and `remove_stage(task_id, stage_name, by_session_id=...)` enforce the legality matrix above and raise `IllegalManifestMutationError(task_id, target_stage_name, target_position, current_stage_name, current_stage_state, mutation, reason)` on violation. The error class is distinct from `IllegalStageTransitionError` and carries the documented seven-tuple payload. Coverage spans every row of the legality matrix: (a) `add_stage` at `position > current.position` succeeds and shifts higher positions up by one in the same transaction; (b) `add_stage` at `position <= current.position` raises with `reason='position_at_or_before_current'`; (c) `add_stage` for a `stage_name` already in the manifest raises with `reason='stage_already_in_manifest'`; (d) `add_stage` on a manifest with `current_stage is None` (all rows `done`) raises with `reason='manifest_exhausted'`; (e) `remove_stage` of a `ready` row at `position > current.position` succeeds and reorders dense; (f) `remove_stage` of a row at `position <= current.position` raises with `reason='position_at_or_before_current'`; (g) `remove_stage` of an `in_progress` / `needs_review` / `review_approved` row raises with `reason='current_row_not_removable'`; (h) `remove_stage` of a `done` row raises with `reason='done_row_not_removable'`; (i) `remove_stage` of a stage not in the manifest raises with `reason='stage_not_in_manifest'`; (j) `remove_stage` on a manifest with `current_stage is None` raises with `reason='manifest_exhausted'`; (k) `remove_stage` of the only remaining future `ready` row (would leave no path to terminal close) raises with `reason='would_exhaust_terminal_position'`. Both mutators emit a `task_lifecycle_events` row with `from_state=f"manifest:{prev_shape_signature}"`, `to_state=f"manifest:{new_shape_signature}"` for the audit trail. The `current_stage` pointer is unchanged after every allowed `add_stage` and `remove_stage` (the current row is untouched by definition). symbol: `gobby.storage.tasks._stage_states.IllegalManifestMutationError`. test: `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_add_stage_after_current_succeeds`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_add_stage_at_current_position_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_add_stage_before_current_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_add_stage_existing_row_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_add_stage_on_exhausted_manifest_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_stage_future_ready_succeeds`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_stage_at_current_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_stage_before_current_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_in_progress_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_needs_review_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_review_approved_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_done_row_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_missing_stage_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_on_exhausted_manifest_rejected`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_last_future_row_rejected_would_exhaust`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_current_stage_unchanged_after_allowed_add`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_current_stage_unchanged_after_allowed_remove`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_illegal_mutation_error_carries_full_payload`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_mutation_emits_lifecycle_event_with_shape_signatures`, `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_add_stage_preserves_zero_indexed_dense_positions` (parameterized: every successful `add_stage` outcome's positions equal `range(N)` exactly), `tests/storage/tasks/test_stage_states_manifest_mutation.py::test_remove_stage_preserves_zero_indexed_dense_positions` (parameterized: every successful `remove_stage` outcome's positions equal `range(N-1)` exactly).

### 2.2 One-shot backfill: derive `task_stage_states` from existing `(lifecycle, status, labels)` [category: code] (depends: 2.1)

`kind: deliverable`

Target: `src/gobby/storage/migrations.py` (new helper `_backfill_task_stage_states_from_legacy`), invoked from migration version 234.

The backfill runs once during the migration to 234. For every task in `tasks`:

0. **Front-half conductor-stage override.** If the task carries any `conductor-stage:<X>` label (created today by `front_half_tick` in `src/gobby/mcp_proxy/tools/tasks/_front_half.py`), the resolved manifest is OVERRIDDEN to a single-row manifest containing only the canonical stage that the conductor child anchored, instead of the default `task_type` manifest. The legacy `task_type='task'` default `[development, pr, merge]` is the wrong manifest for these stage anchors — the planner / plan-adversary / expansion / test-architect agents call `mark_task_*` against these anchors and §2.6 resolves `current_stage(anchor_id)`, so the anchor's manifest must place its stage row at the correct state. Override mapping (table-row decomposition rule applies; one acceptance per row in 2.2.7a-d):

   | Conductor-stage label | Override manifest | Source-state derivation |
   |-----------------------|-------------------|--------------------------|
   | `conductor-stage:requirements` | `[prd]` | The requirements anchor produces a PRD; `prd.review_policy=none`. Status mapping: `open`/`in_progress` → `prd.in_progress`; `needs_review` → `prd.in_progress` (collapses, policy=none); `review_approved` → `prd.done`; `closed` → `prd.done`; `escalated` handled by pre-normalization rule N1 below. |
   | `conductor-stage:planning` | `[planning]` | `planning.review_policy=required`. Status mapping: `open`/`in_progress` → `planning.in_progress`; `needs_review` → `planning.needs_review`; `review_approved` → `planning.review_approved`; `closed` → `planning.done`; `escalated` handled by N1 below. |
   | `conductor-stage:expansion` | `[expansion]` | `expansion.review_policy=required`. Status mapping: `open`/`in_progress` → `expansion.in_progress`; `needs_review` → `expansion.needs_review`; `review_approved` → `expansion.review_approved`; `closed` → `expansion.done`; `escalated` handled by N1 below. |
   | `conductor-stage:test-architecture` | `[test_arch]` | `test_arch.review_policy=none`. Status mapping: `open`/`in_progress` → `test_arch.in_progress`; `needs_review` → `test_arch.in_progress` (collapses, policy=none); `review_approved` → `test_arch.done`; `closed` → `test_arch.done`; `escalated` handled by N1 below. |

   The override fires BEFORE step 1's `task_type_default_stages` lookup. Conductor-stage tasks are leaves under their parent (the autonomous front-half flow); the parent task itself walks the regular `task_type='epic'` (or other) manifest path independently. After the override row is inserted, the migration drops the `conductor-stage:<X>` label so the cutover removes the legacy stage-child marker once and only once. The acceptance 2.6.1-2.6.5 review-tool tests re-run against a fixture migrated from a `conductor-stage:planning` anchor at `(lifecycle='open', status='needs_review')` to prove the rewired tools find `planning.needs_review` as `current_stage` and transition to `planning.review_approved` cleanly.

1. **Pre-normalization** (applied to every task's `(lifecycle, status)` BEFORE the mapping table lookup; both rules are deterministic and exhaustive over the legacy state space):

   - **N1. `status='escalated'`**: set `is_escalated=1` on the task row (idempotent with step 6); substitute `status := 'in_progress'` for the mapping-table lookup. Every legacy lifecycle is reachable with `status='escalated'` (operator escalation, dispatcher cap-hit escalation, conductor escalation), so this rule covers all 8 lifecycle values for which escalation is reachable (`open`, `plan_review`, `test_arch`, `expanding`, `in_development`, `holistic_review`, `pr`, `merging`) plus the conductor-stage anchors from step 0. The post-normalization tuple `(lifecycle, 'in_progress')` is then resolved against the mapping table or the conductor-stage override above.
   - **N2. `status='closed'` AND `lifecycle != 'merged'`** (terminal-close-without-merge case — abandoned tasks, `wont_fix`, `obsolete`, etc.): predecessors of the lifecycle's stage AND the lifecycle's stage itself are `done`; successors are `ready`. `closed_at` is preserved from the legacy row (already populated on `tasks.closed_at` by the legacy close path). For conductor-stage anchors (single-row manifest from step 0), `closed` collapses to that single stage `done`. The merged-and-closed path (`lifecycle='merged' AND status='closed'`) stays in the mapping table proper as the all-`done` row.

   These two rules are formalized in `_backfill_task_stage_states_from_legacy` as functions `_normalize_status_escalated(...)` and `_normalize_status_closed_non_merged(...)` invoked in that order before `_resolve_state_from_lifecycle_status(...)`. The pre-normalization output is what the census step (below) emits, so the census never sees raw `escalated` or non-merged-`closed` tuples; an unknown post-normalization tuple still fails loudly via the existing assertion path.

2. Resolve task's manifest from `task_type_default_stages` for that `task_type` minus any `stage-:<name>` skip labels (so existing skip labels are honored exactly once). Skipped if the conductor-stage override from step 0 fired.
3. Walk the resolved manifest in position order and assign `state` per the mapping table below, derived from the task's current `(lifecycle, status, labels)` AFTER pre-normalization. Each inserted row has its `review_policy` and `reviewer_agent` mirrored from the registry at insertion time (per §2.1 invariant 11).
4. Populate `entered_at` / `completed_at` from the task's `updated_at` and `created_at` as a coarse approximation; `entered_by_session_id` and `completed_by_session_id` use `claimed_by_session_id` if available, else `closed_in_session_id`, else `NULL`.
5. Populate `review_round_count` from the legacy review-round labels (these labels historically tracked review iterations, not work attempts): `planning-round:N` (numeric suffix) → `planning.review_round_count`; `qa-attempts:N` (numeric suffix) → `development.review_round_count` (development subsumes the legacy code_review_qa rounds). Fall back to `0` when the label is absent. Populate `work_attempt_count` from `0` for every backfilled row — no legacy column or label tracks work attempts directly; the historical signal lives in `task_lifecycle_events` but is not worth back-deriving (the dispatcher only needs the counter to be monotonic going forward, and starting at `0` is correct for any row that has not yet entered a fresh `start_stage` post-cutover). Migrate per-stage caps from the five legacy `task_artifacts` cap columns into the per-stage cap columns on the corresponding rows per the §1.1 authoritative inventory: `max_expansion_attempts → expansion.max_work_attempts`; `max_qa_rounds → development.max_review_rounds`; `max_merge_attempts → merge.max_work_attempts`; `max_holistic_rounds → holistic_qa.max_review_rounds`; `max_review_rounds → pr.max_review_rounds`. NULL legacy values stay NULL on the per-stage row (inherit registry defaults at evaluation time). Stages with no legacy cap column (`planning`, `test_arch`, `ideation`, `research`, `architecture`, `prd`) get NULL `max_work_attempts` and NULL `max_review_rounds` on every backfilled row; effective caps fall through to the registry defaults.
6. Drop the `stage-:<name>` skip labels (already encoded as "stage absent from manifest") AND drop the `conductor-stage:<X>` labels for any task whose manifest was overridden by step 0 (the legacy conductor-stage marker has served its purpose; post-migration the front-half flow is retired).
7. `UPDATE tasks SET is_escalated = 1 WHERE escalated_at IS NOT NULL` — column was added at default 0 in migration 233; this is the one-shot backfill so projections that read `tasks.is_escalated` (Phase 3.2 readiness rewrite) see correct values from migration 234 onward. Idempotent with pre-normalization rule N1's per-task `is_escalated=1` write.

Mapping table (`(lifecycle, status)` → manifest result; gaps from the discarded tri-state contract are filled in per the amended strategy plan's step 6):

| lifecycle | status | Resulting per-row state |
|-----------|--------|-------------------------|
| `open` | `open` | All manifest rows `ready` |
| `open` | any other | All `ready` (`status` overrides handled below) |
| `plan_review` | `open` or `in_progress` | `planning` row `in_progress`, predecessors `done`, successors `ready` |
| `plan_review` | `needs_review` | `planning` row `needs_review`, predecessors `done`, successors `ready` |
| `plan_review` | `review_approved` | `planning` row `review_approved`, predecessors `done`, successors `ready` |
| `test_arch` | `open` or `in_progress` | `test_arch` row `in_progress`, predecessors `done`, successors `ready` |
| `test_arch` | `needs_review` | `test_arch` row `in_progress`, predecessors `done`, successors `ready` (test_arch.review_policy=none — `needs_review` collapses to `in_progress` since the legacy state has no policy-aware home) |
| `test_arch` | `review_approved` | `test_arch` row `done`, predecessors `done`, successors `ready` (test_arch.review_policy=none — `review_approved` collapses to `done`) |
| `expanding` | `open` or `in_progress` | `expansion` row `in_progress`, predecessors `done`, successors `ready` |
| `expanding` | `needs_review` | `expansion` row `needs_review`, predecessors `done`, successors `ready` |
| `expanding` | `review_approved` | `expansion` row `review_approved`, predecessors `done`, successors `ready` |
| `in_development` | `open` or `in_progress` | `development` row `in_progress`, predecessors `done`, successors `ready` |
| `in_development` | `needs_review` | `development` row `needs_review`, predecessors `done`, successors `ready` |
| `in_development` | `review_approved` | `development` row `review_approved`, predecessors `done`, successors `ready` (leaf-park; epics scan children) |
| `holistic_review` | `open` or `in_progress` | `holistic_qa` row `in_progress`, predecessors `done`, successors `ready` |
| `holistic_review` | `needs_review` | `holistic_qa` row `needs_review`, predecessors `done`, successors `ready` |
| `holistic_review` | `review_approved` | `holistic_qa` row `review_approved`, predecessors `done`, successors `ready` |
| `pr` | `open` or `in_progress` | `pr` row `in_progress`, predecessors `done`, `merge.ready` |
| `pr` | `needs_review` | `pr` row `needs_review` with `pr_url` populated, predecessors `done`, `merge.ready` |
| `pr` | `review_approved` | `pr` row `review_approved`, predecessors `done`, `merge.ready` |
| `merging` | any non-terminal | `merge` row `in_progress`, predecessors `done` |
| `merged` | `closed` | All rows `done` |

`status='escalated'` and non-`merged` `status='closed'` are NOT separate mapping-table rows — they are handled by the deterministic pre-normalization rules N1 and N2 in step 1 above. The mapping table is the post-normalization lookup; every row in it carries a normalized `status` value (`open`, `in_progress`, `needs_review`, `review_approved`, or the literal `closed` only on the `(merged, closed)` terminal row). The pre-normalization layer is what makes the table exhaustive over the legacy state space without exploding into a per-lifecycle row for every `escalated` / non-merged-`closed` combo.

Pre-migration audit: emit a `(lifecycle, normalized_status, count)` census to `src/gobby/storage/migrations.py` log output AFTER applying the pre-normalization rules N1/N2. If the census includes a `(lifecycle, normalized_status)` tuple not in the mapping table or in the conductor-stage override (step 0), fail the migration with a clear message — this forces the operator (or the implementing agent) to extend the table rather than silently produce wrong rows. The pre-normalization layer guarantees that every reachable legacy `(lifecycle, raw_status)` either resolves into a mapping-table row, into a conductor-stage override row, or is rejected loudly with a named tuple.

`task_type` defaults at migration time. Six task type values are seeded across five distinct manifests; `chore` and `task` share the leaves-only manifest. None of the seeded manifests reference the dropped review stages (`adversarial_review`, `expansion_qa`, `code_review_qa`); review is now state on the work stage.

| task_type | manifest |
|-----------|----------|
| `epic` | `[ideation, research, architecture, prd, planning, test_arch, expansion, development, holistic_qa, pr, merge]` (full 11-stage pipeline) |
| `feature` | `[planning, test_arch, expansion, development, pr, merge]` |
| `bug` | `[development, pr, merge]` |
| `refactor` | `[planning, development, pr, merge]` |
| `chore` | `[development, pr, merge]` |
| `task` | `[development, pr, merge]` |

`holistic_qa` is **epic-level only** — it appears only in `epic`'s manifest. Leaf manifests omit it; the dispatcher's `all_leaves_holistic_rule` (Phase 3.1) advances the epic's `holistic_qa.ready → in_progress` once every direct child is parked.

These six defaults are seeded inline by migration 233 (Phase 1.1, in the same transaction as the schema creation — see F1 fix). Migration 234 reads them when resolving per-task manifests during the backfill; it does NOT re-write them. Phase 5.1 (migration 235) adds the four new task types (`simple_fix`, `research_spike`, `architecture_doc`, `prd_doc`) to the same `task_type_default_stages` table.

After backfill, drop the `stage-:<name>` labels from every task. Do not drop `planning-round:` or `qa-attempts:` labels in this migration — they're still readable for diagnostics; Phase 7 cleans them up.

**Close-pass for all-`done` manifests (invariant-8 retroactive enforcement).** `tasks.closed_at IS NOT NULL` is the canonical SQL closure predicate (no `is_closed` column exists; `Task.is_closed` is a Python projection at `state_semantics.py:88-95`). After the row inserts and label drops, the migration runs a final SQL pass inside the same migration-234 transaction:

```sql
UPDATE tasks
   SET closed_at = datetime('now'),
       closed_in_session_id = 'migration:234'
 WHERE closed_at IS NULL
   AND EXISTS (SELECT 1 FROM task_stage_states tss WHERE tss.task_id = tasks.id)
   AND NOT EXISTS (
       SELECT 1 FROM task_stage_states tss
        WHERE tss.task_id = tasks.id AND tss.state != 'done'
   );
```

The inner `EXISTS` clause guards against tasks with no manifest rows (defensive — should not exist post-backfill). This pass retroactively enforces §2.1 invariant 8 for pre-epic tasks so the `current_stage IS NULL AND closed_at IS NULL` state never persists past the migration commit. The mapping table's `(merged, closed)` row already implies `closed_at IS NOT NULL` from the legacy close path, so this pass is mainly a safety net for any other path that would emit an all-`done` manifest (manual fixture data, direct DB edits, or future mapping-table extensions that close non-merged tasks). After migration 234 commits, the §3.1 `is_child_parked` predicate's `current_stage is None AND NOT child.is_closed` branch is unreachable in normal operation.

**Acceptance:**

- 2.2.1 — Migration version 234 in `MIGRATIONS` performs the backfill in a single transaction. file: `src/gobby/storage/migrations.py`. symbol: `gobby.storage.migrations.MIGRATIONS` (entry 234).
- 2.2.2 — Every observed `(lifecycle, status)` tuple in a fixture DB produces rows matching the mapping table. test: `tests/storage/test_migration_234_backfill.py::test_mapping_exhaustive`.
- 2.2.3 — Unmapped `(lifecycle, status)` tuples cause migration failure with a message naming the offending tuple. test: `tests/storage/test_migration_234_backfill.py::test_unmapped_tuple_fails_loudly`.
- 2.2.4 — `review_round_count` populated from the legacy review-round labels: `planning-round:N` → `planning.review_round_count`; `qa-attempts:N` → `development.review_round_count`. `work_attempt_count` populated as `0` for every backfilled row (no legacy work-attempt counter exists; counter is monotonic from cutover forward). Per-stage caps migrated from all five legacy `task_artifacts` cap columns per the §1.1 authoritative inventory: `max_expansion_attempts → expansion.max_work_attempts`; `max_qa_rounds → development.max_review_rounds`; `max_merge_attempts → merge.max_work_attempts`; `max_holistic_rounds → holistic_qa.max_review_rounds`; `max_review_rounds → pr.max_review_rounds`. NULL legacy values yield NULL per-stage values (inherit registry defaults). Stages with no legacy cap column (`planning`, `test_arch`, and the four discovery stages) get NULL caps on every row. test: `tests/storage/test_migration_234_backfill.py::test_planning_round_label_populates_review_round_count`, `tests/storage/test_migration_234_backfill.py::test_qa_attempts_label_populates_development_review_round_count`, `tests/storage/test_migration_234_backfill.py::test_work_attempt_count_starts_zero`, `tests/storage/test_migration_234_backfill.py::test_max_expansion_attempts_migrates_to_expansion_work_cap`, `tests/storage/test_migration_234_backfill.py::test_max_qa_rounds_migrates_to_development_review_cap`, `tests/storage/test_migration_234_backfill.py::test_max_merge_attempts_migrates_to_merge_work_cap`, `tests/storage/test_migration_234_backfill.py::test_max_holistic_rounds_migrates_to_holistic_qa_review_cap`, `tests/storage/test_migration_234_backfill.py::test_max_review_rounds_migrates_to_pr_review_cap`, `tests/storage/test_migration_234_backfill.py::test_null_legacy_caps_stay_null_post_backfill`, `tests/storage/test_migration_234_backfill.py::test_planning_and_test_arch_get_null_caps_inherit_registry_default`.
- 2.2.5 — Migration 234 reads the six `task_type_default_stages` bundles seeded inline by migration 233 (Phase 1.1 acceptance 1.1.7) when resolving per-task manifests; it does not re-seed the defaults. Both fresh-DB and upgrading-DB paths produce identical resolved manifests for the same `(task_type, labels)` input. test: `tests/storage/test_migration_234_backfill.py::test_uses_233_seeded_defaults`.
- 2.2.6 — `stage-:<name>` labels removed from every task post-backfill. test: `tests/storage/test_migration_234_backfill.py::test_skip_labels_dropped`.
- 2.2.7 — `tasks.is_escalated` backfilled from `escalated_at IS NOT NULL` in migration 234; rows with `status='escalated'` map to `is_escalated=1`, all other rows to 0. test: `tests/storage/test_migration_234_backfill.py::test_is_escalated_backfilled`.

Front-half conductor-stage anchor coverage (one acceptance per data row of the `conductor-stage:<X>` override table in step 0, per the plan-coverage contract's table-row decomposition rule). These tests prove that the §2.6 review-tool rewire (acceptances 2.6.1–2.6.5) operates correctly against migrated stage anchors — the failure mode F1 from round 11 (planning anchor at `(open, needs_review)` resolving to default `task` manifest `[development, pr, merge]` and stalling on `IllegalStageTransitionError`) is fixed by these rows:

- 2.2.7a — Override `conductor-stage:requirements` produces single-row manifest `[prd]` with state derived from `(lifecycle, status)` per the override table; `prd.review_policy=none` collapses `needs_review` to `in_progress` and `review_approved` to `done` on the inserted row. After backfill, the `conductor-stage:requirements` label is removed from the task. test: `tests/storage/test_migration_234_backfill.py::test_conductor_stage_requirements_open_in_progress`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_requirements_needs_review_collapses_to_in_progress`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_requirements_review_approved_collapses_to_done`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_requirements_closed_to_done`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_requirements_label_dropped`.
- 2.2.7b — Override `conductor-stage:planning` produces single-row manifest `[planning]` with state derived from `(lifecycle, status)` per the override table; `planning.review_policy=required` so `needs_review` → `planning.needs_review`, `review_approved` → `planning.review_approved`, `closed` → `planning.done`. After backfill, the `conductor-stage:planning` label is removed; the rewired `mark_task_review_approved` (Phase 2.6) on a fixture migrated from `(lifecycle='open', status='needs_review')` resolves `current_stage = planning at needs_review`, calls `approve_review('planning')`, and produces `planning.review_approved` cleanly. test: `tests/storage/test_migration_234_backfill.py::test_conductor_stage_planning_open_in_progress`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_planning_needs_review`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_planning_review_approved`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_planning_closed_to_done`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_planning_label_dropped`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_planning_anchor_review_approved_via_2_6_review_tool`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_planning_anchor_review_rejected_via_2_6_review_tool`.
- 2.2.7c — Override `conductor-stage:expansion` produces single-row manifest `[expansion]` with state derived from `(lifecycle, status)` per the override table; `expansion.review_policy=required` so the four review-state mappings round-trip cleanly. After backfill, the `conductor-stage:expansion` label is removed; the §2.6 rewired tools operate against the migrated anchor without raising `IllegalStageTransitionError`. test: `tests/storage/test_migration_234_backfill.py::test_conductor_stage_expansion_open_in_progress`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_expansion_needs_review`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_expansion_review_approved`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_expansion_closed_to_done`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_expansion_label_dropped`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_expansion_anchor_review_approved_via_2_6_review_tool`.
- 2.2.7d — Override `conductor-stage:test-architecture` produces single-row manifest `[test_arch]` with state derived from `(lifecycle, status)` per the override table; `test_arch.review_policy=none` so `needs_review` collapses to `in_progress` and `review_approved` collapses to `done` on the inserted row. After backfill, the `conductor-stage:test-architecture` label is removed. The audit in 2.6.6 has already rewritten `test-architect.yaml` to call `complete_stage` (NOT a review tool) so a migrated `test_arch` anchor advances cleanly without ever entering `needs_review`. test: `tests/storage/test_migration_234_backfill.py::test_conductor_stage_test_architecture_open_in_progress`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_test_architecture_needs_review_collapses_to_in_progress`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_test_architecture_review_approved_collapses_to_done`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_test_architecture_closed_to_done`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_test_architecture_label_dropped`, `tests/storage/test_migration_234_backfill.py::test_conductor_stage_test_architecture_anchor_advances_via_complete_stage`.

Pre-normalization rule coverage (one acceptance per pre-normalization rule branch in step 1, per the plan-coverage contract's normalized-rule-branch decomposition rule). These tests prove that legacy `(lifecycle, status)` tuples in the `escalated` and non-merged-`closed` classes are normalized into mapping-table rows BEFORE the census step, and the census no longer rejects those legacy tuples — the failure mode F2 from round 11 (migration 234 failing on legacy `escalated` / non-merged-`closed` rows) is fixed by these rules:

- 2.2.7e — Pre-normalization rule **N1 (escalated)**: every reachable `(lifecycle, 'escalated')` tuple is normalized to `(lifecycle, 'in_progress')` for the mapping-table lookup AND `tasks.is_escalated` is set to 1 on the task row. The 8 reachable lifecycles (`open`, `plan_review`, `test_arch`, `expanding`, `in_development`, `holistic_review`, `pr`, `merging`) are exercised individually plus the conductor-stage override path (a `conductor-stage:planning` anchor at `(lifecycle='open', status='escalated')` produces `planning.in_progress` with `is_escalated=1`). The post-normalization census contains zero `escalated` tuples; the census-failure assertion does not fire on any escalated row. file: `src/gobby/storage/migrations.py` (helper `_normalize_status_escalated`). test: `tests/storage/test_migration_234_backfill.py::test_normalize_n1_open_escalated`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_plan_review_escalated`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_test_arch_escalated`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_expanding_escalated`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_in_development_escalated`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_holistic_review_escalated`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_pr_escalated`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_merging_escalated`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_conductor_stage_planning_escalated`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_sets_is_escalated_one`, `tests/storage/test_migration_234_backfill.py::test_normalize_n1_census_contains_no_escalated_tuples`.
- 2.2.7f — Pre-normalization rule **N2 (non-merged closed)**: every reachable `(lifecycle, 'closed')` tuple where `lifecycle != 'merged'` produces a manifest where predecessors of the lifecycle's stage AND the lifecycle's stage itself are `done` and successors are `ready`; `tasks.closed_at` is preserved (not overwritten). The 8 reachable non-merged lifecycles are exercised individually plus the conductor-stage override path (a `conductor-stage:planning` anchor at `(lifecycle='plan_review', status='closed')` produces `planning.done` with the `conductor-stage:planning` label dropped and `closed_at` preserved). The merged-and-closed path stays in the mapping table proper (`(merged, closed)` row → all `done`); N2 never fires on `lifecycle='merged'`. file: `src/gobby/storage/migrations.py` (helper `_normalize_status_closed_non_merged`). test: `tests/storage/test_migration_234_backfill.py::test_normalize_n2_open_closed`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_plan_review_closed`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_test_arch_closed`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_expanding_closed`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_in_development_closed`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_holistic_review_closed`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_pr_closed`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_merging_closed`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_conductor_stage_planning_closed`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_preserves_closed_at`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_does_not_fire_on_merged_lifecycle`, `tests/storage/test_migration_234_backfill.py::test_normalize_n2_census_contains_no_non_merged_closed_tuples`.
- 2.2.7g — **Census exhaustiveness regression**: a fixture DB containing one task per cell of the cross-product `lifecycle × status` (where status ∈ {`open`, `in_progress`, `needs_review`, `review_approved`, `escalated`, `closed`}) plus one task per `conductor-stage:<X>` × the same status set passes migration 234 without raising the unmapped-tuple error. Every row produces exactly one entry in `task_stage_states` (or N entries for a multi-row manifest); zero rows are silently skipped; zero rows raise. file: `src/gobby/storage/migrations.py`. test: `tests/storage/test_migration_234_backfill.py::test_census_exhaustive_over_full_cross_product`, `tests/storage/test_migration_234_backfill.py::test_census_exhaustive_over_conductor_stage_anchors`.

Per-row mapping coverage (one acceptance per `(lifecycle, status)` mapping table data row, per the plan-coverage contract's table-row decomposition rule):

- 2.2.8 — Mapping `(open, open)`: every manifest row `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_open_open`.
- 2.2.9 — Mapping `(open, any-other)`: every manifest row `ready`; `status`-driven overrides handled by subsequent rows. test: `tests/storage/test_migration_234_backfill.py::test_map_open_other`.
- 2.2.10 — Mapping `(plan_review, open|in_progress)`: `planning` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_plan_review_open`.
- 2.2.11 — Mapping `(plan_review, needs_review)`: `planning` `needs_review`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_plan_review_needs_review`.
- 2.2.12 — Mapping `(plan_review, review_approved)`: `planning` `review_approved`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_plan_review_approved`.
- 2.2.13 — Mapping `(test_arch, open|in_progress)`: `test_arch` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_test_arch_open`.
- 2.2.13a — Mapping `(test_arch, needs_review)`: `test_arch` `in_progress` (test_arch.review_policy=none collapses `needs_review` to `in_progress`). test: `tests/storage/test_migration_234_backfill.py::test_map_test_arch_needs_review_collapses_to_in_progress`.
- 2.2.13b — Mapping `(test_arch, review_approved)`: `test_arch` `done` (test_arch.review_policy=none collapses `review_approved` to `done`). test: `tests/storage/test_migration_234_backfill.py::test_map_test_arch_review_approved_collapses_to_done`.
- 2.2.14 — Mapping `(expanding, open|in_progress)`: `expansion` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_expanding_open`.
- 2.2.15 — Mapping `(expanding, needs_review)`: `expansion` `needs_review`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_expanding_needs_review`.
- 2.2.15a — Mapping `(expanding, review_approved)`: `expansion` `review_approved`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_expanding_review_approved`.
- 2.2.16 — Mapping `(in_development, open|in_progress)`: `development` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_in_development_open`.
- 2.2.17 — Mapping `(in_development, needs_review)`: `development` `needs_review`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_in_development_needs_review`.
- 2.2.18 — Mapping `(in_development, review_approved)`: `development` `review_approved`, predecessors `done`, successors `ready` (leaf-park; epics scan children). test: `tests/storage/test_migration_234_backfill.py::test_map_in_development_approved`.
- 2.2.19 — Mapping `(holistic_review, open|in_progress)`: `holistic_qa` `in_progress`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_holistic_review_open`.
- 2.2.19a — Mapping `(holistic_review, needs_review)`: `holistic_qa` `needs_review`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_holistic_review_needs_review`.
- 2.2.20 — Mapping `(holistic_review, review_approved)`: `holistic_qa` `review_approved`, predecessors `done`, successors `ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_holistic_review_approved`.
- 2.2.21 — Mapping `(pr, open|in_progress)`: `pr` `in_progress`, predecessors `done`, `merge.ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_pr_open`.
- 2.2.22 — Mapping `(pr, needs_review)`: `pr` `needs_review` with `pr_url` populated, predecessors `done`, `merge.ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_pr_needs_review`.
- 2.2.22a — Mapping `(pr, review_approved)`: `pr` `review_approved`, predecessors `done`, `merge.ready`. test: `tests/storage/test_migration_234_backfill.py::test_map_pr_review_approved`.
- 2.2.23 — Mapping `(merging, any-non-terminal)`: `merge` `in_progress`, predecessors `done`. test: `tests/storage/test_migration_234_backfill.py::test_map_merging`.
- 2.2.24 — Mapping `(merged, closed)`: every manifest row `done`. test: `tests/storage/test_migration_234_backfill.py::test_map_merged_closed`.

Per-task-type default-manifest coverage (one acceptance per data row of the task-type defaults table, per the plan-coverage contract's table-row decomposition rule; these are the manifest bundles seeded by migration 233 inline per F1, validated by 234's resolution path):

- 2.2.25 — Default manifest for `epic`: 11-stage pipeline `[ideation, research, architecture, prd, planning, test_arch, expansion, development, holistic_qa, pr, merge]`. `holistic_qa` is present in `epic` only; no leaf manifest contains it. test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_epic`.
- 2.2.26 — Default manifest for `feature`: `[planning, test_arch, expansion, development, pr, merge]`. None of the dropped review stages (`adversarial_review`, `expansion_qa`, `code_review_qa`) appear; review is encoded as state on the work stages. test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_feature`.
- 2.2.27 — Default manifest for `bug`: `[development, pr, merge]`. test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_bug`.
- 2.2.28 — Default manifest for `refactor`: `[planning, development, pr, merge]`. test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_refactor`.
- 2.2.29 — Default manifest for `chore`: `[development, pr, merge]` (leaves-only manifest shared with `task`). test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_chore`.
- 2.2.30 — Default manifest for `task`: `[development, pr, merge]` (leaves-only manifest shared with `chore`). test: `tests/storage/test_migration_234_backfill.py::test_default_manifest_task`.
- 2.2.31 — Backfill close-pass: any task whose post-backfill manifest is all-`done` AND `closed_at IS NULL` is closed in the same migration-234 transaction with `SET closed_at = datetime('now')`, `closed_in_session_id = 'migration:234'`. The SQL filter is `WHERE closed_at IS NULL` (canonical closure predicate; no `is_closed` column exists). After migration 234 commits, no task in the database satisfies `current_stage IS NULL AND closed_at IS NULL`. This retroactively enforces §2.1 invariant 8 for pre-epic tasks; the §3.1 `is_child_parked` predicate's `current_stage is None AND NOT child.is_closed` branch becomes unreachable in normal operation post-migration. test: `tests/storage/test_migration_234_backfill.py::test_close_pass_sets_closed_at_for_all_done_open_tasks`, `tests/storage/test_migration_234_backfill.py::test_close_pass_skips_tasks_with_no_manifest_rows`, `tests/storage/test_migration_234_backfill.py::test_close_pass_does_not_overwrite_existing_closed_at`, `tests/storage/test_migration_234_backfill.py::test_no_stranded_open_exhausted_tasks_post_migration`.

### 2.3 New gobby-tasks MCP tools for stage manifest [category: code] (depends: 2.1)

`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/tasks/_stages.py` (new — exposes both `create_stage_read_registry` and `create_stage_ops_registry` factories), `src/gobby/mcp_proxy/tools/tasks/_factory.py` (registers the stage read registry on the `gobby-tasks` server), `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py` (registers the stage ops registry on the `gobby-tasks-ops` server). Server split (round-22 F1, matching the already-shipped #13731 contract at commit `f8c323b8e`): the three READ tools (`get_task_stages`, `list_stages_registry`, `get_task_type_defaults`) live on `gobby-tasks`; the eight MUTATING tools (`start_stage`, `complete_stage`, `fail_stage`, `add_stage`, `remove_stage`, `record_pr_verdict`, `record_pr_opened`, `record_merge_result`) live on `gobby-tasks-ops`. This mirrors the existing repo convention that mutating ops tools (e.g., `create_task` on tasks-ops, `reindex_tasks` on tasks-ops) live on `gobby-tasks-ops` while read tools (e.g., `get_task`, `list_tasks`) live on `gobby-tasks`. The three review-state-axis tools (`mark_task_needs_review`, `mark_task_review_approved`, `mark_task_review_rejected`) are a separate, pre-existing surface and remain on `gobby-tasks` unchanged (see §2.3.6a regression below); they predate the ops split convention and §2.6 rewires only their behavior, not their server placement.

Add eleven new tools. Each tool has its own subsection — implementing agents see only one subsection at a time, so signatures must be repeated where they appear in dependent tools.

The three review-state-axis tools (`mark_task_needs_review`, `mark_task_review_approved`, `mark_task_review_rejected`) are NOT new in this deliverable — they already exist on the **`gobby-tasks`** server (NOT `gobby-tasks-ops`; verified via progressive discovery against the live proxy and the bundled `task-transitions` skill, which canonically calls `call_tool("gobby-tasks", "mark_task_needs_review", ...)`). The backing handler module is `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py`; registration lives in `src/gobby/mcp_proxy/tools/tasks/_factory.py` (the `gobby-tasks` registry), not `_ops_factory.py`. Phase 2.6 rewires them to first-class stage transitions (`submit_for_review`, `approve_review`, `reject_review` storage methods from §2.1) so their behavior under the new model is well-defined before Phase 3 enables manifest-driven dispatch. There are no separate stage-axis MCP tools for these transitions — the existing review tools on `gobby-tasks` are the operator/agent-facing surface and Phase 2.6 makes them first-class on the stage manifest. The server placement does NOT change in this deliverable or in §2.6: the rewired tools remain registered on `gobby-tasks` with unchanged signatures and unchanged tool names; every bundled agent allowed-tool reference (`planner.yaml`, `plan-adversary.yaml`, `expansion-qa.yaml`, `qa-reviewer.yaml`, `holistic-reviewer.yaml`, `backend-developer.yaml`, `frontend-developer.yaml`) keeps its existing `gobby-tasks:mark_task_*` reference verbatim.

| Tool | Server | Purpose |
|------|--------|---------|
| `get_task_stages(task_id)` | `gobby-tasks` | Return manifest in position order. |
| `list_stages_registry()` | `gobby-tasks` | Return all registry entries. |
| `get_task_type_defaults(task_type)` | `gobby-tasks` | Return the default manifest for a task type. |
| `start_stage(task_id, stage_name, notes?)` | `gobby-tasks-ops` | Transition `ready → in_progress`. Increments `work_attempt_count`. |
| `complete_stage(task_id, stage_name, commit_sha?, artifact_updates?, validation_override_reason?)` | `gobby-tasks-ops` | Transition to `done` per the row's `review_policy`: direct from `in_progress` for `policy ∈ {none, optional}`; from `review_approved` for `policy ∈ {required, optional}`; direct from `in_progress` on `policy=required` is REJECTED unless `validation_override_reason` is supplied. |
| `fail_stage(task_id, stage_name, reason, needs_human?)` | `gobby-tasks-ops` | Work-failure path: transitions `in_progress → ready` (no counter change), OR escalates when `work_attempt_count >= effective_max_work_attempts`. Legal source state is `in_progress` only — calling from `ready`, `needs_review`, `review_approved`, or `done` raises `IllegalStageTransitionError`. Review rejection MUST use `reject_review`. |
| `add_stage(task_id, stage_name, position)` | `gobby-tasks-ops` | Insert a row mid-manifest; mirrors `review_policy` + `reviewer_agent` from registry at insert. |
| `remove_stage(task_id, stage_name)` | `gobby-tasks-ops` | Delete a row from manifest. |
| `record_pr_verdict(task_id, verdict, findings, report_ref?)` | `gobby-tasks-ops` | Store `structured_pr_verdict` + `pr_review_report` on task_artifacts; calls `approve_review(task_id, 'pr')` for `approved` (transitions `pr.needs_review → pr.review_approved`; dispatcher's `pr_advance_rule` later runs `complete_stage`), or `reject_review(task_id, 'pr', reason=findings)` for `rejected`/`needs_changes` (transitions `pr.needs_review → pr.ready`; increments `pr.review_round_count`). Raises `IllegalStageTransitionError` if `pr` is not in `needs_review`. |
| `record_pr_opened(task_id, pr_url, github_pr_number?)` | `gobby-tasks-ops` | Persist `pr_url` and `github_pr_number` artifacts; does not change `pr` stage state. Idempotent: re-recording the same `pr_url` is a no-op. |
| `record_merge_result(task_id, merge_sha?, report_ref?, failure_reason?)` | `gobby-tasks-ops` | Persist merge outcome and advance/fail merge stage. Phase 2.3 registers the tool with stub semantics (delegates to a NotImplementedError until 4.2 wires the success/failure paths and cascade-close); 4.2 fills the body. |

Each tool delegates to `LocalTaskManager.stages_registry` or `.stage_states`. Schemas for the PR-related tools:

```python
def record_pr_verdict(
    task_id: str,
    verdict: Literal["approved", "rejected", "needs_changes"],
    findings: str,
    report_ref: str | None = None,
) -> dict[str, Any]:
    """Persist structured PR verdict on task_artifacts and advance pr stage
    via first-class review transitions per the §2.1 5-state contract.

    The pr stage carries `review_policy: required` (registry default; see §1.2);
    PR review is therefore expressed as `submit_for_review → approve_review`
    or `submit_for_review → reject_review` on the same `pr` row.

    On verdict='approved': StageStatesManager.approve_review(task_id, 'pr')
    (transitions `pr.needs_review → pr.review_approved`). The dispatcher's
    `pr_advance_rule` later picks up `pr.review_approved` and runs
    `complete_stage(task_id, 'pr')` to advance to `merge.ready`. This tool
    does NOT call `complete_stage` itself; durable `pr.review_approved` is a
    real holding state.
    On verdict='rejected' or 'needs_changes':
    StageStatesManager.reject_review(task_id, 'pr', reason=findings)
    (transitions `pr.needs_review → pr.ready` and increments
    `pr.review_round_count`). The PR worker's next pass re-enters via
    `start_stage` and re-submits.
    Stores findings in task_artifacts.pr_review_report and a JSON-encoded
    {verdict, findings, report_ref} in task_artifacts.structured_pr_verdict.
    Raises IllegalStageTransitionError if `pr` is not in `needs_review`.
    """


def record_pr_opened(
    task_id: str,
    pr_url: str,
    github_pr_number: int | None = None,
) -> dict[str, Any]:
    """Persist PR metadata on task_artifacts without changing pr stage state.

    The pr stage stays at in_progress; verdict capture happens via
    record_pr_verdict. Idempotent: re-recording the same pr_url is a no-op.
    Writes pr_url and (if provided) github_pr_number into task_artifacts.
    """
```

Block legacy lifecycle merge tools (`mark_task_pr_opened`, `mark_task_merged`, `mark_task_merge_failed`) for the duration of this epic by leaving them in place AND surfacing their usage in a deprecation logger; Phase 6 / 7 deletes them. Implementing agents must use stage-native operations only.

**Acceptance:**

- 2.3.1 — Eleven new tools registered, each with `inputSchema`, `outputSchema`, and a real handler (or, for `record_merge_result` only, a stub handler that raises `NotImplementedError("wired in Phase 4.2")` so the tool surface exists at registration time but the stage-mutation path is owned by 4.2). file: `src/gobby/mcp_proxy/tools/tasks/_stages.py`. symbol: `gobby.mcp_proxy.tools.tasks._stages`.
- 2.3.2 — Tool registration adds the eleven new stage tools split across two servers (round-22 F1, matching the already-shipped #13731 contract): the three READ tools (`get_task_stages`, `list_stages_registry`, `get_task_type_defaults`) register on the `gobby-tasks` registry via `_factory.py`'s `create_stage_read_registry` merge; the eight MUTATING tools (`start_stage`, `complete_stage`, `fail_stage`, `add_stage`, `remove_stage`, `record_pr_verdict`, `record_pr_opened`, `record_merge_result`) register on the `gobby-tasks-ops` registry via `_ops_factory.py`'s `create_stage_ops_registry` merge. Cross-server leakage is forbidden in either direction: `gobby-tasks` does NOT expose any of the 8 mutating tools; `gobby-tasks-ops` does NOT expose any of the 3 read tools. file: `src/gobby/mcp_proxy/tools/tasks/_factory.py`, `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py`. test: `tests/mcp_proxy/tools/tasks/test_stage_tools_registered.py::test_read_tools_visible_on_gobby_tasks`, `tests/mcp_proxy/tools/tasks/test_stage_tools_registered.py::test_read_tools_absent_from_gobby_tasks_ops`, `tests/mcp_proxy/tools/tasks/test_stage_tools_registered.py::test_mutating_tools_visible_on_gobby_tasks_ops`, `tests/mcp_proxy/tools/tasks/test_stage_tools_registered.py::test_mutating_tools_absent_from_gobby_tasks`.
- 2.3.3 — `record_pr_verdict` writes `structured_pr_verdict` and `pr_review_report`, then advances stage via first-class review transitions: `approved` → `approve_review('pr')` (no `complete_stage` call; durable `pr.review_approved` is the post-state); `rejected`/`needs_changes` → `reject_review('pr', reason=findings)` (no `fail_stage` call; `review_round_count` increments). test: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_calls_approve_review`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_does_not_call_complete_stage`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_post_state_is_review_approved`.
- 2.3.4 — `start_stage` errors on out-of-order start (skipping ahead). test: `tests/mcp_proxy/tools/tasks/test_stage_tools.py::test_start_stage_skipping_errors`.
- 2.3.5 — `record_pr_opened` writes `pr_url` and (when provided) `github_pr_number` to `task_artifacts` without changing `pr` stage state; re-recording the same `pr_url` is a no-op. file: `src/gobby/mcp_proxy/tools/tasks/_stages.py`. test: `tests/mcp_proxy/tools/tasks/test_record_pr_opened.py::test_idempotent`.
- 2.3.6 — `record_merge_result` is registered on `gobby-tasks-ops` with the documented signature and `inputSchema` accepting `task_id` (required), `merge_sha`, `report_ref`, `failure_reason` (all optional). The stub handler raises `NotImplementedError("wired in Phase 4.2")`; tool listing on `gobby-tasks-ops` includes the tool name and schema, and tool listing on `gobby-tasks` does NOT include it. Phase 4.2 (acceptances 4.2.2 and 4.2.3) replaces the stub with the success/failure paths and cascade-close. file: `src/gobby/mcp_proxy/tools/tasks/_stages.py`. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result_stub.py::test_registered_with_stub_on_gobby_tasks_ops`, `tests/mcp_proxy/tools/tasks/test_record_merge_result_stub.py::test_absent_from_gobby_tasks`.
- 2.3.6a — **Review-tool server-placement regression.** The three review-state-axis tools (`mark_task_needs_review`, `mark_task_review_approved`, `mark_task_review_rejected`) remain registered on the `gobby-tasks` server (NOT `gobby-tasks-ops`) with unchanged tool names and unchanged JSON-schema signatures (`task_id`, `review_notes` / `approval_notes` / `rejection_notes` + optional `round_number` for rejected) before AND after the Phase 2.6 rewire. The test queries the `gobby-tasks` and `gobby-tasks-ops` registries and asserts: (a) `gobby-tasks` exposes all three tools by name, (b) `gobby-tasks-ops` does NOT expose any of them, (c) the JSON `inputSchema` field set is unchanged from the pre-rewire baseline, (d) every bundled agent allowed-tool reference in `src/gobby/install/shared/workflows/agents/*.yaml` (covering planner, plan-adversary, expansion-qa, qa-reviewer, holistic-reviewer, backend-developer, frontend-developer) still names `gobby-tasks:mark_task_*` verbatim and zero references name `gobby-tasks-ops:mark_task_*`. file: `src/gobby/mcp_proxy/tools/tasks/_factory.py`, `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py`. test: `tests/mcp_proxy/tools/tasks/test_review_tools_server_placement.py::test_review_tools_on_gobby_tasks_only`, `tests/mcp_proxy/tools/tasks/test_review_tools_server_placement.py::test_review_tools_absent_from_gobby_tasks_ops`, `tests/mcp_proxy/tools/tasks/test_review_tools_server_placement.py::test_review_tool_signatures_unchanged_post_rewire`, `tests/mcp_proxy/tools/tasks/test_review_tools_server_placement.py::test_bundled_agents_reference_gobby_tasks_review_tools`.
- 2.3.6b — **Stage-lifecycle-tool server-placement regression (round-22 F1).** The eleven stage tools split across two servers per the §2.3 contract that matches already-shipped #13731 (commit `f8c323b8e`): the three READ tools (`get_task_stages`, `list_stages_registry`, `get_task_type_defaults`) live on `gobby-tasks`; the eight MUTATING tools (`start_stage`, `complete_stage`, `fail_stage`, `add_stage`, `remove_stage`, `record_pr_verdict`, `record_pr_opened`, `record_merge_result`) live on `gobby-tasks-ops`. The test queries the `gobby-tasks` and `gobby-tasks-ops` registries and asserts: (a) `gobby-tasks` exposes exactly the three read tools by name with the documented `inputSchema` and exposes ZERO of the eight mutating tools; (b) `gobby-tasks-ops` exposes exactly the eight mutating tools by name with the documented `inputSchema` and exposes ZERO of the three read tools; (c) every bundled agent allowed-tool reference in `src/gobby/install/shared/workflows/agents/*.yaml` that names a read tool uses `gobby-tasks:` and every reference that names a mutating tool uses `gobby-tasks-ops:` — cross-references in either direction fail the test; (d) the strategy plan's `### MCP / HTTP / CLI surfaces` section names each tool under the matching server header (read tools under "`gobby-tasks` MCP tools", mutating tools under "`gobby-tasks-ops` MCP tools"); (e) the §2.3 tool table in this implementation plan agrees with the strategy plan and with the runtime registries. file: `src/gobby/mcp_proxy/tools/tasks/_factory.py`, `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py`. test: `tests/mcp_proxy/tools/tasks/test_stage_tools_server_placement.py::test_read_tools_only_on_gobby_tasks`, `tests/mcp_proxy/tools/tasks/test_stage_tools_server_placement.py::test_mutating_tools_only_on_gobby_tasks_ops`, `tests/mcp_proxy/tools/tasks/test_stage_tools_server_placement.py::test_no_cross_server_leakage_in_either_direction`, `tests/mcp_proxy/tools/tasks/test_stage_tools_server_placement.py::test_bundled_agents_route_read_tools_to_gobby_tasks`, `tests/mcp_proxy/tools/tasks/test_stage_tools_server_placement.py::test_bundled_agents_route_mutating_tools_to_gobby_tasks_ops`, `tests/mcp_proxy/tools/tasks/test_stage_tools_server_placement.py::test_strategy_plan_and_implementation_plan_agree_on_split`.

Per-tool registration coverage (one acceptance per data row of the §2.3 tool table, per the plan-coverage contract's table-row decomposition rule):

- 2.3.7 — `get_task_stages(task_id)` on `gobby-tasks` returns the manifest in position order; output schema declares `stages: list[StageStateView]`. test: `tests/mcp_proxy/tools/tasks/test_get_task_stages.py::test_returns_position_order`.
- 2.3.8 — `list_stages_registry()` on `gobby-tasks` returns all 11 registry entries (each carrying `review_policy` and `reviewer_agent`); output schema declares `entries: list[StageRegistryEntry]`. The dropped stages (`adversarial_review`, `expansion_qa`, `code_review_qa`) are not present. test: `tests/mcp_proxy/tools/tasks/test_list_stages_registry.py::test_returns_all_11`, `tests/mcp_proxy/tools/tasks/test_list_stages_registry.py::test_omits_dropped_review_stages`.
- 2.3.9 — `get_task_type_defaults(task_type)` on `gobby-tasks` returns the default manifest for a known type; errors for an unknown type. test: `tests/mcp_proxy/tools/tasks/test_get_task_type_defaults.py::test_known_and_unknown_types`.
- 2.3.10 — `start_stage(task_id, stage_name, notes?)` on `gobby-tasks-ops` transitions `ready → in_progress`; increments `work_attempt_count`; emits a `task_lifecycle_events` row. test: `tests/mcp_proxy/tools/tasks/test_start_stage.py::test_transitions_ready_to_in_progress`, `tests/mcp_proxy/tools/tasks/test_start_stage.py::test_increments_work_attempt_count`.
- 2.3.11 — `complete_stage(task_id, stage_name, commit_sha?, artifact_updates?, validation_override_reason?)` on `gobby-tasks-ops` transitions to `done` per the row's `review_policy`: direct from `in_progress` for `policy ∈ {none, optional}`; from `review_approved` for `policy ∈ {required, optional}`; direct from `in_progress` on `policy=required` raises `IllegalStageTransitionError` unless `validation_override_reason` is supplied. Persists `commit_sha` and merges `artifact_updates`. test: `tests/mcp_proxy/tools/tasks/test_complete_stage.py::test_policy_none_direct_complete`, `tests/mcp_proxy/tools/tasks/test_complete_stage.py::test_policy_required_complete_from_review_approved`, `tests/mcp_proxy/tools/tasks/test_complete_stage.py::test_policy_required_direct_complete_rejected`, `tests/mcp_proxy/tools/tasks/test_complete_stage.py::test_validation_override_allows_direct_complete_on_required`.
- 2.3.12 — `fail_stage(task_id, stage_name, reason, needs_human?)` on `gobby-tasks-ops` is the work-failure path with `in_progress` as the only legal source state. Under-cap: transitions `in_progress → ready` with no counter change when `work_attempt_count < effective_max_work_attempts`. Over-cap: escalates with `reason=f'<stage>:max'` when `work_attempt_count >= effective_max_work_attempts` per acceptance 2.1.7. Calling `fail_stage` from any other source state raises `IllegalStageTransitionError(stage_name, current_state, 'fail_stage', review_policy)`. Review rejection paths must use `reject_review` exclusively. test: `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_under_cap_returns_to_ready`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_over_cap_escalates`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_illegal_from_ready_policy_none`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_illegal_from_ready_policy_required`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_illegal_from_ready_policy_optional`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_illegal_from_needs_review_policy_required`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_illegal_from_needs_review_policy_optional`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_illegal_from_review_approved_policy_required`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_illegal_from_review_approved_policy_optional`, `tests/mcp_proxy/tools/tasks/test_fail_stage.py::test_illegal_from_done_terminal`.
- 2.3.13 — `add_stage(task_id, stage_name, position)` on `gobby-tasks-ops` inserts a future `ready` row at the requested position (must be strictly greater than current stage's position); reorders affected positions to remain dense. Surfaces the typed `IllegalManifestMutationError` from §2.1 invariant 9 as a tool error: position at or before current → `position_at_or_before_current`; row already present → `stage_already_in_manifest`; manifest exhausted → `manifest_exhausted`. The MCP error response includes the full seven-tuple payload `(task_id, target_stage_name, target_position, current_stage_name, current_stage_state, mutation, reason)` so callers can recover deterministically. test: `tests/mcp_proxy/tools/tasks/test_add_stage.py::test_insert_mid_manifest_reorders`, `tests/mcp_proxy/tools/tasks/test_add_stage.py::test_insert_at_or_before_current_returns_typed_error`, `tests/mcp_proxy/tools/tasks/test_add_stage.py::test_insert_existing_stage_returns_typed_error`, `tests/mcp_proxy/tools/tasks/test_add_stage.py::test_insert_on_exhausted_manifest_returns_typed_error`, `tests/mcp_proxy/tools/tasks/test_add_stage.py::test_error_payload_carries_full_seven_tuple`.
- 2.3.14 — `remove_stage(task_id, stage_name)` on `gobby-tasks-ops` deletes a future `ready` row (must be strictly past the current stage's position and have state `ready`); reorders positions to remain dense `range(N)` (0-indexed and contiguous, `0..N-1` after the removal). Surfaces the typed `IllegalManifestMutationError` from §2.1 invariant 9 for every disallowed case in the legality matrix: row at or before current → `position_at_or_before_current`; in-flight row (`in_progress`/`needs_review`/`review_approved`) → `current_row_not_removable`; `done` row → `done_row_not_removable`; missing row → `stage_not_in_manifest`; manifest exhausted → `manifest_exhausted`; only-remaining future row → `would_exhaust_terminal_position`. The MCP error response carries the full seven-tuple payload. test: `tests/mcp_proxy/tools/tasks/test_remove_stage.py::test_remove_reorders_dense`, `tests/mcp_proxy/tools/tasks/test_remove_stage.py::test_remove_at_or_before_current_returns_typed_error`, `tests/mcp_proxy/tools/tasks/test_remove_stage.py::test_remove_in_progress_returns_typed_error`, `tests/mcp_proxy/tools/tasks/test_remove_stage.py::test_remove_done_returns_typed_error`, `tests/mcp_proxy/tools/tasks/test_remove_stage.py::test_remove_missing_stage_returns_typed_error`, `tests/mcp_proxy/tools/tasks/test_remove_stage.py::test_remove_last_future_row_returns_would_exhaust_error`, `tests/mcp_proxy/tools/tasks/test_remove_stage.py::test_error_payload_carries_full_seven_tuple`.
- 2.3.15 — `record_pr_verdict(task_id, verdict, findings, report_ref?)` on `gobby-tasks-ops` writes `structured_pr_verdict` (JSON) and `pr_review_report`; advances `pr` stage via first-class review transitions per §2.1 contract: `approved` → `StageStatesManager.approve_review(task_id, 'pr')` (transitions `pr.needs_review → pr.review_approved`; does NOT call `complete_stage`; the dispatcher's `pr_advance_rule` later runs `complete_stage` after the operator/system gates the merge); `rejected`/`needs_changes` → `StageStatesManager.reject_review(task_id, 'pr', reason=findings)` (transitions `pr.needs_review → pr.ready`; increments `pr.review_round_count`; does NOT call `fail_stage`). Raises `IllegalStageTransitionError` if `pr` is not in `needs_review`. test: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_calls_approve_review_only`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_does_not_advance_to_merge`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_increments_no_counter`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_rejected_calls_reject_review_only`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_rejected_increments_review_round_count`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_needs_changes_treated_as_rejected`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_raises_when_pr_not_in_needs_review`.
- 2.3.16 — `record_pr_opened(task_id, pr_url, github_pr_number?)` on `gobby-tasks-ops` persists artifacts without changing `pr` stage state; idempotent on repeated `pr_url`. test: `tests/mcp_proxy/tools/tasks/test_record_pr_opened.py::test_no_stage_change_idempotent`.
- 2.3.17 — `record_merge_result(task_id, merge_sha?, report_ref?, failure_reason?)` on `gobby-tasks-ops` is registered as a stub in Phase 2.3 (raises `NotImplementedError`); Phase 4.2 supplies the success and failure paths. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result_stub.py::test_stub_raises_notimplemented`.

### 2.4 New HTTP routes for stage manifest [category: code] (depends: 2.1)

`kind: deliverable`

Target: `src/gobby/servers/routes/tasks.py`, `src/gobby/servers/routes/stages.py` (new), `src/gobby/servers/routes/__init__.py` (existing — re-exports `create_tasks_router` at lines 31, 58; extended to re-export `create_stages_router`), `src/gobby/servers/app_factory.py` (existing — `_register_routes` at line 494, `app.include_router(create_tasks_router(server))` at line 541; extended to import and mount `create_stages_router(server)` so the new `/api/stages/registry` and `/api/task-types/{type}/default-stages` endpoints are reachable)

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
    action: Literal["start", "submit_for_review", "approve_review", "reject_review", "complete", "fail", "add", "remove"]
    notes: str | None = None
    reason: str | None = None  # required for action='fail' or 'reject_review'
    needs_human: bool = False
    commit_sha: str | None = None
    artifact_updates: dict[str, str] | None = None
    validation_override_reason: str | None = None  # required for direct in_progress→done on policy=required rows
    position: int | None = None  # required for action='add'
```

The list endpoint's `stage` and `stage_state` filters JOIN `tasks` to `task_stage_states` and filter `WHERE task_stage_states.stage_name = :stage [AND task_stage_states.state = :stage_state]`; `stage_state` accepts any of the 5 values. The response gains an optional `stages` field per task containing the denormalized manifest with `review_policy`, `reviewer_agent`, `work_attempt_count`, `review_round_count` per row (a single SQL query that LEFT JOINs and groups; no N+1). PATCH actions map: `start → start_stage`, `submit_for_review → submit_for_review`, `approve_review → approve_review`, `reject_review → reject_review`, `complete → complete_stage`, `fail → fail_stage`, `add → add_stage`, `remove → remove_stage`. Each transition action raises `IllegalStageTransitionError` on policy/state violations; the route maps the typed error to a 422 response with the `(stage_name, current_state, attempted_transition, review_policy)` payload. Each structural mutation action (`add`, `remove`) raises `IllegalManifestMutationError` on legality-matrix violations (per §2.1 invariant 9); the route maps that error to a 422 response with the `(task_id, target_stage_name, target_position, current_stage_name, current_stage_state, mutation, reason)` payload, distinguished from transition errors by `error: "illegal_manifest_mutation"` versus `error: "illegal_stage_transition"`.

`TaskListResponse.tasks[i].stages: list[StageStateView]` is added; existing fields stay backward compatible. Existing `?status=...` and `?lifecycle=...` params remain functional through Phase 5 (they're consumed by the legacy projection helpers); Phase 5 removes them.

**Acceptance:**

- 2.4.1 — Five new endpoints registered with declared paths, methods, and schemas. file: `src/gobby/servers/routes/stages.py`, `src/gobby/servers/routes/tasks.py`. test: `tests/servers/routes/test_stage_routes.py::test_routes_registered`.
- 2.4.2 — `PATCH /api/tasks/{id}/stages/{name}` action='start' moves the row to `in_progress`. test: `tests/servers/routes/test_stage_routes.py::test_patch_start_stage`.
- 2.4.3 — `GET /api/tasks?stage=development&stage_state=in_progress` returns only tasks with that exact `(stage_name, state)` row; `stage_state` accepts any of the 5 enum values (`ready`, `in_progress`, `needs_review`, `review_approved`, `done`). test: `tests/servers/routes/test_stage_routes.py::test_list_filter_by_stage_state`, parameterized over the 5 state values in `tests/servers/routes/test_stage_routes.py::test_list_filter_5_state_values`.
- 2.4.6 — PATCH `/api/tasks/{id}/stages/{name}` returns 422 with the typed `IllegalStageTransitionError` payload when an action violates the row's `review_policy` (e.g., `submit_for_review` on `policy=none`, `approve_review` from a non-`needs_review` state, `complete` direct on `policy=required` without `validation_override_reason`). The 422 body carries `{"error": "illegal_stage_transition", "stage_name": ..., "current_state": ..., "attempted_transition": ..., "review_policy": ...}`. test: `tests/servers/routes/test_stage_routes.py::test_patch_illegal_transition_returns_422_with_payload`, parameterized over the legality matrix.
- 2.4.7 — PATCH `/api/tasks/{id}/stages/{name}` with `action='add'` or `action='remove'` returns 422 with the typed `IllegalManifestMutationError` payload when a structural mutation violates the §2.1 invariant 9 legality matrix. The 422 body carries `{"error": "illegal_manifest_mutation", "task_id": ..., "target_stage_name": ..., "target_position": ..., "current_stage_name": ..., "current_stage_state": ..., "mutation": ..., "reason": ...}`, distinguished from transition errors by the `error` discriminator. Coverage spans every non-`stage_already_in_manifest`/`stage_not_in_manifest` reason: `position_at_or_before_current`, `current_row_not_removable`, `done_row_not_removable`, `would_exhaust_terminal_position`, `manifest_exhausted`. test: `tests/servers/routes/test_stage_routes.py::test_patch_add_at_current_position_returns_422`, `tests/servers/routes/test_stage_routes.py::test_patch_remove_in_progress_returns_422`, `tests/servers/routes/test_stage_routes.py::test_patch_remove_done_returns_422`, `tests/servers/routes/test_stage_routes.py::test_patch_add_existing_stage_returns_422`, `tests/servers/routes/test_stage_routes.py::test_patch_remove_missing_stage_returns_422`, `tests/servers/routes/test_stage_routes.py::test_patch_remove_last_future_row_returns_would_exhaust_422`, `tests/servers/routes/test_stage_routes.py::test_patch_mutation_on_exhausted_manifest_returns_422`, `tests/servers/routes/test_stage_routes.py::test_patch_422_payload_uses_illegal_manifest_mutation_discriminator`.
- 2.4.4 — Denormalized `stages` field returned on each task in the list response when stage filters are active or when an explicit `?include_stages=1` flag is set. test: `tests/servers/routes/test_stage_routes.py::test_list_includes_denormalized_manifest`.
- 2.4.5 — `task_event` WebSocket events fire on every stage state transition. behavior: "broadcaster emits stage_changed event" verified in `tests/servers/websocket/test_stage_broadcast.py::test_stage_transition_broadcasts`.

### 2.5 New CLI commands and build flags [category: code] (depends: 2.1, 2.3)

`kind: deliverable`

Target: `src/gobby/cli/tasks/stages.py` (new), `src/gobby/cli/tasks/review.py` (new), `src/gobby/cli/tasks/_stage_filters.py` (new helper for `--stage` / `--state` flag plumbing into `gobby tasks list`), `src/gobby/cli/tasks/__init__.py` (Click group registration; wires the new modules into the existing `gobby tasks` group), `src/gobby/cli/tasks/crud.py` (touched ONLY to extend the existing `list` command's option set with `--stage` / `--state` via the new helper module — no new subcommands added here), `src/gobby/cli/tasks/_utils.py` (small shared parsers if needed; do not grow), `src/gobby/cli/build.py` (or wherever `gobby build` lives — extends with the per-stage cap flags but keeps the file under the limit), `src/gobby/mcp_proxy/tools/build.py` (round-19 F3: existing MCP `build_task` tool currently declares `max_review_rounds: int = 3`, `max_expansion_attempts: int | None = None`, `max_qa_rounds: int | None = None`, `max_merge_attempts: int | None = None`, `max_holistic_rounds: int | None = None` as Python kwargs at lines 44–48 and as inputSchema properties at lines 123–127, and forwards them at lines 73–77; the five flat cap kwargs and inputSchema properties are removed and replaced by a `stage_caps: list[dict] | None = None` kwarg whose entries carry `{stage_name, max_work_attempts?, max_review_rounds?}` matching the CLI's `--stage <name>:max_review_rounds=N` resolution, forwarded into the same `BuildOptions`/`StageManifestSpec` shape the CLI builds), `src/gobby/servers/routes/build.py` (round-19 F3: existing HTTP `BuildOptions` Pydantic model carries `max_review_rounds: int = 3`, `max_expansion_attempts: int | None = None`, `max_qa_rounds: int | None = None`, `max_merge_attempts: int | None = None`, `max_holistic_rounds: int | None = None` at lines 29–33; the five flat fields are removed and replaced by a typed `stage_caps: list[StageCapOverride] | None = None` field (`StageCapOverride` = Pydantic model with `stage_name: str`, `max_work_attempts: int | None`, `max_review_rounds: int | None`); the HTTP route forwards into the same shared build service entry point as the CLI/MCP surfaces so all three resolve to the same `list[StageManifestSpec]`), `src/gobby/config/build.py` (round-19 F3: existing daemon-config `BuildConfig` carries `max_review_rounds: int = 3` at line 73 with `default_max_review_rounds()` accessor at lines 82–84, plus `max_expansion_attempts`, `max_qa_rounds`, `max_merge_attempts`, `max_holistic_rounds` at lines 130–134, and the YAML normalizer at lines 162, 201–217 reads/translates the same flat field set; the five flat config fields and the `default_max_review_rounds` accessor are removed; the registry's per-stage default caps (from §1.1's `task_stages_registry.default_max_*` columns and the bundled `stages.yaml`) become the single source of truth for default caps, with daemon config carrying only an optional `stage_caps` overrides map keyed by stage name. The YAML normalizer migrates pre-cutover configs by translating each removed flat field into the matching `stage_caps` entry per the §1.1 authoritative `task_artifacts.max_*` → `task_stage_states.max_<work|review>` mapping (`max_expansion_attempts` → `expansion.max_work_attempts`, `max_qa_rounds` → `development.max_review_rounds`, `max_merge_attempts` → `merge.max_work_attempts`, `max_holistic_rounds` → `holistic_qa.max_review_rounds`, `max_review_rounds` → `pr.max_review_rounds`), with a deprecation log line per migrated field. The new schema is enforced before migration 236 lands so MCP/HTTP/CLI/config never reference dropped columns).

**Monolith-rule constraint (CLAUDE.md guiding principle 2 — no files over 1,000 lines).** The target inventory above intentionally avoids `crud.py`'s body for new subcommands. As of plan time, `src/gobby/cli/tasks/crud.py` is 904 lines (`wc -l` verified); adding three new Click groups (`stages`, `advance`, `review`) plus per-command option parsing inside it would push it past the 1,000-line ceiling. Instead, each new subcommand lives in a focused module:

- `gobby tasks stages` and `gobby tasks advance` → `src/gobby/cli/tasks/stages.py` (one file owning both stage-listing and advance commands; expected size ~250-350 lines).
- `gobby tasks review --submit | --approve | --reject` → `src/gobby/cli/tasks/review.py` (one file owning the three review-axis transitions; expected size ~150-200 lines).
- `gobby tasks list --stage <name> --state <state>` filter plumbing → `src/gobby/cli/tasks/_stage_filters.py` (small helper module with the option callbacks and HTTP-query construction; expected size ~80-120 lines). Imported by `crud.py`'s existing `list` command without adding a new subcommand body to it.

The Click registration entry point in `src/gobby/cli/tasks/__init__.py` imports the new modules and attaches them to the existing `tasks` group. The existing `crud.py` remains under the limit (added net diff is one import line plus two added options on the `list` command) and gains no new subcommands. `gobby build` flag additions are confined to the existing build module under its own line budget.

Add new `gobby tasks` subcommands and extend `gobby build` and `gobby tasks list`.

```text
gobby tasks stages <task_ref>                    # render manifest table
gobby tasks advance <task_ref> [--stage <name>]  # complete current stage per policy; auto-start next
gobby tasks review <task_ref> --submit | --approve | --reject [--reason <text>]
                                                  # stage-axis review transitions (calls submit_for_review/approve_review/reject_review)
gobby tasks list --stage <name> [--state <state>]
gobby build <ref> --stages <a,b,c>               # explicit manifest
gobby build <ref> --add-stage <name>[@<position>]
gobby build <ref> --skip-stage <name>            # opt-out of a default-manifest stage
gobby build <ref> --stage <name>:max_review_rounds=N
                                                  # per-stage cap override (also: max_work_attempts=N)
```

`gobby tasks list` currently has `--status` and `--lifecycle` options (`src/gobby/cli/tasks/crud.py`). Add `--stage` and `--state` flags (`--state` accepts any of the 5 values); they call the new HTTP endpoint with the new filter params. Keep `--status` and `--lifecycle` working through Phase 5; Phase 6 removes them.

`gobby tasks advance`: if `--stage` is omitted, advance the current stage; if specified, validate it equals the current stage's name (else error). The advance respects the row's `review_policy`: `policy=none|optional` advances `in_progress → done` directly; `policy=required` advances `review_approved → done` (and errors with the `IllegalStageTransitionError` payload if the row is at `in_progress`, suggesting `gobby tasks review --submit` first). On success, automatically advance the next manifest row to `in_progress` if it's eligible (no human gate, no agent missing). This is the CLI counterpart of Phase 3's dispatcher behavior.

`gobby tasks review`: stage-axis review transitions for operators. `--submit` calls `submit_for_review` (`in_progress → needs_review`); `--approve` calls `approve_review` (`needs_review → review_approved`); `--reject --reason "..."` calls `reject_review` (`needs_review → ready`, increments `review_round_count`). All three error with the `IllegalStageTransitionError` payload on policy/state violations. The CLI translates the typed error to a non-zero exit code with a clear stderr message naming the constraint.

`gobby build` flag resolution order: `--stages` (explicit list, replaces default); else type defaults + `--add-stage` insertions + `--skip-stage` removals + per-stage cap overrides via `--stage <name>:max_review_rounds=N` / `--stage <name>:max_work_attempts=N` (mirrored onto the manifest row at init time, overriding the registry default). The build service builds a `list[StageManifestSpec]` from the resolved flags (each spec carrying `stage_name`, `position`, and optional `max_work_attempts` / `max_review_rounds`) and passes the list to `StageStatesManager.initialize_manifest(task_id, specs, ...)` so the per-row caps land in the same transaction as the manifest rows themselves — no follow-up UPDATE is needed. Profiles (`quick`, `full`, `full-yolo`) become named bundles of `--skip-stage` arguments resolved at build time.

CLI output for `gobby tasks stages`:

```text
$ gobby tasks stages #13482
#13482  Lifecycle + status enum alignment for kanban visibility
Stage         State            Policy    Work  Review  Updated
────────────  ───────────────  ────────  ────  ──────  ──────────
planning      done             required  3     2       2026-04-30
test_arch     done             none      1     —       2026-04-30
expansion     review_approved  required  1     0       2026-04-30
development   in_progress      required  2     0       2026-04-30
…
```

**Acceptance:**

- 2.5.1 — `gobby tasks stages` Click command renders the manifest table sorted by position; columns include stage name, state, review_policy, work_attempts, review_rounds, updated_at. The command is implemented in the new focused module `src/gobby/cli/tasks/stages.py` (NOT in `crud.py`, which is already at 904 lines and would breach the 1,000-line monolith rule); the Click group is wired into `src/gobby/cli/tasks/__init__.py`. file: `src/gobby/cli/tasks/stages.py`. test: `tests/cli/test_tasks_stages_command.py::test_renders_manifest_with_policy_columns`, `tests/cli/test_tasks_stages_command.py::test_command_lives_in_stages_module_not_crud`.
- 2.5.2 — `gobby tasks advance` advances the current stage per its `review_policy` and auto-starts the next when eligible. On `policy=required` rows at `in_progress`, the command errors with a clear stderr message suggesting `gobby tasks review --submit` first. test: `tests/cli/test_tasks_advance_command.py::test_auto_advance_next_stage_policy_none`, `tests/cli/test_tasks_advance_command.py::test_advance_required_policy_from_review_approved`, `tests/cli/test_tasks_advance_command.py::test_advance_required_from_in_progress_errors`.
- 2.5.2a — `gobby tasks review --submit | --approve | --reject` calls the corresponding stage-axis transitions; `--reject` requires `--reason`. Errors from `IllegalStageTransitionError` are translated to non-zero exit with stderr naming the constraint. test: `tests/cli/test_tasks_review_command.py::test_submit_advances_to_needs_review`, `tests/cli/test_tasks_review_command.py::test_approve_advances_to_review_approved`, `tests/cli/test_tasks_review_command.py::test_reject_returns_to_ready_increments_review_rounds`, `tests/cli/test_tasks_review_command.py::test_reject_requires_reason`, `tests/cli/test_tasks_review_command.py::test_review_on_policy_none_errors_with_payload`.
- 2.5.3 — `gobby tasks list --stage development --state in_progress` filters to that exact `(stage_name, state)` row; `--state` accepts any of the 5 values. test: `tests/cli/test_tasks_list_stage_filter.py::test_stage_state_filter`, parameterized over the 5 state values.
- 2.5.4 — `gobby build <ref> --stages a,b,c` writes exactly that manifest; `--add-stage` inserts at requested position; `--skip-stage` omits a default stage; `--stage <name>:max_review_rounds=N` / `--stage <name>:max_work_attempts=N` mirror the per-stage caps onto the manifest row at init time. Build resolution constructs `list[StageManifestSpec]` (per §2.1) carrying name, position, and optional cap overrides; `StageStatesManager.initialize_manifest` persists the spec into the row's `max_work_attempts` / `max_review_rounds` columns inside the same DB transaction as the row insert. test: `tests/cli/test_build_stage_flags.py::test_build_flag_resolution`, `tests/cli/test_build_stage_flags.py::test_per_stage_cap_overrides_persisted_on_state_row`, `tests/cli/test_build_stage_flags.py::test_build_constructs_StageManifestSpec_with_caps`, `tests/cli/test_build_stage_flags.py::test_initialize_manifest_persists_caps_atomically`.
- 2.5.4a — **(round-19 F3)** Build cap-flag schema parity across CLI/MCP/HTTP/config. The five legacy flat cap fields (`max_expansion_attempts`, `max_qa_rounds`, `max_merge_attempts`, `max_holistic_rounds`, `max_review_rounds`) are removed from every build ingress surface and replaced by the per-stage `stage_caps` shape introduced in 2.5.4. (a) MCP `build_task` (`src/gobby/mcp_proxy/tools/build.py`) — the five flat kwargs and inputSchema properties are removed; the new `stage_caps` array property carries `{stage_name, max_work_attempts?, max_review_rounds?}` items; tool forwards into the same shared build service entry point as the CLI. (b) HTTP `BuildOptions` (`src/gobby/servers/routes/build.py`) — the five flat Pydantic fields are removed; new typed `stage_caps: list[StageCapOverride]` field carries the same shape; route forwards into the same shared build service entry point. (c) Daemon config `BuildConfig` (`src/gobby/config/build.py`) — the five flat fields and the `default_max_review_rounds` accessor are removed; replaced by an optional `stage_caps` overrides map keyed by stage name; default caps come from the `task_stages_registry.default_max_*` columns (per §1.1). (d) Config YAML normalizer (`src/gobby/config/build.py` lines 162, 201–217) translates pre-cutover flat fields into `stage_caps` entries per the §1.1 mapping (`max_expansion_attempts → expansion.max_work_attempts`, `max_qa_rounds → development.max_review_rounds`, `max_merge_attempts → merge.max_work_attempts`, `max_holistic_rounds → holistic_qa.max_review_rounds`, `max_review_rounds → pr.max_review_rounds`), emitting a deprecation log line per migrated field. (e) All three ingress surfaces (CLI, MCP, HTTP) resolve to the SAME `list[StageManifestSpec]` for the same input — proven by parameterized tests that drive each surface with equivalent stage_caps inputs and assert byte-for-byte equal `StageManifestSpec` lists. (f) Pre-§5.3 invariant: the cap-flag schema migration lands BEFORE migration 236 drops `task_artifacts.max_*`, so no ingress surface can write to a dropped column. The §5.3.11 runtime-reader audit (which scopes `src/gobby/mcp_proxy/`, `src/gobby/servers/`, `src/gobby/cli/`, `src/gobby/config/`) returns zero matches for the five legacy names after this deliverable lands. file: `src/gobby/mcp_proxy/tools/build.py`, `src/gobby/servers/routes/build.py`, `src/gobby/config/build.py`. test: `tests/mcp_proxy/tools/test_build_stage_caps.py::test_inputschema_excludes_max_expansion_attempts`, `tests/mcp_proxy/tools/test_build_stage_caps.py::test_inputschema_excludes_max_qa_rounds`, `tests/mcp_proxy/tools/test_build_stage_caps.py::test_inputschema_excludes_max_merge_attempts`, `tests/mcp_proxy/tools/test_build_stage_caps.py::test_inputschema_excludes_max_holistic_rounds`, `tests/mcp_proxy/tools/test_build_stage_caps.py::test_inputschema_excludes_max_review_rounds`, `tests/mcp_proxy/tools/test_build_stage_caps.py::test_stage_caps_array_property_present`, `tests/mcp_proxy/tools/test_build_stage_caps.py::test_build_task_forwards_to_shared_service`, `tests/servers/routes/test_build_route_stage_caps.py::test_buildoptions_excludes_legacy_flat_fields`, `tests/servers/routes/test_build_route_stage_caps.py::test_buildoptions_carries_typed_stagecapoverride_list`, `tests/servers/routes/test_build_route_stage_caps.py::test_route_forwards_to_shared_service`, `tests/config/test_build_config_stage_caps.py::test_buildconfig_excludes_max_review_rounds_field`, `tests/config/test_build_config_stage_caps.py::test_buildconfig_excludes_default_max_review_rounds_accessor`, `tests/config/test_build_config_stage_caps.py::test_buildconfig_carries_optional_stage_caps_map`, `tests/config/test_build_config_stage_caps.py::test_yaml_normalizer_translates_max_expansion_attempts_to_stage_caps`, `tests/config/test_build_config_stage_caps.py::test_yaml_normalizer_translates_max_qa_rounds_to_stage_caps`, `tests/config/test_build_config_stage_caps.py::test_yaml_normalizer_translates_max_merge_attempts_to_stage_caps`, `tests/config/test_build_config_stage_caps.py::test_yaml_normalizer_translates_max_holistic_rounds_to_stage_caps`, `tests/config/test_build_config_stage_caps.py::test_yaml_normalizer_translates_max_review_rounds_to_stage_caps`, `tests/config/test_build_config_stage_caps.py::test_yaml_normalizer_emits_deprecation_log_per_field`, `tests/config/test_build_config_stage_caps.py::test_yaml_normalizer_never_emits_qa_or_review_stage_names` (negative assertion per round-20 F1 — `stage_name` for any translated legacy cap is never one of `{'qa', 'review'}`; the §1.1 authoritative destinations resolve to registry stages only: `expansion`, `development`, `merge`, `holistic_qa`, `pr`), `tests/build/test_ingress_surface_parity.py::test_cli_mcp_http_resolve_to_same_StageManifestSpec_list_with_stage_caps`, parameterized over the 5 legacy field names and their per-stage destinations.
- 2.5.5 — Monolith-rule compliance: every source file touched by §2.5 (`src/gobby/cli/tasks/stages.py`, `src/gobby/cli/tasks/review.py`, `src/gobby/cli/tasks/_stage_filters.py`, `src/gobby/cli/tasks/__init__.py`, `src/gobby/cli/tasks/crud.py`, `src/gobby/cli/tasks/_utils.py`, `src/gobby/cli/build.py`) finishes the deliverable at strictly less than 1,000 lines per file (CLAUDE.md guiding principle 2). The new `stages.py`, `review.py`, and `_stage_filters.py` modules are the destination for net-new subcommand bodies; `crud.py`'s diff is restricted to attaching `--stage` / `--state` filter options to the existing `list` command via the helper module (no new subcommand bodies, no large blocks). If a `wc -l` check on any touched file would breach 1,000 lines, the deliverable MUST split further or open a refactor task under epic #12730 (active monolith-refactor backlog) BEFORE landing — refactor-task-first is the only sanctioned path past the line ceiling. test: `tests/cli/test_stage_cli_monolith_compliance.py::test_all_touched_files_under_1000_lines` (running `wc -l` against each path in the §2.5 target list and asserting `< 1000`), `tests/cli/test_stage_cli_monolith_compliance.py::test_stages_module_owns_stages_and_advance_commands` (asserts `gobby tasks stages` and `gobby tasks advance` Click bodies live in `stages.py`, not `crud.py`), `tests/cli/test_stage_cli_monolith_compliance.py::test_review_module_owns_review_command` (asserts `gobby tasks review` Click body lives in `review.py`, not `crud.py`).

### 2.6 Rewire `mark_task_review_*` tools to first-class stage-axis transitions [category: code] (depends: 2.1, 2.3)

`kind: deliverable`

Target: `src/gobby/storage/tasks/_transitions.py`, `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py`, `src/gobby/install/shared/workflows/agents/test-architect.yaml` (existing — full end-to-end rewrite per acceptance 2.6.6 paragraph (a)–(d): instructions/status_message rewritten to call `complete_stage`, `on_mcp_success` hook re-keyed from `mark_task_review_approved` to `complete_stage`, `mark_task_review_rejected` block dropped from `blocked_mcp_tools`, allowed-tools unchanged), `src/gobby/install/shared/workflows/agents/planner.yaml` (existing — instruction prose validated to call `mark_task_needs_review` only when current stage is `planning`; rewritten if stage context is unclear), `src/gobby/install/shared/workflows/agents/plan-adversary.yaml` (existing — instruction prose validated to call `mark_task_review_approved` / `_rejected` only against `planning.needs_review`), `src/gobby/install/shared/workflows/agents/expansion-qa.yaml` (existing — instruction prose validated against `expansion.needs_review`), `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml` (existing — instruction prose validated against `development.needs_review`), `src/gobby/install/shared/workflows/agents/holistic-reviewer.yaml` (existing — instruction prose validated against `holistic_qa` internal review loop), `src/gobby/install/shared/workflows/agents/backend-developer.yaml` (existing — instruction prose validated to call `mark_task_needs_review` only when current stage is `development`), `src/gobby/install/shared/workflows/agents/frontend-developer.yaml` (existing — same as backend-developer), `src/gobby/install/shared/workflows/agents/requirements-analyst.yaml`, `src/gobby/install/shared/workflows/agents/qa-dev.yaml`, `src/gobby/install/shared/workflows/agents/nightly-linter.yaml`, `src/gobby/install/shared/workflows/agents/nightly-test-fixer.yaml`, `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml`, `src/gobby/install/shared/workflows/agents/default.yaml`, `src/gobby/install/shared/workflows/agents/developer.yaml` (existing — case-by-case validation per acceptance 2.6.6; any YAML whose stage context violates the policy table is rewritten end-to-end with the same scope as test-architect — instructions + status_message + `on_mcp_success` hooks + `blocked_mcp_tools` + allowed-tool lists, as applicable), `src/gobby/install/shared/skills/automate/SKILL.md`, `src/gobby/install/shared/skills/holistic-review/SKILL.md`, `src/gobby/install/shared/skills/merge-expert/SKILL.md`, `src/gobby/install/shared/skills/plan-draft/SKILL.md`, `src/gobby/install/shared/skills/plan-review/SKILL.md`, `src/gobby/install/shared/skills/plan/SKILL.md`, `src/gobby/install/shared/skills/qa/SKILL.md`, `src/gobby/install/shared/skills/review/SKILL.md`, `src/gobby/install/shared/skills/source-control/SKILL.md`, `src/gobby/install/shared/skills/task-transitions/SKILL.md` (existing — bundled SKILL.md instruction surfaces; prose rewritten inline where it describes the legacy "stage stays open, status becomes needs_review" semantics, replaced by per-stage `submit_for_review` / `approve_review` / `reject_review` model), `src/gobby/install/shared/workflows/rules/memory-lifecycle/require-memory-review-before-status.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/block-needs-review-interactive.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/inject-transition-skill.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/require-commit-before-status.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/require-error-triage.yaml`, `src/gobby/install/shared/workflows/rules/task-enforcement/require-task-transitions-skill-loaded.yaml` (existing — bundled workflow-rule YAMLs that reference `mark_task_review_*` / `needs_review`; rewritten inline where their semantics describe the legacy status axis. The post-cutover audit-scope grep is scoped to BOTH `src/gobby/install/shared/workflows/rules/**/*.yaml` (the bundled workflow-rule tree, where the matches above currently live) AND `src/gobby/install/shared/rules/**/*.yaml` (the bundled non-workflow rule tree, currently containing only `build/` but allowlisted to catch any future addition)

The agent-facing review tools (`mark_task_review_approved`, `mark_task_review_rejected`, `mark_task_needs_review`) must be cut over to stage-native semantics BEFORE Phase 3 enables the manifest dispatcher. Existing planner, plan-adversary, expansion-qa, qa-reviewer, and holistic-reviewer agents call these tools today; if Phase 3 swaps the dispatcher to read `task_stage_states` while these tools still write `status='review_approved'` / `'needs_review'` / `'rejected'`, the dispatcher and those agents drift apart for one or more heartbeats and the planning / expansion / development / holistic_qa stage chain stalls.

**The rewired tools are first-class stage-axis transitions, NOT compositions of `complete_stage` / `fail_stage`.** This is the load-bearing architectural distinction in the contract pivot: under the discarded tri-state contract, "approved" collapsed to "complete the review stage and advance," losing the semantic distinction between "submitted for review" and "review passed before merge." The new 5-state-per-stage model preserves that distinction by representing review-state on the SAME row as the work, with `submit_for_review` / `approve_review` / `reject_review` as separate transition methods that respect the row's `review_policy`.

Cutover preserves the agent-facing API (tool names, signatures, return shapes unchanged) but routes implementation through the per-row stage-axis transitions:

- `mark_task_needs_review(task_id, ...)` → resolves `current_stage(task_id)`, calls `StageStatesManager.submit_for_review(task_id, current_stage.name, by_session_id=..., notes=...)`. Transition: `in_progress → needs_review` on the SAME row (no advance to next stage). Raises `IllegalStageTransitionError` when `current_stage.review_policy == 'none'` (e.g., `test_arch.policy=none` — the test-architect agent must call `complete_stage` directly under the new contract; the §2.6.6 audit performs the YAML rewrite for `test-architect.yaml` and any other `policy=none` violators inline as part of this deliverable so §3.1 can spawn `test-architect` safely; the §5.3.7 audit later re-confirms post-cutover). Raises a typed `NoCurrentStageError` if the manifest is exhausted.
- `mark_task_review_approved(task_id, ...)` → resolves `current_stage`, calls `StageStatesManager.approve_review(task_id, current_stage.name, by_session_id=..., notes=...)`. Transition: `needs_review → review_approved` on the SAME row. `review_approved` is a DURABLE holding state — the tool does NOT advance to the next stage; the dispatcher's `<stage>_advance_rule` (Phase 3.1) handles the `review_approved → done` advance in a separate transition. Raises `IllegalStageTransitionError` when `policy='none'` or current state is not `needs_review`.
- `mark_task_review_rejected(task_id, reason, ...)` → resolves `current_stage`, calls `StageStatesManager.reject_review(task_id, current_stage.name, reason=reason, by_session_id=...)`. Transition: `needs_review → ready` on the SAME row. Increments `review_round_count` (NOT `work_attempt_count` — the work product is being asked to be re-done, not the work loop reset). When `review_round_count >= effective_max_review_rounds`, the storage method escalates the task instead of returning to `ready` (cap path documented on §2.1's `reject_review`). Raises `IllegalStageTransitionError` when `policy='none'` or current state is not `needs_review`.

This deliverable is the rewire only; legacy `status` values (`'review_approved'`, `'needs_review'`, `'rejected'`) STOP being written by these tools after this lands. Other writers of those values (if any surface in the call-site audit) are caught and rewired in Phase 5.3 before the column drop.

The `_agent_blocked_mcp_tools` rule that currently gates these tools for spawned agents stays in place — agent visibility is unchanged. The only change is the implementation behind the existing tool surface, and the new error type (`IllegalStageTransitionError`) surfaces clearly when an agent calls a tool against a row whose policy doesn't permit it.

**Scope of §2.6 testing (sequencing):** §2.6 lands BEFORE Phase 3 enables the manifest dispatcher and `auto_advance_ready_rule`. §2.6's tests are unit/contract tests that prove the rewired tools call `submit_for_review`/`approve_review`/`reject_review` correctly (no legacy status writes) — they do NOT exercise the dispatcher heartbeat or auto-advance, because those rules do not exist yet. The end-to-end heartbeat-advance smoke that validates the per-stage rule fan-out (`<stage>_review_rule` spawning the reviewer agent on `needs_review`, `<stage>_advance_rule` advancing `review_approved → done`) lives downstream in `## V1 Verification` where the rules are in place.

**Acceptance:**

- 2.6.1 — `mark_task_needs_review` rewired to `StageStatesManager.submit_for_review(current_stage.name)`; agent-facing signature and return shape unchanged. The transition is `in_progress → needs_review` on the SAME row (NOT a `complete_stage` advance). Raises `IllegalStageTransitionError` when `current_stage.review_policy == 'none'`. file: `src/gobby/storage/tasks/_transitions.py`. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_needs_review_submits_for_review_on_same_row`, `tests/storage/tasks/test_review_tools_stage_native.py::test_needs_review_rejected_on_policy_none`.
- 2.6.2 — `mark_task_review_approved` rewired to `StageStatesManager.approve_review(current_stage.name)`; transition `needs_review → review_approved` on the SAME row. Tool does NOT advance to the next stage; `review_approved` is durable and the dispatcher's `<stage>_advance_rule` handles `review_approved → done` later. Raises `IllegalStageTransitionError` when `policy='none'` or current state is not `needs_review`. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_approved_advances_to_review_approved_on_same_row`, `tests/storage/tasks/test_review_tools_stage_native.py::test_approved_does_not_advance_to_next_stage`, `tests/storage/tasks/test_review_tools_stage_native.py::test_approved_rejected_on_policy_none`.
- 2.6.3 — `mark_task_review_rejected` rewired to `StageStatesManager.reject_review(current_stage.name, reason=...)`; transition `needs_review → ready` on the SAME row. Increments `review_round_count` (NOT `work_attempt_count`). When `review_round_count >= effective_max_review_rounds`, escalates instead of returning to ready. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_rejected_returns_to_ready_increments_review_rounds`, `tests/storage/tasks/test_review_tools_stage_native.py::test_rejected_does_not_increment_work_attempts`, `tests/storage/tasks/test_review_tools_stage_native.py::test_rejected_over_cap_escalates`.
- 2.6.4 — Calling any of the three tools when `current_stage` is `None` (manifest exhausted) raises `NoCurrentStageError`; calling against a row whose state doesn't match the method's required source state raises `IllegalStageTransitionError`. No `task_stage_states` mutation occurs on either error path. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_no_current_stage_errors`, `tests/storage/tasks/test_review_tools_stage_native.py::test_wrong_source_state_errors_no_mutation`.
- 2.6.5 — Unit/contract verification of the rewire (no dispatcher dependency): given a fixture task at `planning.in_progress` (review_policy=required), calling `mark_task_needs_review` invokes `StageStatesManager.submit_for_review('planning', ...)` exactly once and writes no legacy `status` value (`'review_approved'` / `'needs_review'` / `'rejected'` are not written by the rewired tools). At `planning.needs_review`, `mark_task_review_approved` invokes `approve_review('planning', ...)` and produces `planning.review_approved`. At `planning.needs_review`, `mark_task_review_rejected` invokes `reject_review('planning', reason=...)` and produces `planning.ready` with `review_round_count = 1`. Asserted via mutation spy on the storage manager and a SQL probe on the post-call `tasks.status` column. test: `tests/storage/tasks/test_review_tools_stage_native.py::test_needs_review_calls_submit_for_review_no_legacy_writes`, `tests/storage/tasks/test_review_tools_stage_native.py::test_approved_calls_approve_review_no_legacy_writes`, `tests/storage/tasks/test_review_tools_stage_native.py::test_rejected_calls_reject_review_no_legacy_writes`.
- 2.6.6a — **(round-23 F1)** Server-qualified `complete_stage` consistency across the rewritten `test-architect.yaml` AND a negative regression for the round-22 server split. After the §2.6.6 rewrite, EVERY reference to `complete_stage` in the rewritten `test-architect.yaml` — `instructions:` prose, `design.status_message:`, `design.on_mcp_success` hook key, and (if it gains an explicit allowlist) `design.allowed_mcp_tools` — uses the server-qualified name `gobby-tasks-ops:complete_stage` (per §2.3.6b — `complete_stage` is a mutating tool registered on `gobby-tasks-ops` by `_ops_factory.py`). Negative regression: the audit script greps every bundled agent YAML under `src/gobby/install/shared/workflows/agents/**/*.yaml` AND every implementation/strategy plan markdown file under `.gobby/plans/**/*.md` (excluding the §2.3 server-placement table and §2.6.6 callout paragraphs that document the move) for the literal token `gobby-tasks:complete_stage` and asserts zero matches; same audit asserts zero matches for `gobby-tasks:` prefixed against the other seven mutating stage tools (`start_stage`, `fail_stage`, `add_stage`, `remove_stage`, `record_pr_verdict`, `record_pr_opened`, `record_merge_result`) so the round-22 split is permanently anchored. file: `src/gobby/install/shared/workflows/agents/test-architect.yaml`. test: `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_test_architect_yaml_complete_stage_uses_gobby_tasks_ops_in_instructions`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_test_architect_yaml_complete_stage_uses_gobby_tasks_ops_in_status_message`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_test_architect_yaml_complete_stage_success_hook_keyed_on_gobby_tasks_ops`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_test_architect_yaml_all_complete_stage_surfaces_agree_on_gobby_tasks_ops`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_no_bundled_agent_yaml_references_gobby_tasks_complete_stage`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_no_plan_markdown_outside_documented_section_references_gobby_tasks_complete_stage`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_no_bundled_agent_yaml_or_plan_references_gobby_tasks_prefix_for_other_seven_mutating_stage_tools` (parameterized over `start_stage`, `fail_stage`, `add_stage`, `remove_stage`, `record_pr_verdict`, `record_pr_opened`, `record_merge_result`).
- 2.6.6 — Pre-Phase-3 bundled call-site audit AND policy-driven rewrite of `mark_task_review_approved`, `mark_task_review_rejected`, and `mark_task_needs_review`. This deliverable BOTH enumerates callers AND performs every required rewrite inline so that §3.1 (which can spawn any agent on its stage's `in_progress` rule) does not stall on an `IllegalStageTransitionError` from a policy-violating caller. Audit enumerates callers via `grep -rln 'mark_task_review_\(approved\|rejected\)\|mark_task_needs_review' src/gobby/install/shared/` (the `src/gobby/install/shared/` root deliberately captures BOTH rule subtrees — `src/gobby/install/shared/workflows/rules/` where the current matches live, AND `src/gobby/install/shared/rules/` where future bundled rules may be added — plus all bundled workflows/agents and skills) and validates each caller's stage context against the per-stage policy table (§1.2 stages.yaml): the caller's stage row must have `policy ∈ {required, optional}` AND current state must match the legal source state. Allowlist of expected POST-rewrite callers (every entry below must remain a caller after this deliverable): `planner.yaml` (writes `planning`, calls `mark_task_needs_review`), `plan-adversary.yaml` (reviews `planning`, calls `mark_task_review_approved` / `_rejected`), `expansion-qa.yaml` (reviews `expansion`, calls approve/reject), `qa-reviewer.yaml` (reviews `development`, calls approve/reject), `holistic-reviewer.yaml` (reviews `holistic_qa` internally), `backend-developer.yaml` and `frontend-developer.yaml` (write `development`, call `mark_task_needs_review`), plus the bundled SKILL.md instruction surfaces (`automate`, `holistic-review`, `merge-expert`, `plan-draft`, `plan-review`, `plan`, `qa`, `review`, `source-control`, `task-transitions`) and the rule YAMLs whose review-tool references are policy-aligned per the table. **Disallowed under the new contract: any caller invoking these tools while the current stage is `test_arch` (review_policy=none).**

**Test-architect rewrite — full workflow scope, not just call-site swap.** `test-architect.yaml` is rewritten end-to-end so the workflow remains executable under the new contract. The pre-rewrite YAML's `design` step has an `on_mcp_success` hook keyed on `gobby-tasks:mark_task_review_approved` that flips `handoff_ready=true` and gates the transition to `terminate`; if only the agent's instructions are flipped from `mark_task_review_approved` to `complete_stage` while the success hook stays keyed on the old tool name, a successful `complete_stage` call leaves `handoff_ready=false` and the workflow can never reach `terminate`. Every `test-architect.yaml` change required by this deliverable, in one rewrite:
  (a) **Instructions/prose:** the `instructions:` block (drafting prose) and the `design` step's `status_message:` are rewritten to describe completing the stage with `gobby-tasks-ops:complete_stage` (NOT `mark_task_review_approved`; the server prefix is `gobby-tasks-ops` per §2.3.6b — `complete_stage` is a mutating tool and lives on the ops server in the §2.3 split, so the agent-facing prose, status message, success hook, and any `allowed_mcp_tools` references must ALL agree on the same server-qualified name). The CRITICAL RULES line "Do NOT call `mark_task_review_rejected`" is dropped — the rule is irrelevant under the new contract because review tools are illegal on `test_arch.policy=none` and the workflow cannot path through them. The "If blocked on missing context, escalate the stage task" guidance is preserved.
  (b) **`on_mcp_success` hooks:** the `mark_task_review_approved` success hook is removed and replaced with a `complete_stage` success hook that sets `handoff_ready=true` (server: `gobby-tasks-ops` per §2.3.6b — `complete_stage` is a mutating tool and lives on the ops server in the §2.3 split). The existing `escalate_task` success hook stays.
  (c) **`blocked_mcp_tools`:** the `gobby-tasks:mark_task_review_rejected` block is removed (the tool is now illegal at the policy layer for any `test_arch` caller; an agent that calls it gets `IllegalStageTransitionError`, which is a louder failure than a workflow block and does not depend on the agent definition). `gobby-tasks:close_task`, `gobby-tasks:de_escalate_task`, `gobby-tasks:reopen_task`, and the agent-spawn / kill blocks are kept verbatim.
  (d) **Allowed-tools or `allowed_mcp_tools`:** the `design` step uses `allowed_tools: "all"` today; that remains. No allowed-tool list edits are required for the call-site rewrite.

Other agent YAMLs whose stage context is unclear (`requirements-analyst.yaml`, `qa-dev.yaml`, `nightly-linter.yaml`, `nightly-test-fixer.yaml`, `merge-orchestrator.yaml`, `default.yaml`, `developer.yaml`) are validated case-by-case against the policy table; any YAML whose stage context violates the table is rewritten in this same deliverable using the same end-to-end scope (instructions + status_message + `on_mcp_success` hooks + `blocked_mcp_tools` + allowed-tool lists, as applicable) to a policy-respecting alternative (`complete_stage`, `fail_stage`, or removal of the call). Bundled SKILL.md instruction surfaces and rule YAMLs that reference legacy "stage stays open, status becomes needs_review" semantics are rewritten inline to describe the per-stage `submit_for_review` / `approve_review` / `reject_review` model. **Fail-fast gate:** the acceptance test FAILS if any disallowed call site (any review-tool call from a `policy=none` stage agent, any unrewritten legacy-status prose in a callable instruction surface) remains after this deliverable, OR if any rewritten workflow has a residual success hook keyed on a tool the agent no longer calls (handoff break). The §5.3.7 acceptance later re-runs the same gates as a post-cutover regression. test: `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_call_sites_match_policy_table`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_test_architect_yaml_does_not_call_review_tools`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_test_architect_yaml_calls_complete_stage_for_test_arch`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_test_architect_yaml_complete_stage_success_hook_sets_handoff_ready`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_test_architect_yaml_workflow_reaches_terminate_after_complete_stage`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_test_architect_yaml_drops_review_rejected_block_and_prose`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_no_disallowed_caller_remains_post_rewrite`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_no_residual_success_hook_keyed_on_unused_tool`, `tests/storage/tasks/test_review_tools_pre_phase3_audit.py::test_skill_md_prose_describes_5_state_model`.

## P3 Dispatcher Refactor

`kind: framing`

**Goal**: Rewrite the dispatcher's rule evaluation, candidate scan, and build-time manifest resolution to use `task_stage_states` instead of `(lifecycle, status)` tuples. After Phase 3, the daemon dispatches purely from the manifest model.

### 3.1 Rewrite `dispatch/rules.py` to query stage manifest [category: code] (depends: 2.1, 2.2, 2.6, 5.2)

`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`, `src/gobby/dispatch/actions.py`, `src/gobby/dispatch/dispatcher.py`, `src/gobby/storage/tasks/_models.py` (existing — `Task` dataclass; gains denormalized `stages: tuple[StageState, ...]` field populated by `reload_candidate` via the LEFT JOIN per the body of this deliverable. The `is_escalated` projection on `Task` is wired by §5.2 — the `(depends: 5.2)` edge above guarantees that column exists on `Task` before any `dispatch/rules.py` rule reads `child.is_escalated`. The `(depends: 2.6)` edge guarantees the `mark_task_review_*` tools are stage-native before the manifest dispatcher fires; without it, agents would still write legacy `status='needs_review'` / `'review_approved'` while the dispatcher reads `task_stage_states`. No cycle: §2.6 depends on §2.1 and §2.3; §2.3 depends on §2.1.)

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

**Dispatcher action surface (new stage-transition action types).** The new rule bodies emit two new typed actions in addition to the existing `SpawnAgentAction`, `StartExpansionAction`, `CreateIsolationAction`, `AdvanceLifecycleAction`, `AppendAuditMarkerAction`, `EscalateAction` union members. Rules never mutate `task_stage_states` directly — they emit an action, and the dispatcher's `execute_action` branch invokes `StageStatesManager`. The two new types, both defined in `src/gobby/dispatch/actions.py` and added to the `Action` union and module `__all__`:

```python
@dataclass(frozen=True, slots=True)
class StartStageAction:
    """Emit-only signal to start a stage. Executor delegates to
    StageStatesManager.start_stage(task_id, stage_name, by_session_id='dispatcher').
    Used by auto_advance_ready_rule, all_leaves_holistic_rule, and
    development_isolation_rule cases (a)/(b)/(c-followup)."""
    task_id: str
    stage_name: str


@dataclass(frozen=True, slots=True)
class AdvanceStageAction:
    """Emit-only signal to advance a stage past its current state via the
    method named in `method`. Executor branches:
      method='complete_stage' → StageStatesManager.complete_stage(task_id, stage_name, by_session_id, ...)
      method='approve_review' → StageStatesManager.approve_review(task_id, stage_name, by_session_id)
    Used by every <stage>_advance_rule (planning/expansion/development/holistic_qa/pr)
    and the merge rule's terminal-close transition."""
    task_id: str
    stage_name: str
    method: Literal["complete_stage", "approve_review"]
    by_session_id: str = "dispatcher"
```

The dispatcher's `execute_action` (`src/gobby/dispatch/dispatcher.py`) gains two new branches that delegate to the appropriate `StageStatesManager` method, propagate `IllegalStageTransitionError` to the dispatcher's existing typed-error path (which records a lifecycle event and re-raises for the heartbeat to log), and never side-effect outside the `StageStatesManager` call. Rule helpers `_complete_current_stage(task, reason)` and `_fail_current_stage(task, reason, needs_human)` emit `AdvanceStageAction` (or `EscalateAction` for fail paths) — they never call the manager directly. This preserves the existing pure-rule/action-executor separation: rules return actions, executors mutate storage.

Rule rewrite. The new contract requires per-stage rule fan-out: each `review_policy=required` stage emits THREE rules (`<stage>_work_rule` for `in_progress`, `<stage>_review_rule` for `needs_review`, `<stage>_advance_rule` for `review_approved`); each `review_policy=none` stage emits ONE rule (`<stage>_rule` for `in_progress`). Plus the cross-cutting rules below. Total rules: 4 cross-cutting + 5 single-policy stages + (5 × 3) for required-policy stages + 1 special-cased epic rule (`all_leaves_holistic_rule`) + Phase 4 placeholders. Every required-policy stage — including `holistic_qa` — registers a `_review_rule`; for `holistic_qa` the rule is a defensive resume hook (the agent's internal review is the normal path) that prevents a stranded `holistic_qa.needs_review` row from being left for the dispatcher's `list_automation_candidates` scan with no consumer.

Rule ordering (top-down evaluation; first-match-wins per scan):

| # | Rule | Gates on | Action |
|---|------|----------|--------|
| 1 | `auto_advance_ready_rule` | `current_stage.state == 'ready'` AND (`current_stage.position == 0` OR the row at `current_stage.position - 1` exists and has `state == 'done'`) AND registry `requires_human == False` AND (`current_stage.name IN {'expansion', 'pr'}` (work owned by non-agent paths — pipeline action and external PR-Agent respectively, both expected to have `default_agent IS NULL`) OR (`registry.default_agent IS NOT NULL` AND the registered agent resolves with `enabled: true`)) AND `current_stage.name NOT IN {'development', 'holistic_qa'}` (those are owned by their dedicated rules) | emit `StartStageAction(task_id, stage_name=current_stage.name)`; the executor calls `StageStatesManager.start_stage` which transitions `ready → in_progress`. Closes the fresh-task-stalls-at-ready gap. Note: positions are 0-indexed and dense per §1.1; the leftmost row is position `0` (NOT `1`), so a fresh task's first stage row satisfies the `position == 0` branch on the first heartbeat. |
| 2 | `disabled_agent_escalation_rule` | `current_stage.state == 'ready'` AND `current_stage.name NOT IN {'expansion', 'pr', 'development', 'holistic_qa'}` (the first two are non-agent work owners — `expansion` runs as a pipeline action and `pr` is owned by external/PR-Agent under #13552 — so their `default_agent IS NULL` is the EXPECTED state and MUST NOT escalate; the latter two are owned by `development_isolation_rule` and `all_leaves_holistic_rule` respectively) AND `registry.default_agent IS NOT NULL` AND the registered agent resolves with `enabled: false` | `EscalateAction(task_id=task.id, reason=f'{current_stage.name}_no_agent')`. Catches the four discovery placeholder cases. |
| 3 | `development_isolation_rule` | `current_stage.name == 'development'` AND `state == 'ready'` | Four-case state machine — see "Development-ready state machine" below |
| 4 | `all_leaves_holistic_rule` | `_is_epic(task)` AND `current_stage(task) == ('holistic_qa', 'ready')` AND every direct child satisfies `is_child_parked(child)` OR `child.is_closed` | emit `StartStageAction(task_id, stage_name='holistic_qa')` |
| 5 | `ideation_rule` | `current_stage == ('ideation', 'in_progress')` | spawn `analyst` (placeholder shim — `disabled_agent_escalation_rule` short-circuits at `ready` until real agent lands) |
| 6 | `research_rule` | `current_stage == ('research', 'in_progress')` | spawn `researcher` (placeholder shim) |
| 7 | `architecture_rule` | `current_stage == ('architecture', 'in_progress')` | spawn `architect` (placeholder shim) |
| 8 | `prd_rule` | `current_stage == ('prd', 'in_progress')` | spawn `product-manager` (placeholder shim) |
| 9 | `planning_work_rule` | `current_stage == ('planning', 'in_progress')` | spawn `planner` |
| 10 | `planning_review_rule` | `current_stage == ('planning', 'needs_review')` | spawn `plan-adversary` |
| 11 | `planning_advance_rule` | `current_stage == ('planning', 'review_approved')` | emit `AdvanceStageAction(task_id, stage_name='planning', method='complete_stage')`; the executor calls `StageStatesManager.complete_stage` to advance `review_approved → done`; then `auto_advance_ready_rule` promotes the next stage on the next heartbeat |
| 12 | `test_arch_rule` | `current_stage == ('test_arch', 'in_progress')` | spawn `test-architect` (test_arch.review_policy=none — single rule, no review fan-out) |
| 13 | `expansion_work_rule` | `current_stage == ('expansion', 'in_progress')` | `StartExpansionAction` (pipeline action; no agent spawn) |
| 14 | `expansion_review_rule` | `current_stage == ('expansion', 'needs_review')` | spawn `expansion-qa` |
| 15 | `expansion_advance_rule` | `current_stage == ('expansion', 'review_approved')` | emit `AdvanceStageAction(task_id, stage_name='expansion', method='complete_stage')`; the executor calls `complete_stage` to advance `review_approved → done` |
| 16 | `development_rule` | `current_stage == ('development', 'in_progress')` AND `_is_leaf(task)` | spawn `dev-agent` (registry default `backend-developer`) |
| 17 | `development_review_rule` | `current_stage == ('development', 'needs_review')` AND `_is_leaf(task)` | spawn `qa-reviewer` |
| 18 | `development_advance_rule` | `current_stage == ('development', 'review_approved')` AND `_is_leaf(task)` | emit `AdvanceStageAction(task_id, stage_name='development', method='complete_stage')`; the executor calls `complete_stage` to advance `review_approved → done` |
| 19 | `holistic_qa_rule` | `current_stage == ('holistic_qa', 'in_progress')` | spawn `holistic-reviewer` (the agent does work AND review internally; produces `in_progress → review_approved` via `submit_for_review` then `approve_review` from inside the agent run) |
| 19a | `holistic_qa_review_rule` | `current_stage == ('holistic_qa', 'needs_review')` | re-spawn `holistic-reviewer` (resume hook for the rare case where the agent crashed/exited between its internal `submit_for_review` and `approve_review`); the agent's prompt detects the resume context and proceeds straight to internal review + `approve_review` without redoing the work pass |
| 20 | `holistic_qa_advance_rule` | `current_stage == ('holistic_qa', 'review_approved')` | emit `AdvanceStageAction(task_id, stage_name='holistic_qa', method='complete_stage')`; the executor calls `complete_stage` to advance `review_approved → done` |
| 21 | `pr_work_rule` | `current_stage == ('pr', 'in_progress')` | escalate with `EscalateAction(reason='pr_no_agent')` until #13552 lands the PR-Agent (Phase 4.1 supplies the body once the agent is registered) |
| 22 | `pr_review_rule` | `current_stage == ('pr', 'needs_review')` | NO spawn (external review by PR-Agent or human); operator / PR-Agent calls `record_pr_verdict` which maps to `approve_review` / `reject_review` |
| 23 | `pr_advance_rule` | `current_stage == ('pr', 'review_approved')` | emit `AdvanceStageAction(task_id, stage_name='pr', method='complete_stage')`; the executor calls `complete_stage` to advance to `done`; the next stage row (`merge`) becomes the current stage at `ready` |
| 24 | `merge_rule` | `current_stage == ('merge', 'in_progress')` | spawn `merge-orchestrator` (merge.review_policy=none — single rule; the merge agent's success path calls `record_merge_result` → `complete_stage('merge')` which closes the task per §2.1 invariant 8) |

`holistic_qa` is the one required-policy stage where the agent does both work and review internally. The agent's normal path is `in_progress → needs_review → review_approved` from inside its own run before exiting; the dispatcher's `holistic_qa_advance_rule` then advances `review_approved → done`. `holistic_qa_review_rule` exists as a defensive resume hook for the rare case where the agent crashes or exits between its internal `submit_for_review` and `approve_review` calls — without it, `list_automation_candidates` (which now matches `needs_review`) would surface the row indefinitely with no consumer. The resume hook re-spawns `holistic-reviewer` with the same registry slot; the agent prompt is responsible for detecting the resume context (manifest row already at `needs_review`) and proceeding straight to internal review + `approve_review` without redoing the work pass. Idempotency contract for `holistic-reviewer`: re-entry on `needs_review` is a review-only run; re-entry on `in_progress` is a fresh work-then-review run.

`expansion` and `development` are the work stages whose `_review_rule` spawns a different agent than `_work_rule`; this is the pattern that survives the contract pivot — work and review live on the SAME stage row, but the dispatcher fans out into different agents based on the row's state. `pr` is special because external review (human or PR-Agent) replaces the dispatcher-spawned reviewer; `pr_review_rule` is a no-spawn placeholder that surfaces the row in queries but doesn't act.

**Mapping from old (tri-state, 14-stage) rules:**

| Old rule | New rule(s) | Notes |
|----------|-------------|-------|
| `plan_review_rule` | `planning_work_rule`, `planning_review_rule`, `planning_advance_rule` | Review is now state on the same row, not a separate stage |
| `adversarial_review_rule` | `planning_review_rule` | The `adversarial_review` stage is gone; `plan-adversary` now reviews `planning` directly |
| `test_arch_rule` | `test_arch_rule` (unchanged structure) | test_arch.review_policy=none — single rule |
| `expansion_rule` | `expansion_work_rule`, `expansion_review_rule`, `expansion_advance_rule` | The `expansion_qa` stage is gone; `expansion-qa` now reviews `expansion` directly |
| `expansion_qa_rule` | `expansion_review_rule` | Stage merged into `expansion.needs_review` |
| `isolation_rule` | `development_isolation_rule` (unchanged) | Owns `development.ready → in_progress` transition |
| `dev_rule` | `development_rule` (work), `development_review_rule`, `development_advance_rule` | The `code_review_qa` stage is gone; `qa-reviewer` now reviews `development` directly |
| `qa_rule` | `development_review_rule` | Stage merged into `development.needs_review` |
| `code_review_qa_rule` | (deleted) | Stage gone |
| `leaf_park_rule` | (deleted — folded into `is_child_parked` predicate) | Same predicate as before |
| `all_leaves_holistic_rule` | `all_leaves_holistic_rule` (unchanged) | Reads `is_child_parked` |
| `holistic_rule` | `holistic_qa_rule`, `holistic_qa_review_rule`, `holistic_qa_advance_rule` | `_review_rule` exists as a defensive resume hook (re-spawns `holistic-reviewer` for crash recovery) — aggregate review is normally internal to the agent |
| `pr_rule` (Phase 4) | `pr_work_rule`, `pr_review_rule`, `pr_advance_rule` | PR review is policy=required with external reviewer |
| `merge_rule` (Phase 4) | `merge_rule` (unchanged structure) | merge.review_policy=none |

**Stages excluded from auto-advance** (`auto_advance_ready_rule` skips them; the dedicated rule below owns the transition):

- `development` — owned by `development_isolation_rule` (must inspect `task.isolation` and `task_artifacts` worktree/clone pair before starting).
- `holistic_qa` — owned by `all_leaves_holistic_rule` (must verify every child is parked or terminal before starting; epics may have children working through development for many heartbeats while the parent's `holistic_qa.ready` is technically the leftmost-non-done row).

Implementation choice: hardcode the two-element exclude list in the rule body (`current_stage.name NOT IN {'development', 'holistic_qa'}`). Alternative (registry sentinel `auto_advance: false` per stage) is deferred — two special-cases is still cleaner than a registry change for this epic, and the exclude list is co-located with the rule it belongs to.

**`is_child_parked(child) -> bool` predicate** (defined alongside `_is_leaf` in `src/gobby/dispatch/rules.py`):

```python
def is_child_parked(child: Task) -> bool:
    """Durable predicate: True when a leaf child has finished its work and the
    parent's holistic_qa is safe to advance. Computed entirely from the child's
    denormalized manifest (`child.stages`) and `is_closed`/`is_escalated`
    flags — no new column, no transient signal, no cross-heartbeat state.

    A child is parked when ALL of:
      * `_is_leaf(child)` is True,
      * `child.is_escalated` is False (escalated children block the parent),
      * EITHER `child.is_closed` is True (terminal-closed via the §2.1
        invariant-8 manifest-exhausted close — ANY task type, ANY terminal
        stage — or via the legacy merge cascade) OR `current_stage(child)
        is None AND NOT child.is_closed` (every manifest row is `done` but
        is_closed has not yet been written).

    Reachability of the `current_stage is None AND NOT is_closed` branch.
    Under §2.1 invariant 8, `complete_stage` writes the close in the SAME DB
    transaction as the final stage UPDATE; close-failure rolls both back and
    escalates the task. Therefore this branch is NEVER reachable for a task
    that has reached manifest exhaustion via `complete_stage` in normal
    operation. It is reachable in exactly two bounded windows:

      1. **Migration 234 backfill window.** Mid-transaction, after the
         backfill writes manifest rows but before the §2.2 acceptance 2.2.31
         close-pass commits, an all-`done`-but-not-`is_closed` row can briefly
         exist. The window is the single migration transaction; once 234
         commits, every such task is closed.
      2. **Synthetic test fixtures.** Tests that build `Task.stages` directly
         (bypassing `complete_stage`) to exercise this predicate's branches
         without driving a real terminal close.

    Outside those two windows, the branch is unreachable. The predicate
    accepts it as defense-in-depth so the parent's `all_leaves_holistic_rule`
    does not stall on a transient mid-transaction state during migration.

    The previous predicate added a third gate (highest-position done row
    `stage_name == 'code_review_qa'`) which doesn't match the §2.2 default
    manifests: `task` and `chore` end at `merge`; `bug`/`refactor`/`feature`
    have `pr` and `merge` after `code_review_qa`. The terminal-close
    contract from §2.1 invariant 8 / acceptance 2.1.9 makes that gate
    unnecessary — leaves auto-close at whatever stage their manifest ends,
    and the parent's rule treats all terminal-closed leaves as parked.
    """
```

The predicate is a pure function of `child.stages` and the two task flags — `reload_candidate` already populates `child.stages`, `is_closed`, and `is_escalated` per acceptance 3.1.3, so the parent's rule pays no extra SQL. The previous `LeafParkedSignalAction` was deleted because it required scanning manifest-exhausted leaves (which §3.2's automation-candidate filter excludes) and produced only transient state. This predicate replaces it durably without any new schema or scan-set change. With §2.1 invariant 8's terminal-close contract and §2.2 acceptance 2.2.31's backfill close-pass, the predicate's primary truth path is `_is_leaf AND is_closed AND NOT is_escalated`; the `current_stage is None AND NOT is_closed` branch is reachable in exactly two bounded windows — the migration-234 transaction (closed by 2.2.31 before commit) and synthetic test fixtures that bypass `complete_stage` — and is documented as such in the docstring above.

**Development-ready state machine (`development_isolation_rule` four cases):** The legacy isolation rule only created worktrees/clones; it did not own stage starts. Under the manifest model, the development stage stays at `development.ready` until this rule fires, so the rule MUST handle every starting condition or development-ready tasks stall forever. Cases (rule body evaluates them in order):

- **(a) `task.isolation == 'none'`** — no isolation needed. Rule emits `StartStageAction(task_id, stage_name='development')` (the rule itself does NOT call the manager). The executor's `StartStageAction` branch invokes `StageStatesManager.start_stage(task_id, 'development', by_session_id='dispatcher')`, transitioning `ready → in_progress`; `development_rule` fires on the next heartbeat.
- **(b) Isolation required AND the worktree/clone pair already exists in `task_artifacts`** — operator pre-created the isolation, or a prior heartbeat created it but failed to start the stage (recovery case). Same emission as (a): `StartStageAction(task_id, stage_name='development')`; the executor advances `ready → in_progress`. Rule reads `task.artifacts.worktree_path` / `worktree_id` for `isolation == 'worktree'` and `task.artifacts.clone_path` / `clone_id` for `isolation == 'clone'`; both members of the pair must be present.
- **(c) Isolation required AND the worktree/clone pair is missing** — normal first-time start. Rule emits `CreateIsolationAction(task_id)` (no stage transition; the rule never writes to `task_stage_states`). The action handler creates the isolation, writes the artifact pair atomically, and on success the rule on the next heartbeat reaches case (b) and emits the follow-up `StartStageAction` (stage stays at `ready` for one heartbeat while isolation is created — same latency as the legacy isolation rule). Alternative (single-action atomic create + start) is rejected: keeps action handlers single-purpose and lets isolation creation fail without partial stage transitions.
- **(d) Isolation creation fails** — `CreateIsolationAction` returns failure (handler raises or returns a failure result). Rule emits `EscalateAction(task_id=task.id, reason=f'development_isolation_failed:{type(error).__name__}')`. The error type is encoded INSIDE the supported `reason` string; `EscalateAction` at `src/gobby/dispatch/actions.py:63` accepts only `task_id` and `reason`, no `detail` kwarg. Stage remains at `ready`; `is_escalated=1` is set on the task. Operator must investigate (the daemon log carries the full error message and stack trace), clear the escalation, and the rule re-evaluates on the next heartbeat (case (c) again unless the operator manually created the isolation, in which case case (b)).

The rule never spawns the development agent — that is `development_rule`'s job (`current_stage.state == 'in_progress'`). This separation keeps the start transition deterministic and lets retries of agent-spawn failures not re-create isolation.

For each retained rule, port the existing attempt-count helpers to read `StageState.work_attempt_count` and `StageState.review_round_count` instead of artifact counters (`qa_attempts`, etc.). The five legacy `task_artifacts` cap columns (`max_expansion_attempts`, `max_qa_rounds`, `max_merge_attempts`, `max_holistic_rounds`, `max_review_rounds`) are migrated into per-stage caps on `task_stage_states` during §2.2 backfill per the §1.1 authoritative inventory (`max_expansion_attempts → expansion.max_work_attempts`; `max_qa_rounds → development.max_review_rounds`; `max_merge_attempts → merge.max_work_attempts`; `max_holistic_rounds → holistic_qa.max_review_rounds`; `max_review_rounds → pr.max_review_rounds`); rules read effective caps via `state_row.max_<X> ?? registry.default_max_<X>`. There is no `max_planning_rounds` column — `planning`'s caps come from registry defaults via NULL on the per-stage row. The cap predicate is `>=` (a task that has started a stage `cap` times and just failed the cap-th attempt escalates without another retry, per §2.1 invariant 5).

`_is_unattended(task)` continues to read `task.assigned_agent`; that field is retained and still drives the unattended-fallback branch in `_fallback`.

**Acceptance:**

- 3.1.1 — `task_has_stage` and `current_stage` helpers added; `_stage_skipped` and `_state` deleted. file: `src/gobby/dispatch/rules.py`. symbol: `gobby.dispatch.rules.task_has_stage`, `gobby.dispatch.rules.current_stage`.
- 3.1.2 — Each rule in the table above is renamed and rewritten to query the manifest; old `_advance(...)` calls are replaced by emitting `StartStageAction` or `AdvanceStageAction` (per the action surface block above) — rules NEVER write to `StageStatesManager` directly. The executor branches in `dispatcher.execute_action` (acceptance 3.1.4b) own all manager-method calls. test: `tests/dispatch/test_rules_stage_native.py::test_rule_table_complete`, `tests/dispatch/test_rules_stage_native.py::test_no_rule_invokes_stage_states_manager_directly`.
- 3.1.3 — `Task.stages` denormalized field populated by `reload_candidate`. file: `src/gobby/dispatch/dispatcher.py`. test: `tests/dispatch/test_reload_candidate_includes_stages.py::test_stages_loaded`.
- 3.1.4 — Attempt-count helpers read from `StageState.work_attempt_count` and `StageState.review_round_count`; effective caps resolved via `state_row.max_<X> ?? registry.default_max_<X>`. test: `tests/dispatch/test_rules_stage_native.py::test_work_attempt_caps_honored`, `tests/dispatch/test_rules_stage_native.py::test_review_round_caps_honored`, `tests/dispatch/test_rules_stage_native.py::test_caps_inherit_registry_defaults_when_state_row_null`.
- 3.1.4a — `StartStageAction` and `AdvanceStageAction` dataclasses added to `src/gobby/dispatch/actions.py` exactly as documented in the "Dispatcher action surface" block above (frozen, slotted; `StartStageAction` carries `task_id`, `stage_name`; `AdvanceStageAction` carries `task_id`, `stage_name`, `method ∈ {'complete_stage','approve_review'}`, `by_session_id` defaulting to `'dispatcher'`). Both classes are members of the `Action` union and exported via `__all__`. The legacy `Action` union members (`SpawnAgentAction`, `StartExpansionAction`, `CreateIsolationAction`, `AdvanceLifecycleAction`, `AppendAuditMarkerAction`, `EscalateAction`) remain. file: `src/gobby/dispatch/actions.py`. symbol: `gobby.dispatch.actions.StartStageAction`, `gobby.dispatch.actions.AdvanceStageAction`. test: `tests/dispatch/test_actions_surface.py::test_start_stage_action_shape`, `tests/dispatch/test_actions_surface.py::test_advance_stage_action_method_literal`, `tests/dispatch/test_actions_surface.py::test_action_union_includes_new_types`, `tests/dispatch/test_actions_surface.py::test_legacy_action_types_still_present`.
- 3.1.4b — `dispatcher.execute_action` (`src/gobby/dispatch/dispatcher.py`) gains two new branches: a `StartStageAction` branch that calls `StageStatesManager.start_stage(action.task_id, action.stage_name, by_session_id='dispatcher')`, and an `AdvanceStageAction` branch that dispatches by `action.method` to `StageStatesManager.complete_stage` or `StageStatesManager.approve_review` with `by_session_id=action.by_session_id`. `IllegalStageTransitionError` raised by the manager propagates through the dispatcher's existing typed-error path (logged + lifecycle event written by the heartbeat); the executor performs no side-effects outside the `StageStatesManager` call. file: `src/gobby/dispatch/dispatcher.py`. symbol: `gobby.dispatch.dispatcher.execute_action`. test: `tests/dispatch/test_execute_action_stage_branches.py::test_executes_start_stage_action_calls_manager`, `tests/dispatch/test_execute_action_stage_branches.py::test_executes_advance_stage_action_complete_method`, `tests/dispatch/test_execute_action_stage_branches.py::test_executes_advance_stage_action_approve_method`, `tests/dispatch/test_execute_action_stage_branches.py::test_illegal_transition_propagates_typed`, `tests/dispatch/test_execute_action_stage_branches.py::test_executor_no_side_effects_outside_manager`.
- 3.1.4c — Rule emissions verified end-to-end through the executor for EVERY rule in §3.1's table that emits a stage-transition action. The full set covered:
  - **`StartStageAction` emitters (the executor advances `<stage>.ready → <stage>.in_progress`):**
    - `auto_advance_ready_rule` — parameterized over each non-excluded stage (`planning`, `test_arch`, `expansion`, `pr`, `merge` — the four discovery stages stall on `disabled_agent_escalation_rule` so are excluded from this fixture)
    - `all_leaves_holistic_rule` — emits `StartStageAction(stage_name='holistic_qa')` on the epic
    - `development_isolation_rule` case (a) — `task.isolation == 'none'` → emits `StartStageAction(stage_name='development')`
    - `development_isolation_rule` case (b) — pair already in artifacts → emits `StartStageAction(stage_name='development')`
    - `development_isolation_rule` case (c) follow-up — after a successful `CreateIsolationAction` lands the artifact pair, the next heartbeat reaches case (b) and emits `StartStageAction(stage_name='development')`
  - **`AdvanceStageAction(method='complete_stage')` emitters (the executor advances `<stage>.review_approved → <stage>.done`):**
    - `planning_advance_rule` (stage_name='planning')
    - `expansion_advance_rule` (stage_name='expansion')
    - `development_advance_rule` (stage_name='development')
    - `holistic_qa_advance_rule` (stage_name='holistic_qa')
    - `pr_advance_rule` (stage_name='pr') — placeholder in this deliverable, full body in Phase 4.1
  - **Negative source-code grep (rules never call the manager directly):**
    `tests/dispatch/test_rule_to_executor_integration.py::test_no_rule_mutates_stage_states_directly` reads `src/gobby/dispatch/rules.py` source bytes and asserts ZERO matches for `r"StageStatesManager\\b"` (any reference — import, type annotation, attribute access, or method call) AND ZERO matches for `r"\\.(start_stage|complete_stage|approve_review|submit_for_review|reject_review|fail_stage)\\("` (any direct invocation of a stage-transition method, regardless of the receiver name). The same grep is repeated for `src/gobby/dispatch/__init__.py` re-exports to catch indirect imports.

  Together with the per-rule integration tests below, this covers the complete §3.1 rule-table action surface; no rule emits an `AdvanceStageAction` or `StartStageAction` without an executor-integration test, and no rule writes `task_stage_states` directly. test: `tests/dispatch/test_rule_to_executor_integration.py::test_auto_advance_ready_rule_advances_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_all_leaves_holistic_rule_starts_holistic_qa_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_development_isolation_rule_case_a_starts_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_development_isolation_rule_case_b_starts_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_development_isolation_rule_case_c_followup_starts_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_planning_advance_rule_completes_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_expansion_advance_rule_completes_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_development_advance_rule_completes_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_holistic_qa_advance_rule_completes_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_pr_advance_rule_placeholder_completes_via_executor`, `tests/dispatch/test_rule_to_executor_integration.py::test_no_rule_mutates_stage_states_directly`, `tests/dispatch/test_rule_to_executor_integration.py::test_no_rule_imports_or_references_stage_states_manager`.
- 3.1.5 — Pass-through escalate-no-reviewer rules for `policy=required` stages whose `reviewer_agent` is missing or disabled (`expansion_review_rule` / `development_review_rule` / `holistic_qa_rule` use the `<stage>_no_reviewer` reason; `pr_work_rule` uses `pr_no_agent`). The escalation rule is per-stage, not generic — each rule checks its own `reviewer_agent` slot in the registry. test: `tests/dispatch/test_rules_stage_native.py::test_no_reviewer_stage_escalates`.
- 3.1.6 — `auto_advance_ready_rule` promotes the leftmost `ready` row to `in_progress` when (a) `current_stage.position == 0` OR the row at `current_stage.position - 1` exists and has `state == 'done'` (positions are 0-indexed and dense per §1.1; the leftmost row is position `0`, not `1`), (b) the stage registry entry has `requires_human == False`, (c) `current_stage.name IN {'expansion', 'pr'}` (non-agent work owners — `expansion` runs as a pipeline action, `pr` is owned by external/PR-Agent under #13552; both have `default_agent IS NULL` by design and the rule MUST start them despite the null) OR (`registry.default_agent IS NOT NULL` AND the registered agent resolves with `enabled: true`), and (d) `current_stage.name NOT IN {'development', 'holistic_qa'}` (those two stages are owned by `development_isolation_rule` and `all_leaves_holistic_rule` respectively). A freshly built task with manifest `[planning, test_arch, expansion, development, pr, merge]` (positions `0..5`) advances `planning.ready (position 0) → planning.in_progress` on the first heartbeat via the `position == 0` branch; the same task's `expansion.ready` row (position `2`) advances on the heartbeat after `test_arch.done` (position `1`) via the prior-row-done branch even though `expansion.default_agent IS NULL` (pipeline-driven); the same task's `pr.ready` row (position `4`) advances after `development.done` (position `3`) even though `pr.default_agent IS NULL` (PR-Agent placeholder); an epic with `holistic_qa.ready` as its leftmost-non-done row does NOT auto-start `holistic_qa` even if the prior `development` row is `done` (owned by `all_leaves_holistic_rule`). When condition (c) is FALSE (the `default_agent` is `NOT NULL` AND that agent is `enabled: false`), this rule does NOT fire — the disabled-agent case is handled by `disabled_agent_escalation_rule` (acceptance 3.1.23) which escalates instead of stalling. test: `tests/dispatch/test_rules_stage_native.py::test_auto_advance_first_stage_at_position_zero`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_skips_human_gated`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_skips_disabled_agent_yields_to_escalation_rule`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_skips_holistic_qa`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_starts_expansion_despite_null_default_agent`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_starts_pr_despite_null_default_agent`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_does_not_route_expansion_or_pr_to_disabled_agent_escalation`, `tests/dispatch/test_rules_stage_native.py::test_auto_advance_uses_zero_indexed_first_row_predicate` (asserts the rule fires on the leftmost-non-done row whose `position` equals zero, and does NOT fire on a synthetic-broken manifest whose leftmost row's `position` value is one with no row at position-value zero — guards against re-introducing 1-based predicates).
- 3.1.7 — `development_isolation_rule` case (a): `task.isolation == 'none'` and `current_stage == ('development', 'ready')` → rule emits `StartStageAction(task_id, stage_name='development')` (no manager call from inside the rule); the executor advances `ready → in_progress` without creating any isolation. test: `tests/dispatch/test_development_isolation_rule.py::test_isolation_none_emits_start_stage_action`, `tests/dispatch/test_development_isolation_rule.py::test_isolation_none_advances_via_executor`.
- 3.1.8 — `development_isolation_rule` case (b): isolation required AND the worktree/clone pair already exists in `task_artifacts` → rule emits `StartStageAction(task_id, stage_name='development')`; the executor advances `ready → in_progress` without re-creating isolation. Verifies recovery from a prior heartbeat that created isolation but did not start the stage. test: `tests/dispatch/test_development_isolation_rule.py::test_isolation_pair_present_emits_start_stage_action`, `tests/dispatch/test_development_isolation_rule.py::test_isolation_pair_present_advances_via_executor`.
- 3.1.9 — `development_isolation_rule` case (c): isolation required AND pair missing → rule emits `CreateIsolationAction(task_id)` (rule writes nothing to `task_stage_states`). On success the artifact pair is written atomically; the next heartbeat reaches case (b) and emits the follow-up `StartStageAction` which the executor turns into `start_stage`. Stage stays at `ready` for exactly one heartbeat after isolation creation. test: `tests/dispatch/test_development_isolation_rule.py::test_isolation_missing_emits_create_isolation_action`, `tests/dispatch/test_development_isolation_rule.py::test_isolation_missing_then_pair_present_emits_start_stage_action_next_heartbeat`.
- 3.1.10 — `development_isolation_rule` case (d): `CreateIsolationAction` fails (handler returns failure or raises) → rule emits an escalation action whose constructor receives ONLY `task_id` and `reason` kwargs (no `detail`, matching `src/gobby/dispatch/actions.py:63` signature), with the reason string carrying the error type as a colon-suffix in the form `development_isolation_failed:<error_type>`. `task.is_escalated == 1` is set; stage remains at `development.ready`; on next heartbeat the rule re-evaluates only after the escalation is cleared. test: `tests/dispatch/test_development_isolation_rule.py::test_isolation_failure_escalates_with_reason_carrying_error_type`, `tests/dispatch/test_development_isolation_rule.py::test_isolation_failure_escalation_uses_supported_signature_only`. Source-code regression (post-implementation): a grep for the unsupported-kwarg pattern across `src/gobby/dispatch/rules.py` and `src/gobby/dispatch/actions.py` returns zero matches. The plan file itself is exempt because its changelog and acceptance prose are historical documentation of the bug; only runnable code is in scope for the negative check.

Per-rule coverage (one acceptance per data row of the §3.1 rule rewrite table, per the plan-coverage contract's table-row decomposition rule). Some rules already have dedicated acceptances above (`auto_advance_ready_rule` → 3.1.6; `development_isolation_rule` → 3.1.7-3.1.10); the items below cover the remaining rule rows so every data row has its own acceptance:

- 3.1.11 — `planning_work_rule` fires on `current_stage == ('planning', 'in_progress')` and emits a spawn action for the `planner` agent. test: `tests/dispatch/test_rules_stage_native.py::test_planning_work_rule_spawns_planner`.
- 3.1.11a — `planning_review_rule` fires on `current_stage == ('planning', 'needs_review')` and emits a spawn action for the `plan-adversary` agent. test: `tests/dispatch/test_rules_stage_native.py::test_planning_review_rule_spawns_plan_adversary`.
- 3.1.11b — `planning_advance_rule` fires on `current_stage == ('planning', 'review_approved')` and emits `AdvanceStageAction(task_id, stage_name='planning', method='complete_stage')`; the executor's `AdvanceStageAction` branch calls `StageStatesManager.complete_stage('planning')` to transition `review_approved → done`. After the rule fires and the executor commits, `auto_advance_ready_rule` promotes the next stage (`test_arch.ready → in_progress`) on the next heartbeat. test: `tests/dispatch/test_rules_stage_native.py::test_planning_advance_rule_emits_advance_stage_action`, `tests/dispatch/test_rules_stage_native.py::test_planning_advance_followed_by_auto_advance_test_arch`.
- 3.1.12 — `ideation_rule` fires on `current_stage == ('ideation', 'in_progress')` and emits a spawn action for the `analyst` placeholder agent (`disabled_agent_escalation_rule` short-circuits at `ready` until a real agent ships). test: `tests/dispatch/test_rules_stage_native.py::test_ideation_rule_spawns_analyst`.
- 3.1.12a — `research_rule` fires on `current_stage == ('research', 'in_progress')` and emits a spawn action for the `researcher` placeholder agent. test: `tests/dispatch/test_rules_stage_native.py::test_research_rule_spawns_researcher`.
- 3.1.12b — `architecture_rule` fires on `current_stage == ('architecture', 'in_progress')` and emits a spawn action for the `architect` placeholder agent. test: `tests/dispatch/test_rules_stage_native.py::test_architecture_rule_spawns_architect`.
- 3.1.12c — `prd_rule` fires on `current_stage == ('prd', 'in_progress')` and emits a spawn action for the `product-manager` placeholder agent. test: `tests/dispatch/test_rules_stage_native.py::test_prd_rule_spawns_product_manager`.
- 3.1.13 — `test_arch_rule` fires on `current_stage == ('test_arch', 'in_progress')` and emits a spawn action for the `test-architect` agent. Single rule — `test_arch.review_policy=none` so no `_review_rule` or `_advance_rule` exist for this stage. test: `tests/dispatch/test_rules_stage_native.py::test_test_arch_rule_spawns_test_architect`, `tests/dispatch/test_rules_stage_native.py::test_test_arch_no_review_rule_registered`.
- 3.1.14 — `expansion_work_rule` fires on `current_stage == ('expansion', 'in_progress')` and emits a `StartExpansionAction` (pipeline action; no agent spawn). test: `tests/dispatch/test_rules_stage_native.py::test_expansion_work_rule_emits_start_expansion`.
- 3.1.15 — `expansion_review_rule` fires on `current_stage == ('expansion', 'needs_review')` and emits a spawn action for the `expansion-qa` agent. test: `tests/dispatch/test_rules_stage_native.py::test_expansion_review_rule_spawns_expansion_qa`.
- 3.1.15a — `expansion_advance_rule` fires on `current_stage == ('expansion', 'review_approved')` and emits `AdvanceStageAction(task_id, stage_name='expansion', method='complete_stage')`; the executor calls `complete_stage('expansion')`. The rule itself never invokes `StageStatesManager`. test: `tests/dispatch/test_rules_stage_native.py::test_expansion_advance_rule_emits_advance_stage_action`, `tests/dispatch/test_rules_stage_native.py::test_expansion_advance_rule_advances_via_executor`.
- 3.1.16 — `development_rule` fires on `current_stage == ('development', 'in_progress')` AND `_is_leaf(task)` and emits a spawn action for the `dev-agent` (registry default `backend-developer`). test: `tests/dispatch/test_rules_stage_native.py::test_development_rule_spawns_dev_agent`.
- 3.1.17 — `development_review_rule` fires on `current_stage == ('development', 'needs_review')` AND `_is_leaf(task)` and emits a spawn action for the `qa-reviewer` agent. test: `tests/dispatch/test_rules_stage_native.py::test_development_review_rule_spawns_qa_reviewer`.
- 3.1.17a — `development_advance_rule` fires on `current_stage == ('development', 'review_approved')` AND `_is_leaf(task)` and emits `AdvanceStageAction(task_id, stage_name='development', method='complete_stage')`; the executor calls `complete_stage('development')`. The rule itself never invokes `StageStatesManager`. test: `tests/dispatch/test_rules_stage_native.py::test_development_advance_rule_emits_advance_stage_action`, `tests/dispatch/test_rules_stage_native.py::test_development_advance_rule_advances_via_executor`.
- 3.1.18 — `is_child_parked(child)` predicate is a pure function of `child.stages` + `child.is_closed` (Python projection from `is_task_closed` reading `closed_at IS NOT NULL OR status == 'closed'`) + `child.is_escalated` flags: returns True iff `_is_leaf(child)` AND NOT `child.is_escalated` AND (`child.is_closed` OR `current_stage(child) is None`). Returns False for non-leaf, in-progress, escalated, or open-and-non-exhausted children. The predicate is body-aligned with §2.2 default manifests: `task`/`chore`/`bug` end at `merge`; `refactor`/`feature` end at `merge` after `pr`; all auto-close at manifest exhaustion via §2.1 invariant 8 (which writes `closed_at`), and the predicate fires on `child.is_closed`. The `current_stage is None AND NOT child.is_closed` branch is reachable ONLY (i) on the §2.2 migration-234 transaction boundary before the acceptance 2.2.31 close-pass commits, or (ii) inside synthetic test fixtures that bypass `complete_stage`. symbol: `gobby.dispatch.rules.is_child_parked`. test: `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_true_for_terminal_closed_leaf`, `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_true_for_each_default_manifest_terminal_stage` (parameterized over `[development, merge, pr]` as last stage — the surviving 11-stage default manifests), `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_false_for_in_progress_leaf`, `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_false_for_escalated_leaf`, `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_false_for_non_leaf`, `tests/dispatch/test_rules_stage_native.py::test_is_child_parked_synthetic_branch_is_test_only_or_migration_window`.
- 3.1.19 — `all_leaves_holistic_rule` fires on an epic whose `current_stage == ('holistic_qa', 'ready')` AND every direct child satisfies `is_child_parked(child) OR child.is_closed`. The rule emits `StartStageAction(task_id, stage_name='holistic_qa')`, transitioning the epic's `holistic_qa` row `ready → in_progress` via `StageStatesManager.start_stage`. The rule reaches into each child's denormalized `child.stages` (already loaded by `reload_candidate` per 3.1.3) — no leaf-side rule, no transient signal, no extra SQL round-trip per heartbeat. Cross-heartbeat correctness: a child that closed at its terminal manifest stage (typically `merge.done` for merge-bearing leaves, or `prd.done` / `architecture.done` for research-terminal types) in heartbeat N still satisfies `is_child_parked` at heartbeat N+M for any M≥0, so the parent advances `holistic_qa` whenever the heartbeat happens to scan it next, without any race window. The rule does NOT fire when any child is still working (some `is_child_parked` returns False AND that child is not closed). test: `tests/dispatch/test_rules_stage_native.py::test_all_leaves_holistic_advances_epic_when_all_children_parked`, `tests/dispatch/test_rules_stage_native.py::test_all_leaves_holistic_does_not_fire_when_any_child_in_progress`, `tests/dispatch/test_rules_stage_native.py::test_all_leaves_holistic_advances_with_mix_of_parked_and_terminal_closed_children`, `tests/dispatch/test_rules_stage_native.py::test_all_leaves_holistic_durable_across_heartbeats`.
- 3.1.20 — `holistic_qa_rule` fires on `current_stage == ('holistic_qa', 'in_progress')` and emits a spawn action for the `holistic-reviewer` agent (the agent does work AND review internally, producing `in_progress → needs_review → review_approved` from inside its run via `submit_for_review` then `approve_review`). test: `tests/dispatch/test_rules_stage_native.py::test_holistic_qa_rule_spawns_holistic_reviewer`.
- 3.1.20a — `holistic_qa_review_rule` fires on `current_stage == ('holistic_qa', 'needs_review')` and re-spawns the `holistic-reviewer` agent as a defensive resume hook for the rare case where the agent crashed or exited between its internal `submit_for_review` and `approve_review` calls. Without this rule, `list_automation_candidates` (which matches `needs_review`) would surface the row indefinitely with no consumer. The agent's prompt detects the resume context (manifest row already at `needs_review`) and proceeds straight to internal review + `approve_review` without redoing the work pass. test: `tests/dispatch/test_rules_stage_native.py::test_holistic_qa_review_rule_respawns_on_needs_review`, `tests/dispatch/test_rules_stage_native.py::test_holistic_qa_review_rule_only_fires_on_needs_review`, `tests/dispatch/test_rules_stage_native.py::test_holistic_qa_review_rule_passes_resume_flag_to_agent_prompt`, `tests/dispatch/test_rules_stage_native.py::test_holistic_qa_needs_review_not_stranded_after_agent_crash`.
- 3.1.20b — `holistic_qa_advance_rule` fires on `current_stage == ('holistic_qa', 'review_approved')` and emits `AdvanceStageAction(task_id, stage_name='holistic_qa', method='complete_stage')`; the executor calls `complete_stage('holistic_qa')`. The rule itself never invokes `StageStatesManager`. test: `tests/dispatch/test_rules_stage_native.py::test_holistic_qa_advance_rule_emits_advance_stage_action`, `tests/dispatch/test_rules_stage_native.py::test_holistic_qa_advance_rule_advances_via_executor`.
- 3.1.21 — `pr_work_rule`, `pr_review_rule`, `pr_advance_rule` are registered placeholders in this deliverable; full bodies (including `record_pr_verdict` mapping to `approve_review`/`reject_review`) land in Phase 4.1. The placeholder bodies emit `EscalateAction(reason='pr_no_agent')` for `pr_work_rule`, no-op for `pr_review_rule` (external review), and emit `AdvanceStageAction(task_id, stage_name='pr', method='complete_stage')` for `pr_advance_rule`. None of the three placeholder rules invokes `StageStatesManager` directly. test: `tests/dispatch/test_rules_stage_native.py::test_pr_rules_registered_placeholders`, `tests/dispatch/test_rules_stage_native.py::test_pr_advance_rule_emits_advance_stage_action`.
- 3.1.22 — `merge_rule` (registered placeholder in this deliverable; full body in Phase 4.2) is present in the rules list at the post-`pr_advance_rule` position; Phase 4.2 supplies its body. Single rule because `merge.review_policy=none`. test: `tests/dispatch/test_rules_stage_native.py::test_merge_rule_registered_placeholder`.
- 3.1.23 — `disabled_agent_escalation_rule` (round-14 F2; round-9 F2 sharpening): fires when `current_stage.state == 'ready'` AND `current_stage.name NOT IN {'expansion', 'pr', 'development', 'holistic_qa'}` AND `registry.default_agent IS NOT NULL` AND that agent resolves to a bundled agent with `enabled: false` AND no other rule has produced an action this scan. Action: `EscalateAction(task_id=task.id, reason=f'{current_stage.name}_no_agent')` — sets `is_escalated=1`, writes the escalation reason. Ordered AFTER `auto_advance_ready_rule` (which short-circuits the chain on enabled-agent stages and on the two `default_agent IS NULL` allowlist stages `expansion` and `pr`) and BEFORE the per-stage in-progress rules. The exclude set `{'expansion', 'pr', 'development', 'holistic_qa'}` is load-bearing: `expansion` and `pr` are non-agent work owners (`expansion` runs as a pipeline action, `pr` is owned by external/PR-Agent under #13552) whose `default_agent IS NULL` is the EXPECTED state and MUST NOT trigger an escalation; `development` and `holistic_qa` are owned by `development_isolation_rule` and `all_leaves_holistic_rule` respectively. The `default_agent IS NOT NULL` predicate is also load-bearing: when the registry slot is unpopulated the rule does NOT fire (those cases collapse to the allowlist above), so the escalation reasons are exclusively the four discovery-stage placeholder cases under §1.3 — `ideation_no_agent`, `research_no_agent`, `architecture_no_agent`, `prd_no_agent`. Without this rule, ready stages with disabled placeholder agents would stall forever — making §5.1.4's `research_spike` / `prd_doc` / `architecture_doc` walks unreachable under dispatcher control. symbol: `gobby.dispatch.rules.disabled_agent_escalation_rule`. test: `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_fires_on_ideation_with_disabled_analyst` (parameterized over the four (stage, slug) pairs from §1.3: ideation/analyst, research/researcher, architecture/architect, prd/product-manager), `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_skips_when_agent_enabled`, `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_skips_development_and_holistic_qa`, `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_skips_expansion_and_pr_with_null_default_agent`, `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_does_not_fire_on_default_agent_is_null` (asserts the rule never fires on `default_agent IS NULL` regardless of stage; complements the allowlist above), `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_does_not_fire_when_state_in_progress`, `tests/dispatch/test_rules_stage_native.py::test_disabled_agent_escalation_rule_emits_correct_reason_per_stage`.

### 3.2 Manifest resolution at build time + readiness projections rewrite [category: code] (depends: 2.1, 2.2, 3.1)

`kind: deliverable`

Target: `src/gobby/build/service.py`, `src/gobby/storage/tasks/_blocking.py`, `src/gobby/storage/tasks/_aggregates.py`, `src/gobby/storage/tasks/_queries.py` (existing — `list_ready_tasks` at lines 171–284, `list_blocked_tasks` at lines 287–348; both rewritten to project from manifest reads while preserving the existing parent-blocker carve-outs), `src/gobby/storage/tasks/_manager.py` (existing — `LocalTaskManager.list_ready_tasks` at lines 754–786, `LocalTaskManager.list_blocked_tasks` at lines 788–820; thin wrappers updated for any signature/return-shape changes from the projection rewrite), `src/gobby/mcp_proxy/tools/task_readiness.py` (existing — `list_ready_tasks` MCP tool at line 397; updated for any response-shape changes propagating from the manager wrapper), `src/gobby/dispatch/dispatcher.py`

`gobby build` flow rewrite. Build resolves the task's manifest before the dispatcher ever sees it:

1. Read `task.task_type`, fetch defaults via `StageRegistryManager.list_default_stages(task_type)`.
2. Apply CLI/MCP/HTTP flag overrides (`--stages`, `--add-stage`, `--skip-stage`, profiles `quick|full|full-yolo`).
3. Call `StageStatesManager.initialize_manifest(task_id, resolved_stages, ...)`.
4. Set `allow_automation=True`, `yolo` per profile, `isolation` per profile.
5. Return `BuildResult` with the resolved manifest in `manifest` field for caller display.

Profile → flag bundle resolution:

```python
PROFILE_BUNDLES: dict[str, ProfileBundle] = {
    "quick":     ProfileBundle(skip=["research", "holistic_qa"]),  # under the new contract, planning/expansion/development reviews are state on the same row, not separate stages; `quick` skips both the discovery `research` stage (zero-value for code-only iterations) and the aggregate-review `holistic_qa` stage to keep the leaf walk tight
    "review":    ProfileBundle(skip=[]),  # default
    "full":      ProfileBundle(skip=[]),
    "full-yolo": ProfileBundle(skip=[], yolo=True),
}
```

Profile bundles resolve to `--skip-stage` arguments that get applied alongside any explicit `--skip-stage` flags.

Readiness projections rewrite:

- `list_ready_tasks` (storage layer) — old: `WHERE status='open' AND ...`. New: `WHERE NOT is_closed AND NOT is_escalated AND NO unresolved blocker AND current_stage IS NOT NULL AND current_stage.state IN ('ready','in_progress')`. Implementation: subquery against `task_stage_states` for `current_stage`; filter by `closed_at IS NULL`, `is_escalated = 0` (Phase 5 backfills the column), and the existing blocker join.
- `list_blocked_tasks` — old: relied on `status='escalated'`-as-block plus dependency checks. New: `is_escalated = 1 OR active_blocked_by IS NOT EMPTY`. Excludes parent tasks blocked only by their own descendants — preserve the existing SQL-inline completion-block exclusion (the `parent_task_id` carve-out at `src/gobby/storage/tasks/_queries.py:211,237,314` and `src/gobby/storage/tasks/_aggregates.py:81,103,164`; there is no Python helper named `_filter_completion_blocks`, the carve-out is a `WHERE` clause clause repeated across queries).
- `suggest_next_task` — same readiness criteria as `list_ready_tasks`, sorted by priority + age.
- `list_automation_candidates` — old: `WHERE allow_automation=true AND status IN ('open','in_progress','needs_review','review_approved')`. New: `WHERE allow_automation=true AND NOT is_closed AND NOT is_escalated AND current_stage.state IN ('ready','in_progress','needs_review','review_approved')`. All four states are dispatcher-actionable: `ready` (work rule fires), `in_progress` (mid-work cap/timeout checks; also the source state for `<stage>_review_rule` once the worker submits), `needs_review` (`<stage>_review_rule` spawns the reviewer agent), `review_approved` (`<stage>_advance_rule` advances the manifest). Excluding any of these would silently disable the corresponding rule. Manifest-exhausted leaves (`current_stage IS NULL`) are intentionally excluded — their work is complete and they need no dispatcher attention. The `done` state is excluded because dispatcher actions on a `done` row are never needed; the next-stage row owns the next action. The parent epic remains automation-eligible because its own `holistic_qa.ready` (or whichever non-terminal state) keeps `current_stage` populated; `all_leaves_holistic_rule` reaches into each child's denormalized `child.stages` via `is_child_parked(child)` (acceptance 3.1.18) to gate the parent's transition. No leaf scan is required for parking detection.

For each rewritten projection, write a contract test that runs the OLD model on a fixture DB, runs the NEW model on the same fixture DB after backfill, and asserts identical task ID sets. This is the load-bearing equivalence guarantee from the strategy plan.

`reload_candidate` (`src/gobby/dispatch/dispatcher.py:145-156`) loads `Task.stages` via a JOIN against `task_stage_states ORDER BY position` and packs into `Task.stages` tuple. The SELECT projects `task_stage_states.updated_at` into each `StageState.updated_at` field per the §2.1 dataclass shape (acceptance 2.1.8); the §3.3 mutex snapshot reads it from `Task.stages[<current_idx>].updated_at`.

**Acceptance:**

- 3.2.1 — `gobby build` writes the resolved manifest via `initialize_manifest` and returns it in `BuildResult`. file: `src/gobby/build/service.py`. test: `tests/build/test_build_resolves_manifest.py::test_default_manifest`.
- 3.2.2 — Profile bundles `quick`, `review`, `full`, `full-yolo` resolve to declared skip lists against the surviving 11-stage registry (the dropped stages `adversarial_review`, `expansion_qa`, `code_review_qa` are not skip-targets — they no longer exist). test: `tests/build/test_build_profiles.py::test_quick_skips_research_and_holistic_qa`, `tests/build/test_build_profiles.py::test_review_skips_only_optional_review_stages`, `tests/build/test_build_profiles.py::test_full_skips_nothing`, `tests/build/test_build_profiles.py::test_full_yolo_skips_nothing_and_sets_yolo`, `tests/build/test_build_profiles.py::test_no_profile_references_dropped_review_stages`.
- 3.2.3 — `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates` rewritten to manifest reads. `list_automation_candidates` matches all four dispatcher-actionable states (`ready`, `in_progress`, `needs_review`, `review_approved`); `list_ready_tasks` and `suggest_next_task` keep human-facing `ready` / `in_progress` only. file: `src/gobby/storage/tasks/_blocking.py`, `src/gobby/storage/tasks/_aggregates.py`. test: `tests/storage/tasks/test_readiness_equivalence.py::test_old_vs_new_identical`, `tests/storage/tasks/test_automation_candidates_states.py::test_includes_needs_review`, `tests/storage/tasks/test_automation_candidates_states.py::test_includes_review_approved`, `tests/storage/tasks/test_automation_candidates_states.py::test_excludes_done_and_null_current_stage`, `tests/dispatch/test_review_states_reachable.py::test_planning_needs_review_spawns_reviewer`, `tests/dispatch/test_review_states_reachable.py::test_planning_review_approved_advances_to_done`.
- 3.2.4 — `reload_candidate` populates `Task.stages` in a single SQL round-trip. test: `tests/dispatch/test_reload_candidate_n1.py::test_no_n1_query`.

### 3.3 Cut over `RuntimeDispatchMutex` candidate-snapshot check from `(lifecycle, status)` to `(stage_name, stage_state, updated_at)` [category: code] (depends: 3.1, 3.2)

`kind: deliverable`

Target: `src/gobby/dispatch/mutex.py`, `src/gobby/dispatch/dispatcher.py`

`RuntimeDispatchMutex` (defined at `src/gobby/dispatch/mutex.py:27`) currently carries `expected_lifecycle` and `expected_status` fields plus a `candidate_tuple_matches()` method that the heartbeat (`run_heartbeat` in `dispatcher.py`) uses to detect when a candidate's state changed between scan and mutex acquisition. After Phase 5.3 drops `lifecycle`, `lifecycle_stage`, and active `status` from `tasks`, this race check is either broken (FK reads return `NULL`) or silently disabled (default-tuple match). The dispatcher's correctness guarantee — "skip a candidate whose state changed under us" — vanishes.

Cutover: replace the legacy tuple with a stage manifest snapshot. The new fields are `expected_stage_name`, `expected_stage_state`, and `expected_stage_updated_at` (the `task_stage_states.updated_at` value of the candidate's `current_stage` at scan time). The renamed method `candidate_stage_snapshot_matches(current_stage_name, current_stage_state, current_stage_updated_at)` returns True iff all three match. The heartbeat passes `candidate.stages[<current_stage_idx>].updated_at` as the third arg.

`expected_stage_state` is typed as `Literal["ready","in_progress","needs_review","review_approved"] | None`. These are exactly the four dispatcher-actionable states matched by §3.2's `list_automation_candidates` projection — the snapshot must cover each so reviewer-spawn (`<stage>_review_rule` racing against the worker that submitted) and review-approved-advance (`<stage>_advance_rule` racing against `complete_stage` from a different writer) both get the staleness check. `done` is excluded because `current_stage` is the leftmost non-done row by definition: a `done` row is never the dispatcher's current stage. Manifest-exhausted candidates carry `current_stage is None` (and the helper falls back to `expected_stage_*=None`, which is treated as a mismatch on any non-None observation).

```python
@dataclass(frozen=True, slots=True)
class RuntimeDispatchMutex:
    storage: TaskDispatchMutexManager
    task_id: str
    holder: str
    action_kind: str
    ttl_seconds: int = 30
    expected_stage_name: str | None = None
    expected_stage_state: Literal["ready","in_progress","needs_review","review_approved"] | None = None
    expected_stage_updated_at: str | None = None

    def candidate_stage_snapshot_matches(
        self,
        current_stage_name: str | None,
        current_stage_state: str | None,
        current_stage_updated_at: str | None,
    ) -> bool:
        """True iff the candidate's current_stage row is unchanged from scan time.

        All three expected_* fields must be non-None and equal to the passed
        values. Any field None on either side is a mismatch (forces re-scan).
        """
```

The heartbeat call site (`run_heartbeat` in `dispatcher.py`) passes the candidate's current stage snapshot when constructing the mutex:

```python
current_stage = candidate.current_stage  # leftmost non-done row, or None
mutex = RuntimeDispatchMutex(
    storage=storage,
    task_id=candidate.id,
    holder=holder,
    action_kind=f"dispatch:{action.kind}",
    ttl_seconds=30,
    expected_stage_name=current_stage.name if current_stage else None,
    expected_stage_state=current_stage.state if current_stage else None,
    expected_stage_updated_at=current_stage.updated_at if current_stage else None,
)
```

When the dispatcher acquires the mutex, it re-reads the candidate's current stage and calls `candidate_stage_snapshot_matches`; if it returns False, the dispatch is aborted for this heartbeat (next heartbeat re-scans).

Phase 2.1 (acceptance 2.1.6) already requires `StageStatesManager` mutators to wrap their writes in `RuntimeDispatchMutex`. Those call sites pre-acquired the mutex via the wrapper class but did not yet check the candidate snapshot — they relied on the row-level lock for serialization. After this deliverable, those call sites pass `expected_stage_*` fields too, giving the same staleness check as the dispatcher heartbeat.

This deliverable depends on Phase 3.1 (manifest-native rules supply `Task.stages`) and Phase 3.2 (`reload_candidate` populates `Task.stages.updated_at`). Phase 5.3 cannot drop `lifecycle`/`status` from `tasks` until this deliverable lands; §5.3's `depends_on` already includes `3.1, 3.2` and the F4 fix re-pins it to include this deliverable explicitly.

**Acceptance:**

- 3.3.1 — `RuntimeDispatchMutex` API exposes `expected_stage_name`, `expected_stage_state`, `expected_stage_updated_at` fields and `candidate_stage_snapshot_matches()` method; legacy `expected_lifecycle`, `expected_status`, and `candidate_tuple_matches()` are removed. file: `src/gobby/dispatch/mutex.py`. symbol: `gobby.dispatch.mutex.RuntimeDispatchMutex.candidate_stage_snapshot_matches`. test: `tests/dispatch/test_runtime_dispatch_mutex.py::test_snapshot_match_api`.
- 3.3.2 — `run_heartbeat` constructs the mutex with the candidate's current-stage snapshot (`name`, `state`, `updated_at`); on mutex acquisition the heartbeat re-reads the row and compares. file: `src/gobby/dispatch/dispatcher.py`. test: `tests/dispatch/test_runtime_dispatch_mutex.py::test_heartbeat_passes_snapshot`.
- 3.3.3 — Stale-candidate test: candidate is scanned at `(development, in_progress, T0)`; before mutex acquisition, another writer transitions the row to `(development, ready, T1)` (a `fail_stage` from concurrent rejection). The mutex acquires the row-lock, calls `candidate_stage_snapshot_matches`, returns False, and the dispatch action is dropped without any side effect; next heartbeat re-scans and produces a new candidate. The same test pattern is parameterized over the two review-state races: candidate scanned at `(planning, needs_review, T0)` and concurrently moved to `(planning, ready, T1)` (reject_review race against `planning_review_rule`'s reviewer spawn); candidate scanned at `(planning, review_approved, T0)` and concurrently moved to `(planning, done, T1)` (`complete_stage` race against `planning_advance_rule`). Both must trigger snapshot mismatch and abort. test: `tests/dispatch/test_runtime_dispatch_mutex.py::test_stale_snapshot_aborts_dispatch`, `tests/dispatch/test_runtime_dispatch_mutex.py::test_stale_snapshot_aborts_reviewer_spawn_when_state_changes_under_us`, `tests/dispatch/test_runtime_dispatch_mutex.py::test_stale_snapshot_aborts_advance_when_state_changes_under_us`, `tests/dispatch/test_runtime_dispatch_mutex.py::test_literal_accepts_four_actionable_states_only`, `tests/dispatch/test_runtime_dispatch_mutex.py::test_literal_rejects_done`.
- 3.3.4 — `StageStatesManager` mutator call sites (Phase 2.1 acceptance 2.1.6) pass `expected_stage_*` fields when constructing the mutex; concurrent transitions on the same task observe each other's state changes via the snapshot check rather than only the row lock. test: `tests/storage/tasks/test_stage_states_concurrency.py::test_mutex_snapshot_check_on_mutators`.

## P4 PR / Merge / Review Stage Cutover

`kind: framing`

**Goal**: Land the stage-native PR and merge rules with their delivery artifacts. After Phase 4, #13552 (PR-Agent) and #13560-class merge work can target the new stage contract.

### 4.1 PR stage rule + delivery artifacts [category: code] (depends: 3.1)

`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`, `src/gobby/storage/tasks/_artifacts.py`, `src/gobby/mcp_proxy/tools/tasks/_stages.py`

Wire `pr_work_rule`, `pr_review_rule`, `pr_advance_rule` (placeholders registered in Phase 3.1) with their full bodies. PR is `review_policy=required` with a blank `reviewer_agent` slot — external review is owned by PR-Agent (#13552) or a human reviewer; this epic does NOT register a PR-Agent.

Stage transitions during PR work:

1. `holistic_qa` reaches `done` (epic) or `development.review_approved → done` is followed by `auto_advance_ready_rule` (leaf) → `pr.state` transitions `ready → in_progress` via `auto_advance_ready_rule` (or operator-driven for human-gated PR work).
2. PR opened: agent or operator calls `record_pr_opened(task_id, pr_url, github_pr_number?)` (registered in Phase 2.3) to write `pr_url` and `github_pr_number` artifacts without changing stage state. Then calls `mark_task_needs_review(task_id)` (rewired in Phase 2.6 to `submit_for_review`) to transition `pr.in_progress → pr.needs_review`, signaling that the PR is open and awaiting external review.
3. PR review verdict: `record_pr_verdict(task_id, verdict='approved'|'rejected'|'needs_changes', findings, report_ref?)`. Writes `structured_pr_verdict` (JSON) and `pr_review_report`. Then maps to a stage-axis transition:
    - `verdict='approved'` → `StageStatesManager.approve_review(task_id, 'pr')`. Transition: `pr.needs_review → pr.review_approved`. The PR is approved but NOT yet merged; `pr.review_approved` is the durable holding state queue position.
    - `verdict='rejected'` or `verdict='needs_changes'` → `StageStatesManager.reject_review(task_id, 'pr', reason=findings)`. Transition: `pr.needs_review → pr.ready`. Increments `review_round_count`. Per §2.1 contract, when `review_round_count >= effective_max_review_rounds`, `reject_review` escalates with reason `pr_review_failed:max` instead of returning to ready. The effective cap is resolved as `pr_state_row.max_review_rounds ?? registry.default_max_review_rounds` (registry default 5 per §1.1); the legacy `task_artifacts.max_review_rounds` column was migrated into the per-stage column during the §2.2 backfill (PR-stage cap inherits from the registry default unless explicitly overridden via `gobby build --stage pr:max_review_rounds=N`).
4. `pr_advance_rule` fires when `current_stage == ('pr', 'review_approved')` and calls `complete_stage('pr')`. Transition: `pr.review_approved → pr.done`. The next stage (`merge.ready`) becomes the new current_stage; `auto_advance_ready_rule` promotes it on the next heartbeat (merge.review_policy=none, no reviewer to wait on).

`pr_work_rule`, `pr_review_rule`, `pr_advance_rule` bodies:

```python
def pr_work_rule(task: Task, context: RuleContext) -> Action | None:
    stage = current_stage(task)
    if stage is None or stage.name != "pr" or stage.state != "in_progress":
        return None
    if not _has_pr_agent(context):
        # Until #13552 lands, escalate so a human picks up PR-opening work
        return EscalateAction(task_id=task.id, reason="pr_no_agent")
    return _spawn_stage_agent(task, stage, context, "pr-agent")


def pr_review_rule(task: Task, context: RuleContext) -> Action | None:
    """No-op: external review by PR-Agent or human; verdict arrives via
    record_pr_verdict which calls approve_review or reject_review directly.
    The rule exists only to keep the row visible in queries and surface
    the queue position to operators."""
    stage = current_stage(task)
    if stage is None or stage.name != "pr" or stage.state != "needs_review":
        return None
    return None  # external reviewer drives the next transition


def pr_advance_rule(task: Task, context: RuleContext) -> Action | None:
    stage = current_stage(task)
    if stage is None or stage.name != "pr" or stage.state != "review_approved":
        return None
    return AdvanceStageAction(
        task_id=task.id, stage_name="pr", method="complete_stage",
        by_session_id="dispatcher",
    )
```

`_has_pr_agent` checks the agent registry for a stage-aware `pr-agent`; if missing, `pr_work_rule` escalates so #13552's owner can pick up the work.

**Acceptance:**

- 4.1.1 — `pr_work_rule`, `pr_review_rule`, `pr_advance_rule` registered in the rules list at the right positions (work → review → advance, after `holistic_qa_advance_rule`, before `merge_rule`). file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_pr_rules.py::test_three_pr_rules_in_correct_order`.
- 4.1.2 — `record_pr_verdict` with `verdict='approved'` calls `StageStatesManager.approve_review(task_id, 'pr')` (transition `pr.needs_review → pr.review_approved`); writes `structured_pr_verdict` and `pr_review_report` artifacts in the same write path. The tool does NOT call `complete_stage` or advance to merge — that's `pr_advance_rule`'s job on the next heartbeat. test: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_calls_approve_review_no_advance`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_approved_writes_artifacts`.
- 4.1.2a — `record_pr_verdict` with `verdict='rejected'` or `verdict='needs_changes'` calls `StageStatesManager.reject_review(task_id, 'pr', reason=findings)` (transition `pr.needs_review → pr.ready`, increments `review_round_count`). When `review_round_count >= effective_max_review_rounds`, `reject_review` escalates instead. test: `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_rejected_calls_reject_review`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_needs_changes_calls_reject_review`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_rejected_over_cap_escalates`.
- 4.1.3 — `pr_advance_rule` firing at `pr.review_approved` calls `complete_stage('pr')`, which advances to `pr.done` and lets `auto_advance_ready_rule` promote `merge.ready → merge.in_progress` on the next heartbeat. test: `tests/dispatch/test_pr_to_merge_advance.py::test_pr_advance_then_merge_auto_start`.
- 4.1.4 — Without a registered `pr-agent`, `pr_work_rule` escalates with reason `pr_no_agent`. `pr_review_rule` is a no-op (no spawn) — external review drives the next transition via `record_pr_verdict`. test: `tests/dispatch/test_pr_rules.py::test_pr_work_escalates_when_no_agent`, `tests/dispatch/test_pr_rules.py::test_pr_review_rule_is_no_op`.
- 4.1.5 — PR rejection cap source under the new contract is the per-stage `task_stage_states.max_review_rounds` column (nullable; null inherits `task_stages_registry.default_max_review_rounds`, default 5). The legacy `task_artifacts.max_review_rounds` column was migrated into the per-stage column during §2.2 backfill. `record_pr_verdict(verdict='rejected')` calls `reject_review('pr', reason=findings)`; under the cap (`review_round_count < effective_max_review_rounds`) the stage transitions `needs_review → ready`; at-or-over the cap, `reject_review` escalates with reason `pr_review_failed:max`. Build-time override available via `gobby build <ref> --stage pr:max_review_rounds=N`. behavior: "PR review cap inherits from registry default; per-stage override works" verified in `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_rejected_under_cap_returns_to_ready`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_rejected_over_cap_escalates`, `tests/mcp_proxy/tools/tasks/test_record_pr_verdict.py::test_per_stage_max_review_rounds_override_works`.
- 4.1.6 — End-to-end PR flow walks `pr.ready → pr.in_progress → pr.needs_review → pr.review_approved → pr.done → merge.ready → merge.in_progress` driven by the dispatcher + tool calls, including a rejected-then-approved cycle: `record_pr_verdict('rejected')` returns to `pr.ready` with `review_round_count=1`; `auto_advance_ready_rule` promotes back to `pr.in_progress` (incrementing `work_attempt_count`); a second `record_pr_verdict('approved')` advances to `pr.review_approved` and on through. test: `tests/dispatch/test_pr_full_walk.py::test_pr_lifecycle_with_rejection_then_approval`.

### 4.2 Merge stage rule + delivery artifacts [category: code] (depends: 4.1)

`kind: deliverable`

Target: `src/gobby/dispatch/rules.py`, `src/gobby/mcp_proxy/tools/tasks/_stages.py`

Add `merge_rule`. Mirrors `pr_rule`'s escalate-without-agent fallback. The terminal close is NOT the merge rule's responsibility — it is owned by `StageStatesManager.complete_stage` per §2.1 invariant 8 / acceptance 2.1.9 (the generic manifest-exhausted close path). When `record_merge_result` calls `complete_stage('merge', commit_sha=merge_sha)`, the merge row becomes `done`, the manifest is exhausted, and `complete_stage` closes the task atomically in the same transaction. The merge stage uses the same generic close path as `research_spike`/`prd_doc`/`architecture_doc`; the only merge-specific behavior is the `commit_sha = merge_sha` argument and the artifact writes (`merge_commit_sha`, `merge_campaign_report`).

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
    merge_campaign_report to task_artifacts; calls
    complete_stage('merge', commit_sha=merge_sha). Per §2.1 invariant 8 /
    acceptance 2.1.9 / 2.1.10, completing the highest-position manifest
    row (the merge row, which is terminal for merge-bearing manifests)
    atomically closes the task in the same DB transaction by calling
    `_close_task_in_txn(..., cascade_descendants=True, reason='manifest_exhausted', commit_sha=merge_sha)`.
    The cascade=True is passed because `stage_name == 'merge'` (per the
    round-7/-8 caller-cascade rule); this is the cascade-aware close that
    replaces the legacy `mark_task_merged` / `_cascade_merged_close`
    behavior. The public `close_task(...)` API is NOT invoked anywhere
    on this path — the merge close is `complete_stage` →
    `_close_task_in_txn` directly, with cascade enabled by the merge
    branch. There is NO merge-specific close call beyond the generic
    helper invocation.

    Failure path: failure_reason required. Writes merge_campaign_report;
    calls fail_stage('merge', reason=failure_reason). Per §2.1's contract,
    fail_stage transitions in_progress → ready WITHOUT changing
    work_attempt_count (the just-failed attempt is already counted by the
    start_stage that started it; the next retry's start_stage is the next
    increment). Cap escalation: fail_stage compares
    `merge.work_attempt_count` to the effective merge cap, resolved as
    `state_row.max_work_attempts ?? registry_row.default_max_work_attempts`
    (registry default `3` for `merge` per §1.2). The
    `task_artifacts.max_merge_attempts` legacy column is NOT read on this
    path — it was migrated into `task_stage_states.max_work_attempts`
    during the Phase 2.2 backfill and is dropped in Phase 5.3. On
    `work_attempt_count >= effective_max_work_attempts`, fail_stage
    escalates with reason 'merge_failed:max' instead of transitioning back
    to ready. The cap predicate is `>=`, not `>`: a task that has started
    the merge stage `cap` times and just failed the cap-th attempt
    escalates without another retry.
    """
```

The cascade-close behavior from `mark_task_merged` (`src/gobby/storage/tasks/_transitions.py:661-680`) MUST be preserved and is reused via the §2.1 generic terminal-close path: `complete_stage(stage_name='merge', ...)` calls `_close_task_in_txn(..., cascade_descendants=True, ...)` per the round-7/-8 caller-cascade rule (acceptance 2.1.10). The cascade implementation lives ONCE in `_close_task_in_txn`; both the legacy `mark_task_merged` cascade behavior and any future cascade-needing terminal stage inherit from there. The public `close_task` API always passes `cascade_descendants=False` and is NOT invoked on the merge-close path — Phase 4.2 reaches `_close_task_in_txn` exclusively through `complete_stage`'s merge branch. No new close path is added by Phase 4.2; the merge close is one application of the §2.1 invariant with cascade=True.

`expansion_review_rule`, `development_review_rule`, `holistic_qa_rule` — each checks for its reviewer agent in the context and either spawns or escalates with `<stage>_no_reviewer`. These rules already exist in skeleton form from Phase 3.1; this section extends them to use the same `_has_<agent>(context)` pattern as `pr_work_rule`/`merge_rule` so missing reviewer agents surface uniformly. (`expansion-qa`, `qa-reviewer`, and `holistic-reviewer` are bundled and enabled by default — these escalation paths cover the operator-disabled or registry-missing failure modes.)

**Acceptance:**

- 4.2.1 — `merge_rule` registered in the rules list. file: `src/gobby/dispatch/rules.py`. test: `tests/dispatch/test_merge_rule.py::test_merge_rule_in_list`.
- 4.2.2 — `record_merge_result(merge_sha=...)` writes `merge_commit_sha` + `merge_campaign_report`, then calls `complete_stage('merge', commit_sha=merge_sha)`. The §2.1 invariant 8 generic terminal-close path (acceptance 2.1.9 / 2.1.10) closes the task atomically in the same transaction by calling `_close_task_in_txn(..., reason='manifest_exhausted', commit_sha=merge_sha, cascade_descendants=True)`; cascade=True comes from the round-7/-8 caller-cascade rule that selects `stage_name == 'merge'`. Public `close_task(...)` is NOT invoked on this path. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_success_closes_task_via_terminal_close`, `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_success_close_uses_manifest_exhausted_reason_and_merge_sha`, `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_success_close_uses_cascade_descendants_true`, `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_success_does_not_invoke_public_close_task`.
- 4.2.3 — `record_merge_result(failure_reason=...)` fails the stage; over the cap, escalates. test: `tests/mcp_proxy/tools/tasks/test_record_merge_result.py::test_failure_path`.
- 4.2.4 — `expansion_review_rule`, `development_review_rule`, `holistic_qa_rule` all surface missing-reviewer-agent escalations with stage-specific reason codes (`expansion_no_reviewer`, `development_no_reviewer`, `holistic_qa_no_reviewer`). test: `tests/dispatch/test_review_rules_no_agent.py::test_each_review_rule_escalates_specifically_when_reviewer_missing`.
- 4.2.5 — End-to-end stage chain `holistic_qa.review_approved → holistic_qa.done → pr.ready → pr.in_progress → pr.needs_review → pr.review_approved → pr.done → merge.ready → merge.in_progress → merge.done → task closed` walks correctly across heartbeats. test: `tests/dispatch/test_delivery_chain.py::test_full_delivery_chain_5_state`.

## P5 Task Type Expansion + Legacy Removal

`kind: framing`

**Goal**: Add the new task types, promote `is_escalated` to a first-class column, and rip out the legacy `lifecycle`/`status`/`lifecycle_stage` columns and the projection helpers. This phase closes the legacy model.

### 5.1 New task types + default-stages seed [category: code] (depends: 2.1, 2.2)

`kind: deliverable`

Target: `src/gobby/storage/tasks/_models.py`, `src/gobby/cli/tasks/crud.py`, `src/gobby/storage/migrations.py`, `src/gobby/servers/routes/tasks.py` (existing — `TaskCreateRequest` at lines 32–53; extends `task_type` validation to accept the four new types), `src/gobby/mcp_proxy/tools/tasks/_crud.py` (existing — `create_task` MCP tool at lines 34–239; extends inputSchema's `task_type` enum to accept the four new types)

**Stage-defaults ownership (round-21 F1):** Migration 235 is the SOLE owner of the four new `task_type_default_stages` bundles. The bundled stage-registry file is NOT a target of this deliverable: §1.1 owns that file's exact 11-stage body byte-for-byte (acceptance 1.1.6a), and §1.2's `StageRegistryLoader` is stage-registry-only — task-type defaults have never been part of the bundled YAML's schema. The four new bundles are seeded directly into the `task_type_default_stages` table by migration 235; no YAML edit is required and no edit is permitted (touching the §1.1-owned bundled registry file here would break 1.1.6a's byte-equality regression test).

Add four new task types and their default-stages bundles. Migration version 235:

```python
NEW_TASK_TYPE_DEFAULTS = {
    "simple_fix":       ["development", "pr", "merge"],
    "research_spike":   ["ideation", "research", "prd"],            # terminal at prd, no merge
    "architecture_doc": ["research", "architecture"],               # terminal at architecture
    "prd_doc":          ["ideation", "prd"],                        # terminal at prd
}
```

Update `Task.task_type` validation in `_models.py` to accept the new values. The current inline comment (`src/gobby/storage/tasks/_models.py:163`) lists `bug, feature, task, epic, chore, refactor`; extend with the four new types. Add a `VALID_TASK_TYPES` module constant alongside existing validations (or inline as a frozenset literal — match nearby patterns).

Update `gobby tasks create --type <type>` Click choices in `src/gobby/cli/tasks/crud.py` to include the new types. Same for `TaskCreateRequest.task_type` validation in HTTP route models and the `create_task` MCP tool's `inputSchema`.

For research-terminal types (`research_spike`, `prd_doc`, `architecture_doc`): terminal-close is the §2.1 invariant 8 generic manifest-exhausted path (acceptance 2.1.9), NOT a Phase 4.2 dispatcher rule. When `complete_stage` is called on the highest-position row of any manifest (whatever its stage name — `prd` for research_spike/prd_doc, `architecture` for architecture_doc, `merge` for merge-bearing types), the task closes atomically in the same transaction with `reason='manifest_exhausted'`. There is no separate close path for research-terminal types. Add unit test fixtures for each: `research_spike` walks `ideation → research → prd → closed`; `prd_doc` walks `ideation → prd → closed`; `architecture_doc` walks `research → architecture → closed`.

**Acceptance:**

- 5.1.1 — Four new task types accepted by `Task.task_type` validation. file: `src/gobby/storage/tasks/_models.py`. test: `tests/storage/tasks/test_task_type_validation.py::test_new_types_accepted`.
- 5.1.2 — `task_type_default_stages` seeded with the four new defaults via migration 235. test: `tests/storage/test_migration_235.py::test_new_task_type_defaults`.
- 5.1.3 — CLI, HTTP, and MCP creation surfaces all accept the new types. test: `tests/cli/test_tasks_create_new_types.py::test_create_simple_fix`, `tests/servers/routes/test_tasks_create.py::test_post_simple_fix`, `tests/mcp_proxy/tools/tasks/test_create_task.py::test_simple_fix_type`.
- 5.1.4 — A `research_spike` task with manifest `[ideation, research, prd]` walks to `prd.done`; the §2.1 invariant 8 generic terminal-close path (acceptance 2.1.9) closes the task atomically in the same transaction with `reason='manifest_exhausted'`. The same path closes a `prd_doc` task at `prd.done` and an `architecture_doc` task at `architecture.done`. No Phase 4.2 dispatcher rule is involved. While the §1.3 placeholder agents remain disabled, the dispatcher escalates the `<discovery>.ready` row via `disabled_agent_escalation_rule` (acceptance 3.1.23) with reason `ideation_no_agent` / `research_no_agent` / `architecture_no_agent` / `prd_no_agent` — surfacing the gap loudly to operators rather than stalling silently. The terminal-close walks become reachable under dispatcher control once the real discovery agents ship (parent epic from §5.4); until then operators advance the discovery stages manually after clearing each escalation. test: `tests/dispatch/test_terminal_non_merge.py::test_research_spike_closes_at_prd`, `tests/dispatch/test_terminal_non_merge.py::test_prd_doc_closes_at_prd`, `tests/dispatch/test_terminal_non_merge.py::test_architecture_doc_closes_at_architecture`, `tests/dispatch/test_terminal_non_merge.py::test_research_spike_at_ideation_with_disabled_placeholder_escalates_with_ideation_no_agent`.

### 5.2 Wire `is_escalated` first-class column through dataclass + readers [category: code] (depends: 1.1, 2.2)

`kind: deliverable`

Target: `src/gobby/storage/tasks/_models.py`, `src/gobby/storage/tasks/_transitions.py`, `src/gobby/tasks/state_semantics.py`

The column itself is created in migration 233 (Phase 1.1, on `tasks` directly) and backfilled from `escalated_at IS NOT NULL` in migration 234 (Phase 2.2 step 6). This deliverable wires the dataclass and read paths to use it; no migration is created here.

Rationale for placement: escalation is task-level, not artifact-level. `task_artifacts` is sparse evidence; `tasks` is the row that gets read on every list. Hosting `is_escalated` on `tasks` from migration 233 onward avoids the previous design's two-migration column-relocation churn.

Update `Task` dataclass: add `is_escalated: bool = False` field. Update every read site that currently calls `is_task_escalated(task)` (`src/gobby/tasks/state_semantics.py:98-105`) to read `task.is_escalated` directly. Keep the old helper only as a one-line `return task.is_escalated`; Phase 5.3 deletes it.

`escalate_task` and `de_escalate_task` (`src/gobby/storage/tasks/_transitions.py:397-457`) update `is_escalated` alongside `escalated_at` / `escalation_reason`. Atomic single transaction.

**Acceptance:**

- 5.2.1 — `Task.is_escalated` field present and populated on read from `tasks.is_escalated`. file: `src/gobby/storage/tasks/_models.py`. test: `tests/storage/tasks/test_task_dataclass.py::test_is_escalated_field`.
- 5.2.2 — `escalate_task` sets `is_escalated=1`; `de_escalate_task` sets `is_escalated=0`; both write `escalated_at` / `escalation_reason` in the same transaction. test: `tests/storage/tasks/test_transitions_is_escalated.py::test_escalate_round_trip`.
- 5.2.3 — Readers in dispatcher, projections, and HTTP responses use `task.is_escalated` directly. test: `tests/dispatch/test_is_escalated_first_class.py::test_no_helper_calls`.
- 5.2.4 — `escalate_task` and `de_escalate_task` do NOT touch `task_stage_states`; a task that escalates from `(stage='development', state='in_progress', work_attempt_count=2, review_round_count=0, entered_at=T)` and then de-escalates returns to the same row values exactly. behavior: "stage state survives escalate/de-escalate round-trip with `work_attempt_count`, `review_round_count`, and `entered_at` preserved" verified in `tests/storage/tasks/test_escalation_preserves_stage.py::test_round_trip_preserves_row`.

### 5.3 Drop `lifecycle`, `lifecycle_stage`, active `status` semantics [category: code] (depends: 2.6, 3.1, 3.2, 3.3, 4.1, 4.2, 5.2, 6.3)

`kind: deliverable`

Target: `src/gobby/storage/migrations.py` (migration 236), `src/gobby/storage/tasks/_models.py`, `src/gobby/storage/tasks/_transitions.py` (DELETIONS ONLY — `mark_task_pr_opened`, `mark_task_merged`, `mark_task_merge_failed`, `advance_lifecycle` are removed; no new code lands here), `src/gobby/storage/tasks/_crud.py` (round-11 F1: `create_task` / `update_task` lose `status` / `lifecycle` / `lifecycle_stage` parameters and column references), `src/gobby/storage/tasks/_manager.py` (round-11 F1: `LocalTaskManager` list/update/filter signatures drop legacy kwargs), `src/gobby/storage/tasks/_queries.py`, `src/gobby/tasks/state_semantics.py`, `src/gobby/storage/tasks/_lifecycle.py`, `src/gobby/tasks/expansion/_apply.py` (round-11 F1: `_complete_dev_only_run` ports its `UPDATE tasks SET lifecycle='in_development'` write to a `complete_stage(task_id, 'expansion')` call against the parent task's manifest), `src/gobby/sync/` task-sync paths (round-11 F1: JSONL exports drop `status` / `lifecycle` / `lifecycle_stage` keys), `src/gobby/mcp_proxy/tools/tasks/_lifecycle_merge.py`, `src/gobby/mcp_proxy/tools/tasks/_lifecycle_status.py`, `src/gobby/cli/tasks/crud.py` (DELETIONS ONLY — `--status` / `--lifecycle` flag removal from the existing `list` command; no new commands added), `src/gobby/servers/routes/tasks.py` (DELETIONS ONLY — `?status=` / `?lifecycle=` query-param handling removed; new stage-related endpoints already live in `src/gobby/servers/routes/stages.py` per §2.4), `src/gobby/storage/tasks/_artifacts.py` (round-19 F2: `_ARTIFACT_FIELDS` frozenset (lines 11–30), `TaskArtifacts` dataclass (lines 56–72), `TaskArtifacts.from_row` (lines 78–96), and `set_artifacts_atomic` (lines 191–205) all carry the five legacy cap columns `max_expansion_attempts`, `max_qa_rounds`, `max_merge_attempts`, `max_holistic_rounds`, `max_review_rounds`; all five field references and read/write paths are removed in lockstep with migration 236's column drops so artifact reads/writes never reference dropped columns — see acceptance 5.3.11 for the precise removals), `src/gobby/agents/lifecycle_monitor.py` (round-18 F1 / round-19 F1 corrected: stale-claim recovery currently reads `getattr(task, "lifecycle_stage", None)` / `getattr(task, "status", None)` and calls `release_task_claim(..., status='open'|'escalated')` at lines 158–230; ports stage context to `current_stage(task).name` and splits the recovery branch by stage state per the §2.1 legality matrix: `in_progress` cancellation/failure recovery uses `fail_stage(task_id, current_stage.name, reason=...)` (legal `in_progress → ready`; under-cap returns to ready, over-cap escalates per §2.1 acceptance 2.1.7); `needs_review` recovery clears ownership without changing review state (`release_task_claim` with no stage transition — the row stays in `needs_review` because the work product is still pending review); `review_approved` recovery is identical (no stage transition); the unrecoverable escalation path (`failure_count >= 3`) calls `escalate_task(task_id, reason=...)` directly. `reject_review` is NOT used here because §2.1 makes it legal only from `needs_review` and the cancellation/failure source state is `in_progress`), `src/gobby/workflows/pipeline_heartbeat.py` (round-18 F1 / round-19 F1 corrected: heartbeat stale-task scan at lines 168–230 currently calls `list_tasks(status=list(ACTIVE_CLAIM_STATUSES))` (line 168–169), reads `getattr(task, "lifecycle_stage", None)` (line 201–205), and writes `release_task_claim(..., status='needs_review')` for the `lifecycle_stage == "in_progress" and has_commits` branch (line 206–213) and `release_task_claim(..., status='open')` for the `lifecycle_stage == "in_progress"` no-commits branch (line 215–219); ports to a `current_stage`-keyed predicate (replaces `list_tasks(status=...)` with a manifest-aware list filtered on `current_stage(task).state == 'in_progress'`), then splits the recovery branch by commit presence per the §2.1 legality matrix: `in_progress` with commits uses `submit_for_review(task_id, current_stage.name, by_session_id=...)` (legal `in_progress → needs_review`); `in_progress` without commits uses `fail_stage(task_id, current_stage.name, reason="stale-no-commits")` (legal `in_progress → ready`; under-cap returns to ready, over-cap escalates per §2.1 acceptance 2.1.7). `reject_review` is NOT used here because §2.1 makes it legal only from `needs_review` and the heartbeat source state is `in_progress`), `src/gobby/hooks/event_handlers/_plan.py` (round-18 F1 corrected line refs: plan-archival event handler at lines 9–49 — `_TERMINAL_LIFECYCLE_STAGES` constant (line 10), `_event_value(event, "lifecycle_stage")` reads (lines 37–38, 47–49) — ports to keying off task-level closure (`task.is_closed` / `closed_at IS NOT NULL`) and the new terminal-close event shape from §2.1's stage-axis transitions; `_TERMINAL_LIFECYCLE_STAGES` constant is removed entirely)

**Monolith-rule constraint (CLAUDE.md guiding principle 2 — no files over 1,000 lines).** As of plan time, `_transitions.py` is 784 lines, `crud.py` is 904 lines, and `routes/tasks.py` is 699 lines (`wc -l` verified). §5.3 is intentionally a net-deletion deliverable for these files: the only edits are removals (legacy column reads/writes, legacy MCP tools, legacy CLI flags, legacy HTTP filters). No new method bodies, route handlers, or subcommands are added inside these files. New stage-axis HTTP routes already live in `src/gobby/servers/routes/stages.py` (created in §2.4); new stage-axis CLI commands already live in `src/gobby/cli/tasks/stages.py` and `review.py` (created in §2.5); new stage-axis storage methods already live in `src/gobby/storage/tasks/_stage_states.py` (created in §2.1). If a `wc -l` check on any of `_transitions.py`, `crud.py`, or `routes/tasks.py` rises during §5.3 (rather than falling), the deliverable MUST diagnose why and either reroute the addition to one of the §2.x stage-axis modules or open a refactor task under #12730 BEFORE landing.

Migration 236 drops legacy columns. Pre-flight: assert no rule, MCP tool, HTTP route, or CLI command writes `lifecycle`, `lifecycle_stage`, or active `status` values (Phases 2.6, 3, 4 must have completed) AND no web source reads them (Phase 6.3 must have completed). The migration runs in a transaction:

**Pre-drop web audit (load-bearing for cross-phase ordering — see F6 fix):** Phase 6.3 retires `KanbanBoard`, `taskState.ts` legacy types, and `TasksPage` kanban-branch reads of `state.lifecycle_stage`. This deliverable cannot ship until 6.3 has shipped because the web bundle would crash on a stripped column. The audit step (acceptance 5.3.8) runs a multi-pattern, legacy-only grep over `web/src/` (excluding the test files that intentionally regression-grep for absence) and a `pnpm tsc --noEmit` build; both must succeed (grep returns no source matches in non-test files, tsc compiles clean) before the migration is unblocked. The dependency chain is therefore: 6.1 → 6.2 → 6.3 → audit → 5.3 migration. Document order in the plan keeps phases 5 then 6 for readability, but task expansion respects the explicit `depends_on` annotation.

**Audit grep patterns (narrowed per F5 fix):** The grep MUST NOT use the bare token `lifecycle`, because the new Phase 6 board introduces intentional `LifecycleBoard` / `lifecycle-board` / `lifecycle-board:hide-blocked` identifiers and a broad token would false-positive on the new naming. The audit is a multi-pattern check that targets only legacy symbols. Use `git grep -nE` (or `grep -rnE` rooted at `web/src/`) with each of the patterns below combined as alternation in a single invocation:

- `\blifecycle_stage\b` — the legacy task field name
- `\bLifecycle\.` — legacy enum-style member access (matches `Lifecycle.Open`, `Lifecycle.PlanReview`, etc.; does NOT match `LifecycleBoard` because the next char is `B`, not `.`)
- `\bTaskBucket\b` — legacy bucket type
- `\bTASK_BUCKET_(LABELS|ORDER)\b` — legacy bucket constants
- `\bmoveTaskToBucket\b` — legacy mover function
- `\bgetTaskBucket\b` — legacy getter
- `\bKanbanBoard\b` — the legacy board component name (does NOT match `LifecycleBoard`)
- `\.lifecycle_stage\b` — direct field reads on task state
- `\bstate\.lifecycle\b` (without `_stage`) — legacy projection reads (does NOT match `state.lifecycle_stage`, which is caught by the first pattern, and does NOT match `LifecycleBoard`-related identifiers)

The combined invocation, expressed as a single shell command for CI:

```bash
git grep -nE '\blifecycle_stage\b|\bLifecycle\.|\bTaskBucket\b|\bTASK_BUCKET_(LABELS|ORDER)\b|\bmoveTaskToBucket\b|\bgetTaskBucket\b|\bKanbanBoard\b|\.lifecycle_stage\b|\bstate\.lifecycle\b' -- 'web/src/' ':(exclude)web/src/**/*test_legacy_symbols_removed*' ':(exclude)web/src/**/*lifecycle-board-css-lint*'
```

The audit passes iff this command returns zero matches (exit 1 / no output for `git grep`). New `LifecycleBoard`, `StageColumn`, `StageCard`, `lifecycle-board.css`, and `lifecycle-board:hide-blocked` identifiers introduced in Phase 6.1/6.2/6.3 are intentionally NOT matched by any of the patterns above (verified via the patterns' word-boundary anchors and the `\bLifecycle\.` pattern requiring a literal `.` after `Lifecycle`).

1. `ALTER TABLE tasks DROP COLUMN lifecycle;`
2. `ALTER TABLE tasks DROP COLUMN lifecycle_stage;`
3. `ALTER TABLE tasks DROP COLUMN status;`
4. `ALTER TABLE task_artifacts DROP COLUMN max_expansion_attempts;`
5. `ALTER TABLE task_artifacts DROP COLUMN max_qa_rounds;`
6. `ALTER TABLE task_artifacts DROP COLUMN max_merge_attempts;`
7. `ALTER TABLE task_artifacts DROP COLUMN max_holistic_rounds;`
8. `ALTER TABLE task_artifacts DROP COLUMN max_review_rounds;`

Steps 4-8 fulfill the §1.1 cap-column-drop promise: after the §2.2 backfill (migration 234) copied each legacy `task_artifacts.max_*` value into the appropriate per-stage `task_stage_states.max_<work|review>` column per the §1.1 authoritative inventory (`max_expansion_attempts → expansion.max_work_attempts`; `max_qa_rounds → development.max_review_rounds`; `max_merge_attempts → merge.max_work_attempts`; `max_holistic_rounds → holistic_qa.max_review_rounds`; `max_review_rounds → pr.max_review_rounds`), and after migration 234 commits, no runtime reader touches the legacy cap columns (§1.1 invariant). Migration 236 drops them in the same transaction as the `tasks` legacy-column drops to keep cleanup atomic.

The `status` column drop is unconditional, matching the strategy plan's clean-cutover constraint (no shadow model, no compatibility shim). Pre-flight audit (covered by acceptance 5.3.9) identifies every remaining reader/writer of `tasks.status` in runtime code (MCP tools, HTTP routes, CLI commands, dispatcher rules, projections, web bundle); each is ported in THIS SAME deliverable to: `closed_at IS NOT NULL` for closure checks, `tasks.is_escalated` (Phase 5.2 column) for escalation, and stage-state reads (`current_stage(task).name/state`) for everything else. The migration is blocked until the audit returns zero hard readers; the audit is a tooling step that runs before the `ALTER TABLE` statements execute (failing the migration with a named-readers diagnostic if any remain).

`Task.status` Literal field is removed unconditionally from `_models.py`; `serialize_task_state` strips `status` from the response shape (only `current_stage`, `is_closed`, `is_escalated`, `is_blocked`, `owner_session_id` remain). MCP/HTTP responses that previously surfaced `status` strings now surface `current_stage.name` plus `current_stage.state` plus the boolean flags. The `is_closed` projection (already a Python derivation at `state_semantics.py:88-95`) keeps reading `closed_at IS NOT NULL` post-drop — the helper's `OR status == 'closed'` clause is removed when the column is dropped (one-line change in `state_semantics.py`).

Tools and helpers to delete (after final rule-rewrite checks):

- `mark_task_pr_opened` (storage `_transitions.py:642-658`, MCP `_lifecycle_merge.py:23-34`)
- `mark_task_merged` (storage `_transitions.py:661-680`, MCP `_lifecycle_merge.py:60-80`)
- `mark_task_merge_failed` (storage `_transitions.py:683-726` area, MCP `_lifecycle_merge.py:115-138`)
- `advance_lifecycle` (`_transitions.py:220-286`)
- `Lifecycle` StrEnum (`_models.py:42-51`)
- `TaskLifecycleStage` Literal (`state_semantics.py:7`)
- `lifecycle_stage_from_status` (`state_semantics.py:45-49`)
- `normalize_lifecycle_stage` (`state_semantics.py:52-63`)
- `project_legacy_status` (`state_semantics.py:66-85`)
- `_coerce_task_lifecycle_stage` (`state_semantics.py:175-192`)
- `serialize_task_state` returns no `lifecycle_stage` field; rewrite to expose `current_stage`, `is_closed`, `is_escalated`, `is_blocked`, `owner_session_id` only.

CLI flag removals: `gobby tasks list --status` and `--lifecycle` are deleted (Phase 2.5 added their replacements; the old flags are now removed). HTTP filter param removals: `?status=...` and `?lifecycle=...` query params are deleted from the list endpoint.

`mark_task_review_approved`, `mark_task_review_rejected`, `mark_task_needs_review` MCP tools — Phase 2.6 already rewired these to **first-class stage-axis transitions** (`StageStatesManager.approve_review` / `.reject_review` / `.submit_for_review`) before the dispatcher cutover; they are NOT compositions of `complete_stage` / `fail_stage` (that lossy mapping was the discarded tri-state contract — under the 5-state contract `review_approved` is a durable holding state distinct from `done`, and reject increments `review_round_count` which `fail_stage` does not touch). This deliverable retains acceptance 5.3.7's audit (now reframed as a post-cutover regression gate: re-runs §2.6.6's audit to confirm every caller's stage row still satisfies `review_policy ∈ {required, optional}` AND current state matches the legal source state, AND no remaining caller writes legacy `status` values like `'review_approved'`/`'needs_review'`/`'rejected'` for these review transitions). Any stragglers found by the audit are removed here so migration 236 can drop the active `status` enum values cleanly.

`escalated` is preserved as a first-class column (Phase 5.2's `tasks.is_escalated`). `closed` is no longer represented by a `status` value at all; closure is `closed_at IS NOT NULL` (canonical SQL) and `task.is_closed` (Python projection). The `status` column drop in step 3 above is unconditional.

**Acceptance:**

- 5.3.1 — Migration 236 drops `lifecycle`, `lifecycle_stage`, AND `status` from `tasks` in a single transaction. The `status` drop is unconditional (no audit-gated optionality); the pre-flight audit covered by 5.3.9 must pass before the migration proceeds. file: `src/gobby/storage/migrations.py`. test: `tests/storage/test_migration_236_drop_legacy.py::test_lifecycle_column_dropped`, `tests/storage/test_migration_236_drop_legacy.py::test_lifecycle_stage_column_dropped`, `tests/storage/test_migration_236_drop_legacy.py::test_status_column_dropped`.
- 5.3.2 — `Lifecycle` StrEnum, `TaskLifecycleStage` Literal, and the projection helpers in `state_semantics.py` are deleted. file: `src/gobby/tasks/state_semantics.py`, `src/gobby/storage/tasks/_models.py`. test: grep-based regression `tests/test_legacy_symbols_removed.py::test_no_lifecycle_imports`.
- 5.3.3 — Legacy lifecycle MCP tools (`mark_task_pr_opened`, `mark_task_merged`, `mark_task_merge_failed`, `advance_lifecycle`) are removed. file: `src/gobby/mcp_proxy/tools/tasks/_lifecycle_merge.py`, `src/gobby/storage/tasks/_transitions.py`. test: `tests/mcp_proxy/tools/tasks/test_legacy_tools_removed.py::test_tools_absent`.
- 5.3.4 — Post-rewire verification: `mark_task_review_approved` / `mark_task_review_rejected` / `mark_task_needs_review` (rewired in Phase 2.6 to `StageStatesManager.approve_review` / `.reject_review` / `.submit_for_review` — first-class stage-axis transitions, NOT compositions of `complete_stage` / `fail_stage`) write no legacy `status` values; their stage-native paths are the only path. The plan/repo audit also asserts no surviving prose or comment describes these tools as `complete_stage` / `fail_stage` shims. test: `tests/storage/tasks/test_review_tools_no_legacy_writes.py::test_no_status_writes_after_rewire`, `tests/storage/tasks/test_review_tools_no_legacy_writes.py::test_review_tools_call_first_class_stage_axis_methods`, `tests/storage/tasks/test_review_tools_no_legacy_writes.py::test_no_complete_stage_or_fail_stage_in_review_tool_paths`, `tests/test_plan_prose_review_tool_descriptions.py::test_no_complete_stage_fail_stage_shim_prose_in_plan_files`.
- 5.3.5 — CLI `--status`/`--lifecycle` flags and HTTP `?status=`/`?lifecycle=` filters are removed. test: `tests/cli/test_legacy_flags_removed.py::test_status_flag_unknown`, `tests/servers/routes/test_legacy_filters_removed.py::test_status_filter_400`.
- 5.3.6 — `serialize_task_state` returns the new shape without `lifecycle_stage`. file: `src/gobby/tasks/state_semantics.py`. test: `tests/tasks/test_serialize_task_state.py::test_new_shape`.
- 5.3.7 — Post-cutover call-site audit (regression gate against §2.6.6's inline rewrites): every existing caller of `mark_task_review_approved`, `mark_task_review_rejected`, and `mark_task_needs_review` is invoked from a context where the caller's stage row satisfies the precondition under the new contract — `current_stage.review_policy ∈ {required, optional}` AND the row's current state matches the legal source state for the called transition. §2.6.6 performed the inline rewrites and fail-fast gate before §3.1 dispatcher cutover; this acceptance re-runs the same audit AFTER the legacy `lifecycle`/`status`/`lifecycle_stage` columns are dropped to confirm no regression slipped in during the intervening phases. The audit grep is the same: `grep -rln 'mark_task_review_\(approved\|rejected\)\|mark_task_needs_review' src/gobby/install/shared/`. The allowlist is identical to §2.6.6's POST-rewrite allowlist; `test-architect.yaml` MUST NOT appear as a caller (its calls were rewritten to `complete_stage` in §2.6.6). Workflow handoff regression: every rewritten workflow's `on_mcp_success` hooks still match a tool the agent actually calls under the new contract (no residual hook keyed on `mark_task_review_approved` for any rewritten policy=none workflow), and `test-architect.yaml`'s `complete_stage` success hook still flips `handoff_ready=true` so the `design → terminate` transition resolves. Drops legacy-status prose audit too: confirm no SKILL.md instruction surface or rule YAML still references legacy "stage stays open, status becomes needs_review" semantics. test: `tests/storage/tasks/test_review_tools_call_site_audit.py::test_all_callers_satisfy_policy`, `tests/storage/tasks/test_review_tools_call_site_audit.py::test_test_architect_yaml_does_not_call_review_tools`, `tests/storage/tasks/test_review_tools_call_site_audit.py::test_test_architect_yaml_still_calls_complete_stage_for_test_arch`, `tests/storage/tasks/test_review_tools_call_site_audit.py::test_test_architect_yaml_complete_stage_success_hook_still_sets_handoff_ready`, `tests/storage/tasks/test_review_tools_call_site_audit.py::test_test_architect_yaml_workflow_still_reaches_terminate_after_complete_stage`, `tests/storage/tasks/test_review_tools_call_site_audit.py::test_no_residual_success_hook_keyed_on_unused_tool`, `tests/storage/tasks/test_review_tools_call_site_audit.py::test_no_legacy_status_prose_remains_in_skill_or_rule_yaml`, `tests/storage/tasks/test_review_tools_call_site_audit.py::test_post_cutover_allowlist_matches_phase_2_6_6`.
- 5.3.8 — Pre-drop web audit: the multi-pattern, legacy-only grep documented in the §5.3 narrative ("Audit grep patterns") returns zero source matches across `web/src/` (excluding the legacy-removal regression tests `test_legacy_symbols_removed.test.ts` and the CSS lint test `lifecycle-board-css-lint.test.ts`); `pnpm tsc --noEmit` compiles clean against the post-Phase-6.3 web bundle; running this audit before the migration runs is enforced by a CI step gating migration 236. The grep MUST NOT use the bare token `lifecycle` (would false-positive on `LifecycleBoard`, `lifecycle-board`, and `lifecycle-board:hide-blocked` introduced by Phase 6); the patterns are anchored on `\blifecycle_stage\b`, `\bLifecycle\.`, `\bTaskBucket\b`, `\bTASK_BUCKET_(LABELS|ORDER)\b`, `\bmoveTaskToBucket\b`, `\bgetTaskBucket\b`, `\bKanbanBoard\b`, `\.lifecycle_stage\b`, and `\bstate\.lifecycle\b` (full single-shell-line invocation in the §5.3 narrative). behavior: "web bundle has no legacy reads before column drop" verified in `tests/migrations/test_pre_drop_web_audit.py::test_no_legacy_web_reads` (the test executes the documented `git grep -nE` command and asserts zero output); the new `LifecycleBoard` family of identifiers is asserted to be ignored by the patterns in `tests/migrations/test_pre_drop_web_audit.py::test_grep_does_not_match_new_lifecycle_board_identifiers`. Both tests carry `pytest.mark.integration` for local runs and execute unconditionally in CI.
- 5.3.10 — Monolith-rule compliance: every source file touched by §5.3 finishes the deliverable at strictly less than 1,000 lines (CLAUDE.md guiding principle 2). Specifically: `src/gobby/storage/tasks/_transitions.py` (currently 784 lines) drops to a smaller line count after legacy-method deletions; `src/gobby/cli/tasks/crud.py` (currently 904 lines) drops to a smaller line count after `--status` / `--lifecycle` flag removal; `src/gobby/servers/routes/tasks.py` (currently 699 lines) drops to a smaller line count after `?status=` / `?lifecycle=` filter removal. All three line counts MUST be lower than their pre-§5.3 values, confirming that §5.3 is a net-deletion deliverable. New stage-axis bodies live in §2.x destination files (`src/gobby/storage/tasks/_stage_states.py`, `src/gobby/servers/routes/stages.py`, `src/gobby/cli/tasks/stages.py`, `src/gobby/cli/tasks/review.py`). If a `wc -l` check shows any of the three files at or above its pre-§5.3 baseline, the deliverable FAILS — either rerouting the addition to a §2.x file or opening a refactor task under #12730 is required before landing. test: `tests/test_phase_5_3_monolith_compliance.py::test_transitions_py_under_1000_lines_and_smaller_than_baseline`, `tests/test_phase_5_3_monolith_compliance.py::test_crud_py_under_1000_lines_and_smaller_than_baseline`, `tests/test_phase_5_3_monolith_compliance.py::test_routes_tasks_py_under_1000_lines_and_smaller_than_baseline`, `tests/test_phase_5_3_monolith_compliance.py::test_no_new_method_bodies_added_to_legacy_files` (greps for new `def` / `async def` introductions in the three files' diffs).
- 5.3.11 — Migration 236 drops the five legacy `task_artifacts` cap columns (`max_expansion_attempts`, `max_qa_rounds`, `max_merge_attempts`, `max_holistic_rounds`, `max_review_rounds`) in the same transaction as the `tasks` legacy-column drops (5.3.1), AND removes those five fields from the runtime artifact manager (`src/gobby/storage/tasks/_artifacts.py`) in lockstep so `set_artifacts_atomic`'s dynamically-built INSERT/UPDATE column list never references dropped columns. **(round-19 F2)** Concrete artifact-manager removals: the five field names are dropped from `_ARTIFACT_FIELDS` (lines 11–30), from the `TaskArtifacts` dataclass field declarations (lines 56–72), from `TaskArtifacts.from_row`'s `_optional_row_int` calls (lines 78–96), and (consequentially, since `set_artifacts_atomic` builds its column list from `_ARTIFACT_FIELDS`) from the dynamic write path at lines 191–205. Pre-flight: assert §2.2 backfill (migration 234) has copied every legacy cap value into the per-stage `task_stage_states.max_<work|review>` column per the §1.1 authoritative inventory, and assert no runtime reader touches `task_artifacts.max_*` (the §1.1 invariant — runtime caps resolve via `state_row.max_<X> ?? registry_row.default_max_<X>`). Fresh-schema test asserts the five columns are absent from `task_artifacts`; upgrade-path test asserts a pre-cutover database surviving migration 234 + 236 has the per-stage caps populated and the legacy columns gone. **(round-19 F2)** Targeted (non-grep) assertions on the artifact manager: `_ARTIFACT_FIELDS` does NOT contain any of the five legacy cap names; `TaskArtifacts.__dataclass_fields__` does NOT contain any of the five legacy cap names; `set_artifact(task_id, field='max_review_rounds', value=...)` raises `ValueError` (the existing unknown-field path at line 104–106); `set_artifacts_atomic(task_id, max_review_rounds=...)` raises `ValueError`. The runtime-reader audit retained for the rest of the codebase: scoped grep over `src/gobby/storage/`, `src/gobby/dispatch/`, `src/gobby/build/`, `src/gobby/mcp_proxy/`, `src/gobby/servers/`, `src/gobby/cli/`, `src/gobby/agents/`, `src/gobby/workflows/`, `src/gobby/hooks/` for the patterns `task_artifacts\.\(max_expansion_attempts\|max_qa_rounds\|max_merge_attempts\|max_holistic_rounds\|max_review_rounds\)` and the qualified `_ARTIFACT_FIELDS` / `TaskArtifacts\.\(max_expansion_attempts\|max_qa_rounds\|max_merge_attempts\|max_holistic_rounds\|max_review_rounds\)` references returns zero matches outside `src/gobby/storage/migrations.py` (where migration 234's backfill SELECT is allowed) and `tests/storage/test_migration_*.py`. The unqualified-name grep is intentionally NOT used (it would false-positive on per-stage cap kwargs); the targeted dataclass assertions above replace it. file: `src/gobby/storage/migrations.py`, `src/gobby/storage/tasks/_artifacts.py`. test: `tests/storage/test_migration_236_drop_legacy.py::test_max_expansion_attempts_column_dropped`, `tests/storage/test_migration_236_drop_legacy.py::test_max_qa_rounds_column_dropped`, `tests/storage/test_migration_236_drop_legacy.py::test_max_merge_attempts_column_dropped`, `tests/storage/test_migration_236_drop_legacy.py::test_max_holistic_rounds_column_dropped`, `tests/storage/test_migration_236_drop_legacy.py::test_max_review_rounds_column_dropped`, `tests/storage/test_migration_236_drop_legacy.py::test_fresh_schema_lacks_all_five_legacy_cap_columns`, `tests/storage/test_migration_236_drop_legacy.py::test_upgrade_path_preserves_per_stage_caps_after_drop`, `tests/storage/test_migration_236_drop_legacy.py::test_no_runtime_reader_references_legacy_cap_columns`, `tests/storage/tasks/test_artifacts_no_legacy_caps.py::test_artifact_fields_excludes_max_expansion_attempts`, `tests/storage/tasks/test_artifacts_no_legacy_caps.py::test_artifact_fields_excludes_max_qa_rounds`, `tests/storage/tasks/test_artifacts_no_legacy_caps.py::test_artifact_fields_excludes_max_merge_attempts`, `tests/storage/tasks/test_artifacts_no_legacy_caps.py::test_artifact_fields_excludes_max_holistic_rounds`, `tests/storage/tasks/test_artifacts_no_legacy_caps.py::test_artifact_fields_excludes_max_review_rounds`, `tests/storage/tasks/test_artifacts_no_legacy_caps.py::test_taskartifacts_dataclass_fields_excludes_legacy_caps`, `tests/storage/tasks/test_artifacts_no_legacy_caps.py::test_set_artifact_rejects_max_review_rounds_kwarg`, `tests/storage/tasks/test_artifacts_no_legacy_caps.py::test_set_artifacts_atomic_rejects_max_review_rounds_kwarg`.
- 5.3.9 — Pre-`status`-drop audit-and-port: every remaining runtime reader/writer of `tasks.status`, `tasks.lifecycle`, and `tasks.lifecycle_stage` is identified and ported in this deliverable to one of the post-cutover sources (`closed_at IS NOT NULL` for closure, `tasks.is_escalated` for escalation, `current_stage(task)` / `task.stages` for everything else) BEFORE migration 236 executes. The audit is multi-pattern, scoped to task-state code (`src/gobby/storage/tasks/`, `src/gobby/tasks/`, `src/gobby/sync/`, `src/gobby/dispatch/`, `src/gobby/build/`, `src/gobby/cli/`, `src/gobby/mcp_proxy/`, `src/gobby/servers/`, `src/gobby/agents/`, `src/gobby/workflows/`, `src/gobby/hooks/`; explicitly excludes `src/gobby/storage/migrations.py` and `tests/storage/test_migration_*.py`). The three additional directories (`agents/`, `workflows/`, `hooks/`) cover the round-18 F1 surfaces — `agents/lifecycle_monitor.py` stale-claim recovery, `workflows/pipeline_heartbeat.py` task scans, and `hooks/event_handlers/_plan.py` plan-archival event handler — that consume task state outside the storage/MCP/HTTP/CLI/dispatcher core. Patterns matched: (a) comparisons of `.status` / `.lifecycle` / `.lifecycle_stage` against legacy enum string literals (`open`, `in_progress`, `needs_review`, `review_approved`, `escalated`, `closed`, `plan_review`, `expanding`, `in_development`, `holistic_review`, `pr`, `merging`, `merged`, `test_arch`); (b) unqualified SQL inserts/updates referencing legacy columns: `INSERT INTO tasks (... status ...)`, `INSERT INTO tasks (... lifecycle ...)`, `INSERT INTO tasks (... lifecycle_stage ...)`, `status = ?`, `lifecycle = ?`, `lifecycle_stage = ?`; (c) function-parameter usages on task CRUD/list/update APIs declaring `status:` / `lifecycle:` / `lifecycle_stage:` typed parameters; (d) JSONL/sync export key emissions for `status` / `lifecycle` / `lifecycle_stage`; (e) **(round-12 F1)** dynamic-write sources where dict construction places legacy columns into a write-bound dict: literal dict keys `'status':`, `'lifecycle':`, `'lifecycle_stage':` inside `synced_values`-style dicts that flow into dynamically-built `INSERT INTO tasks ({columns})` or `UPDATE tasks SET {set_clause}` SQL. Pattern (e) is scoped to task-sync code (`src/gobby/sync/`) and task-CRUD (`src/gobby/storage/tasks/`) to avoid false-positives against unrelated `status` keys (validation status, run status, workflow status, etc.). The scoping by directory ensures unrelated tables (workflows, sessions) with their own `status` columns do not false-positive — those directories are excluded from the audit. The named runtime ports include: `_crud.py::create_task` and `update_task` lose legacy column references; `_apply.py::_complete_dev_only_run` ports to `complete_stage(task_id, 'expansion')`; `LocalTaskManager` list/update/filter signatures lose `status` / `lifecycle` kwargs; task sync EXPORT drops legacy keys; **(round-18 F1 / round-19 F1 corrected)** `src/gobby/agents/lifecycle_monitor.py::_recover_stale_claims` (lines 158–230) ports its `getattr(task, "lifecycle_stage", None)` reads to `current_stage(task).name` and splits the recovery branch by stage state per §2.1's legality matrix: `in_progress` cancellation/failure recovery uses `fail_stage(task_id, current_stage.name, reason=...)` (legal `in_progress → ready`; under-cap returns to ready, over-cap escalates per §2.1 acceptance 2.1.7) — NOT `reject_review`, which is legal only from `needs_review`; `needs_review` and `review_approved` recovery clears ownership only (`release_task_claim` with no stage transition — the work product is still pending review); the unrecoverable escalation path (`failure_count >= 3`) calls `escalate_task(task_id, reason=...)` directly; **(round-18 F1 / round-19 F1 corrected)** `src/gobby/workflows/pipeline_heartbeat.py` (lines 168–230) ports `list_tasks(status=list(ACTIVE_CLAIM_STATUSES))` (line 168–169) to a `current_stage`-keyed predicate (`tasks_with_current_stage_state(state='in_progress')`) and splits the stale-task recovery branch by commit presence per §2.1's legality matrix: `in_progress` with commits (the line 206–213 branch that currently writes `status='needs_review'`) uses `submit_for_review(task_id, current_stage.name, by_session_id=...)` (legal `in_progress → needs_review`); `in_progress` without commits (the line 215–219 branch that currently writes `status='open'`) uses `fail_stage(task_id, current_stage.name, reason='stale-no-commits')` (legal `in_progress → ready`; under-cap returns to ready, over-cap escalates per §2.1 acceptance 2.1.7) — NOT `reject_review`, which is legal only from `needs_review`; **(round-18 F1 corrected line refs)** `src/gobby/hooks/event_handlers/_plan.py::_handle_task_close` (lines 9–49 — file is 64 lines total; the `_TERMINAL_LIFECYCLE_STAGES` constant on line 10 and `_event_value(event, "lifecycle_stage")` reads on lines 37–38 and 47–49) stops reading `_event_value(event, "lifecycle_stage")` and switches to the new terminal-close event shape — keys archival on `event.kind == 'task_closed'` (or `task.is_closed == True` for the post-cutover event payload), removes `_TERMINAL_LIFECYCLE_STAGES` matching entirely; **(round-12 F1)** `src/gobby/sync/tasks.py::TaskSyncManager.import_from_jsonl` ports its dynamic write path: stops reading `lifecycle_stage` from `tasks` rows, stops recognizing top-level JSONL `status` / `lifecycle_stage` keys, removes `status` / `lifecycle_stage` from `synced_values` dict construction, so the dynamically-built `INSERT INTO tasks ({columns})` and `UPDATE tasks SET {set_clause}` no longer reference dropped columns. Audit returns zero matches outside the historical-migration boundary post-port. The MCP `get_task` / `list_tasks` response shape no longer includes `status` / `lifecycle` / `lifecycle_stage`; the HTTP `/api/tasks` GET endpoints no longer include those keys; the CLI `gobby tasks list` output no longer includes those columns; task JSONL exports AND imports no longer carry/process those keys. test: `tests/storage/test_migration_236_drop_legacy.py::test_legacy_column_audit_grep_returns_zero_runtime_matches`, `tests/storage/test_migration_236_drop_legacy.py::test_dynamic_dict_write_audit_returns_zero_matches`, `tests/storage/tasks/test_crud_no_legacy_columns.py::test_create_task_no_status_param`, `tests/storage/tasks/test_crud_no_legacy_columns.py::test_update_task_no_lifecycle_param`, `tests/tasks/expansion/test_apply_dev_only.py::test_complete_dev_only_run_via_complete_stage`, `tests/sync/test_task_jsonl_export_shape.py::test_no_legacy_keys`, `tests/sync/test_task_jsonl_import_shape.py::test_import_does_not_write_legacy_columns`, `tests/sync/test_task_jsonl_import_shape.py::test_import_ignores_top_level_legacy_keys`, `tests/mcp_proxy/tools/tasks/test_get_task_response_shape.py::test_no_legacy_fields`, `tests/servers/routes/test_tasks_list_response_shape.py::test_no_legacy_fields`, `tests/cli/test_tasks_list_columns.py::test_no_legacy_columns`, `tests/agents/test_lifecycle_monitor_stage_native.py::test_recover_stale_claims_uses_current_stage`, `tests/agents/test_lifecycle_monitor_stage_native.py::test_recover_in_progress_cancellation_calls_fail_stage_not_status_open`, `tests/agents/test_lifecycle_monitor_stage_native.py::test_recover_needs_review_clears_ownership_no_stage_transition`, `tests/agents/test_lifecycle_monitor_stage_native.py::test_recover_review_approved_clears_ownership_no_stage_transition`, `tests/agents/test_lifecycle_monitor_stage_native.py::test_recover_in_progress_does_not_call_reject_review`, `tests/agents/test_lifecycle_monitor_stage_native.py::test_recover_unrecoverable_calls_escalate_task_not_status_escalated`, `tests/workflows/test_pipeline_heartbeat_stage_native.py::test_heartbeat_filters_on_current_stage_predicate_not_status`, `tests/workflows/test_pipeline_heartbeat_stage_native.py::test_heartbeat_in_progress_with_commits_calls_submit_for_review_not_status_needs_review`, `tests/workflows/test_pipeline_heartbeat_stage_native.py::test_heartbeat_in_progress_no_commits_calls_fail_stage_not_status_open`, `tests/workflows/test_pipeline_heartbeat_stage_native.py::test_heartbeat_in_progress_no_commits_does_not_call_reject_review`, `tests/workflows/test_pipeline_heartbeat_stage_native.py::test_heartbeat_submit_branch_only_used_from_in_progress_with_commits`, `tests/hooks/test_plan_event_handler_stage_native.py::test_archive_keys_on_task_closed_event_not_lifecycle_stage`, `tests/hooks/test_plan_event_handler_stage_native.py::test_terminal_lifecycle_stages_constant_removed`.

### 5.4 Discovery-stage agent follow-up tracking [category: manual] (depends: 1.3)

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

Implementation note for the executing agent: task lifecycle tools live on `gobby-tasks` (NOT `gobby-tasks-ops`, which carries plan/expansion/build operations only). Use progressive discovery to confirm: call `list_tools(server_name='gobby-tasks')` and `get_tool_schema(server_name='gobby-tasks', tool_name='create_task')` once at the top of the run before issuing any creates. Then call `gobby-tasks:create_task` for each task — create the parent first, capture its ref/id, then create each child with `parent_task_id` set. Apply labels via `gobby-tasks:add_label` (or include in initial creation if the `create_task` schema accepts a `labels` field). Verify all five tasks land in the same project (`d45545c5-ded5-4335-b115-0245752edacf`) and surface the parent ref to the operator on completion. The previous draft of this note pointed at `gobby-tasks-ops:create_task` and `gobby-tasks-ops:add_label`; that was incorrect — `gobby-tasks-ops` does not expose those tools, and following the stale prose literally hands the executing agent an unavailable tool/server combination.

This deliverable does **not** open or implement any of the agents — only creates the tracking tasks. Real implementation work happens in later planning rounds spawned from the new parent epic.

Test pattern for 5.4.1–5.4.4: tests query the live gobby-tasks DB to verify seeded tasks exist. Mark each test with `@pytest.mark.integration` and gate behind a fixture that calls `gobby-tasks:list_tasks(label="agent-followup:")` once per test session — if the result is empty, `pytest.skip("agent-followup tasks not yet seeded; run deliverable 5.4 first")`. This prevents flakiness when the test file runs before the deliverable has executed (e.g., during a fresh checkout test run) without losing the value of the post-execution check.

**Acceptance:**

- 5.4.1 — One parent epic exists in gobby-tasks titled `Discovery-stage agent registry` with the declared labels and description. behavior: "epic exists with deferred-from label and references all four placeholders" verified in `tests/dispatch/test_agent_followup_tasks.py::test_parent_epic_exists` (post-execution fixture seeded by the executing agent).
- 5.4.2 — Four child tasks exist under the parent, one per `(stage, agent-slug)` pair, each carrying the `agent-followup:<slug>` and `deferred-from:` labels. test: `tests/dispatch/test_agent_followup_tasks.py::test_four_children_with_labels`.
- 5.4.3 — Every child task references the exact placeholder YAML path in its description. behavior: "each child description names src/gobby/install/shared/workflows/agents/<slug>.yaml verbatim" verified in `tests/dispatch/test_agent_followup_tasks.py::test_descriptions_reference_placeholders`.
- 5.4.4 — Children are open (`is_closed=false`) so future planning rounds can pick them up. test: `tests/dispatch/test_agent_followup_tasks.py::test_children_open`.
- 5.4.4a — Plan-prose regression: the §5.4 implementation note names `gobby-tasks` (NOT `gobby-tasks-ops`) as the MCP server hosting `create_task` and `add_label`. The §5.4 narrative window (from the section heading to the `**Acceptance:**` line, exclusive) contains zero positive recommendations of the form `gobby-tasks-ops:create_task` or `gobby-tasks-ops:add_label`; `gobby-tasks-ops` is mentioned only inside the explicit corrective sentence that calls out the prior incorrect prose. test: `tests/test_plan_section_5_4_server_routing.py::test_create_task_routed_to_gobby_tasks_not_ops`, `tests/test_plan_section_5_4_server_routing.py::test_add_label_routed_to_gobby_tasks_not_ops` (both tests grep the §5.4 narrative window for the forbidden patterns `r"gobby-tasks-ops:create_task"` and `r"gobby-tasks-ops:add_label"` outside the documented corrective sentence and assert zero matches).

Per-row coverage (one acceptance per data row of the §5.4 Stage|Agent-slug table, per the plan-coverage contract's table-row decomposition rule):

- 5.4.5 — Stage `ideation` → agent slug `analyst`: a child task exists under the parent epic with title `Implement analyst agent for ideation stage`, `task_type=feature`, `category=planning`, `parent_task_id` set to the parent, labels including `agent-followup:analyst` and `deferred-from:task-13482-stage-manifest-cutover:5.4`, and a description that names `src/gobby/install/shared/workflows/agents/analyst.yaml` verbatim. behavior: "ideation/analyst follow-up child task seeded with declared shape" verified in `tests/dispatch/test_agent_followup_tasks.py::test_child_for_ideation_analyst`.
- 5.4.6 — Stage `research` → agent slug `researcher`: a child task exists under the parent epic with title `Implement researcher agent for research stage`, `task_type=feature`, `category=planning`, `parent_task_id` set to the parent, labels including `agent-followup:researcher` and `deferred-from:task-13482-stage-manifest-cutover:5.4`, and a description that names `src/gobby/install/shared/workflows/agents/researcher.yaml` verbatim. behavior: "research/researcher follow-up child task seeded with declared shape" verified in `tests/dispatch/test_agent_followup_tasks.py::test_child_for_research_researcher`.
- 5.4.7 — Stage `architecture` → agent slug `architect`: a child task exists under the parent epic with title `Implement architect agent for architecture stage`, `task_type=feature`, `category=planning`, `parent_task_id` set to the parent, labels including `agent-followup:architect` and `deferred-from:task-13482-stage-manifest-cutover:5.4`, and a description that names `src/gobby/install/shared/workflows/agents/architect.yaml` verbatim. behavior: "architecture/architect follow-up child task seeded with declared shape" verified in `tests/dispatch/test_agent_followup_tasks.py::test_child_for_architecture_architect`.
- 5.4.8 — Stage `prd` → agent slug `product-manager`: a child task exists under the parent epic with title `Implement product-manager agent for prd stage`, `task_type=feature`, `category=planning`, `parent_task_id` set to the parent, labels including `agent-followup:product-manager` and `deferred-from:task-13482-stage-manifest-cutover:5.4`, and a description that names `src/gobby/install/shared/workflows/agents/product-manager.yaml` verbatim. behavior: "prd/product-manager follow-up child task seeded with declared shape" verified in `tests/dispatch/test_agent_followup_tasks.py::test_child_for_prd_product_manager`.

## P6 Web UI — LifecycleBoard

`kind: framing`

**Goal**: Replace the 6-bucket `KanbanBoard` with a stage-manifest-driven `LifecycleBoard`. After Phase 6, the kanban view renders one column per registry stage with tri-state badges per task and drag-to-advance hooked into `PATCH /api/tasks/{id}/stages/{name}`.

**Design prerequisite (every Phase 6 deliverable)**: before producing or modifying any UI surface, the implementing agent MUST call `get_skill(name="impeccable")` on `gobby-skills` and read `.impeccable.md` at the project root (per CLAUDE.md "Design Context"). All visual decisions — column layout, tri-state visualization, badge palette, swimlane styling, drag-feedback animations, blocked-overlay styling, focus rings, type ramp — must conform to that skill's deutan-safe color constraints, WCAG 2.2 AA contrast targets, aesthetic references, and the per-surface variation rules for `./web/`. Freehand color, typography, or spacing choices are not permitted; if the skill is silent on a specific case, surface the gap to the operator rather than guess.

### 6.1 New `LifecycleBoard.tsx` + `StageColumn.tsx` + `StageCard.tsx` + `stageActions.ts` helper [category: code] (depends: 2.4)

`kind: deliverable`

Target: `web/src/components/tasks/LifecycleBoard.tsx`, `web/src/components/tasks/StageColumn.tsx`, `web/src/components/tasks/StageCard.tsx`, `web/src/lib/stageActions.ts`, `web/src/styles/lifecycle-board.css` (per acceptance 6.1.8 — token-only stylesheet for the board chrome; raw color literals are forbidden, enforced by 6.1.9), `web/src/__tests__/lifecycle-board-css-lint.test.ts` (per acceptance 6.1.9 — automated lint test asserting no raw color literals in `lifecycle-board.css`) (all new)

Replicate the props pattern of `KanbanBoard` (`web/src/components/tasks/KanbanBoard.tsx`) but driven by registry stages instead of fixed buckets. Use the existing `@atlaskit/pragmatic-drag-and-drop` library — it's already wired (`web/package.json` v1.7.7) and handles draggable cards + drop targets in `KanbanBoard`.

**Shared drag-action helper authored here.** This deliverable owns `web/src/lib/stageActions.ts` — the single source of truth for the 5-state row enum, the review-policy enum, the typed `StageAdvanceAction`, and the `resolveAdvanceAction` resolver. §6.1's components import the helper directly; §6.2 (`useTasks` denormalized stage manifest) re-imports the same module instead of authoring a duplicate. Owning the helper here keeps §6.1's leaf self-contained — the components that depend on `resolveAdvanceAction` and the helper that defines it land in the same deliverable, no forward dependency on §6.2:

```typescript
// web/src/lib/stageActions.ts (new shared helper — owned by §6.1; §6.2 imports from here)
export type StageState5 = 'ready' | 'in_progress' | 'needs_review' | 'review_approved' | 'done'
export type StageRowState = StageState5  // alias kept for legacy snippet readability; both names refer to the same union
export type ReviewPolicy = 'none' | 'required' | 'optional'

export type StageAdvanceAction = 'start' | 'submit_for_review' | 'approve_review' | 'complete'

// Per-stage manifest row as the web layer consumes it. Authored here (§6.1)
// rather than in §6.2 so §6.1's components typecheck self-contained without a
// forward dependency on §6.2's GobbyTask extension. §6.2 re-imports this type
// when extending GobbyTask with `stages?: StageStateView[]`.
export interface StageStateView {
  stage_name: string
  position: number
  state: StageState5
  review_policy: ReviewPolicy
  reviewer_agent: string | null
  work_attempt_count: number
  review_round_count: number
  max_work_attempts: number | null
  max_review_rounds: number | null
  artifact_refs: Record<string, string> | null
}

// Minimal task row shape required by §6.1's components (LifecycleBoard,
// StageColumn, StageCard) and §6.1's helpers (taskAtStage, taskStateAt,
// currentStage). Authored here so §6.1 typechecks without GobbyTask. §6.2's
// extended GobbyTask is structurally compatible: GobbyTask extends LifecycleTask
// by being a superset, so passing `GobbyTask[]` where `LifecycleTask[]` is
// expected is valid TypeScript without an explicit `extends` clause.
export interface LifecycleTask {
  id: string
  task_type: string
  state?: { is_blocked?: boolean }
  stages?: StageStateView[]
}

// Resolver: derives the legal next-step action from the current row state and
// its review policy. Mirrors §2.1's transition matrix on the web side so the
// drag handler asks the backend for exactly one legal step at a time.
export function resolveAdvanceAction(
  currentState: StageState5,
  reviewPolicy: ReviewPolicy,
): StageAdvanceAction | null {
  if (currentState === 'ready')           return 'start'
  if (currentState === 'in_progress')     return reviewPolicy === 'required' ? 'submit_for_review' : 'complete'
  if (currentState === 'needs_review')    return 'approve_review'      // illegal on policy=none rows; resolver only fires on rows that already reached needs_review, which can only happen on required/optional policy
  if (currentState === 'review_approved') return 'complete'            // dispatcher's <stage>_advance_rule fires the same transition server-side; web layer's drag is a manual override
  if (currentState === 'done')            return null                  // terminal — no advance available
  return null
}
```

```typescript
// LifecycleBoard.tsx
import {
  resolveAdvanceAction,
  type StageAdvanceAction,
  type LifecycleTask,
} from '../../lib/stageActions'  // shared helper authored in this deliverable; see also §6.2 useTasks contract

interface LifecycleBoardProps {
  tasks: LifecycleTask[]  // GobbyTask is structurally compatible: §6.2 extends GobbyTask with `stages?: StageStateView[]`, making it a superset of LifecycleTask. §6.1 typechecks without §6.2.
  registry: StageRegistryEntry[]
  onSelectTask: (id: string) => void
  // The drag handler in StageCard reads (currentState, reviewPolicy) from
  // the per-task manifest, calls resolveAdvanceAction to derive the next legal
  // step, and forwards (taskId, stageName, action) to onAdvanceStage. Cards
  // whose resolver returns null are drag-disabled. The PATCH route keyed by
  // `action` lives in §2.4; the hook implementation lives in §6.2.
  onAdvanceStage?: (taskId: string, stageName: string, action: StageAdvanceAction) => void
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
import type { StageRowState } from '../../lib/stageActions'  // shared helper authored in this deliverable

function StageColumn({ stage, tasks, ... }: StageColumnProps) {
  // Per-policy grouping. `policy=required` columns render the full 5-state
  // chain; `policy=none` columns render only the 3 actionable states. The
  // `optional` policy renders the 5-state chain too (the row may flow
  // through review if the work agent submits, or skip review if it directly
  // completes).
  const isRequiredOrOptional = stage.review_policy === 'required' || stage.review_policy === 'optional'
  const grouped: Partial<Record<StageRowState, Task[]>> = {
    ready:           tasks.filter(t => taskStateAt(t, stage.name) === 'ready'),
    in_progress:     tasks.filter(t => taskStateAt(t, stage.name) === 'in_progress'),
    ...(isRequiredOrOptional && {
      needs_review:    tasks.filter(t => taskStateAt(t, stage.name) === 'needs_review'),
      review_approved: tasks.filter(t => taskStateAt(t, stage.name) === 'review_approved'),
    }),
    done:            tasks.filter(t => taskStateAt(t, stage.name) === 'done'),
  }
  // Render order top-to-bottom: ready (pale) → in_progress (accent) →
  // needs_review (review-accent, only on required/optional) → review_approved
  // (review-accent-strong, only on required/optional) → done (collapsed by
  // default via toggle, summary count visible).
}
```

```typescript
// StageCard.tsx
function StageCard({ task, stageName, stage, columnState, onAdvanceStage, ... }: StageCardProps) {
  const isBlocked = task.state?.is_blocked
  // Blocked tasks render with a blocked badge/overlay; they stay in their
  // current stage column and do NOT move to a synthetic blocked column.
  // The badge shows whether the block is from an open dependency or from
  // is_escalated, with a tooltip naming the blocker.
  //
  // Drag right resolves the next legal action via resolveAdvanceAction
  // (shared helper from web/src/lib/stageActions.ts) using the row's
  // current state and the stage's review_policy, then calls
  // onAdvanceStage(task.id, stageName, action). If the resolver returns
  // null (terminal state), the card is drag-disabled. Blocked tasks are
  // drag-disabled regardless of resolver result; the badge tooltip
  // explains why so the user can't accidentally advance over a blocker.
  // The 422 IllegalStageTransitionError payload from the PATCH route
  // surfaces via a tooltip per §6.1.3.
}
```

Blocked-task default visibility: blocked tasks are **shown by default** in their current stage column. A "Hide blocked" toggle in the board toolbar removes them from the rendered set; toggle state persists per user via `localStorage` keyed by `lifecycle-board:hide-blocked`. The default is "show" because the kanban's primary value is workflow visibility — `list_ready_tasks` and `suggest_next_task` already serve the actionable-only view for users who want filtered "what should I work on" output. Hiding by default risks a silently-growing stalled backlog, which the visibility-first design avoids.

Stage state and blocked-ness are orthogonal projections (see Constraints): a task can be `(stage='development', state='ready', is_blocked=true)` or `(stage='development', state='in_progress', is_blocked=true)`, and these mean meaningfully different things. The card's column position tells you the pipeline stage; the tri-state group within the column tells you work progress; the blocked badge tells you about external blockers. None of these axes collapses into the others.

Helpers (in `web/src/lib/stageActions.ts` — co-located with the types they consume so §6.1 owns the full helper surface; §6.2 imports these alongside `LifecycleTask` and `StageStateView`):

```typescript
// web/src/lib/stageActions.ts (continued — same module as the types/resolver above)

export function taskAtStage(task: LifecycleTask, stageName: string): boolean {
  return task.stages?.some(r => r.stage_name === stageName) ?? false
}

export function taskStateAt(task: LifecycleTask, stageName: string): StageRowState | undefined {
  return task.stages?.find(r => r.stage_name === stageName)?.state
}

export function currentStage(task: LifecycleTask): { name: string; state: StageRowState } | null {
  // Leftmost row by position whose state != 'done'.
  if (!task.stages) return null
  const sorted = [...task.stages].sort((a, b) => a.position - b.position)
  const next = sorted.find(r => r.state !== 'done')
  return next ? { name: next.stage_name, state: next.state } : null
}
```

Swimlanes by `task_type`: render one row per distinct `task_type` in the visible task set. Within each lane, render the columns. Empty lanes are hidden.

The `done` group within each column collapses by default to one summary row showing the count; click to expand. Reuses the `details/summary` HTML pattern or a small toggle component — match nearby disclosure patterns in `web/src/components/`.

**Acceptance:**

- 6.1.1 — Three new components exist with the declared prop shapes. file: `web/src/components/tasks/LifecycleBoard.tsx`, `web/src/components/tasks/StageColumn.tsx`, `web/src/components/tasks/StageCard.tsx`. symbol: `LifecycleBoard`, `StageColumn`, `StageCard`.
- 6.1.2 — Columns render only the stages present in any visible task's manifest. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_visible_stage_filtering`.
- 6.1.3 — Per-column state grouping renders top-to-bottom in this order: `ready`, `in_progress`, `needs_review`, `review_approved`, `done`. Stages with `review_policy=none` render only three groups (`ready`, `in_progress`, `done`) — no `needs_review` or `review_approved` group is rendered for `none`-policy columns regardless of whether the task set contains rows in those states (the data model never permits them on a `none`-policy row, but the render layer ALSO defends against malformed data by hiding the groups). The `done` group collapses by default to a one-line summary showing the count; click to expand. Drag-to-advance respects per-row `review_policy`: dragging right on a `policy=required` card walks the full chain `ready → in_progress → needs_review → review_approved → done` (one drag advances one step); on `policy=none`/`optional` cards, drag advances `ready → in_progress → done`. Drag attempts that violate legality surface a tooltip with the constraint reason (`IllegalStageTransitionError` payload). test: `web/src/components/tasks/__tests__/StageColumn.test.tsx::test_5_state_grouping_for_required_policy`, `web/src/components/tasks/__tests__/StageColumn.test.tsx::test_3_state_grouping_for_none_policy`, `web/src/components/tasks/__tests__/StageColumn.test.tsx::test_done_group_collapsed_by_default`, `web/src/components/tasks/__tests__/StageColumn.test.tsx::test_drag_advance_required_policy_walks_full_chain`, `web/src/components/tasks/__tests__/StageColumn.test.tsx::test_drag_advance_none_policy_walks_3_state_chain`, `web/src/components/tasks/__tests__/StageColumn.test.tsx::test_illegal_drag_surfaces_tooltip`.
- 6.1.4 — Blocked tasks render in their current column with a blocked badge by default; the badge tooltip names the blocker (open upstream dep or escalation reason). test: `web/src/components/tasks/__tests__/StageCard.test.tsx::test_blocked_badge_default_visible`.
- 6.1.4a — A "Hide blocked" toolbar toggle removes blocked tasks from the rendered set; toggle state persists in `localStorage['lifecycle-board:hide-blocked']`. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_hide_blocked_toggle_persists`.
- 6.1.4b — Drag-to-advance is disabled on blocked cards; attempting to drag surfaces the badge tooltip. test: `web/src/components/tasks/__tests__/StageCard.test.tsx::test_blocked_drag_disabled`.
- 6.1.5 — Drag right on a card resolves the next legal action via `resolveAdvanceAction(currentState, stage.review_policy)` (shared helper from `web/src/lib/stageActions.ts`, authored in this deliverable per 6.1.5a) and calls `onAdvanceStage(task.id, stageName, action)`. The resolver returns: `ready → 'start'`; `in_progress → 'submit_for_review'` for `policy=required`, `'complete'` for `policy=none`/`policy=optional`; `needs_review → 'approve_review'`; `review_approved → 'complete'`; `done → null`. A null return drag-disables the card. The signature is `(taskId: string, stageName: string, action: StageAdvanceAction) => void` — matches `LifecycleBoardProps.onAdvanceStage` and §6.2's `advanceStage(taskId, stageName, action)` exactly. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_drag_advance_calls_three_arg_signature`, `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_drag_advance_resolves_action_per_policy`, `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_drag_advance_disabled_when_resolver_returns_null`, `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_drag_advance_action_arg_matches_resolver_output`.
- 6.1.5a — `web/src/lib/stageActions.ts` is created in this deliverable and exports `StageState5`, `StageRowState` (alias of `StageState5`), `ReviewPolicy`, `StageAdvanceAction`, `StageStateView`, `LifecycleTask`, `resolveAdvanceAction`, `taskAtStage`, `taskStateAt`, and `currentStage` exactly as documented in the "Shared drag-action helper" block above. The resolver returns `'start'` for `ready`, `'submit_for_review'` for `in_progress` on `policy=required` and `'complete'` for `in_progress` on `policy=none`/`policy=optional`, `'approve_review'` for `needs_review`, `'complete'` for `review_approved`, and `null` for `done`. `StageStateView` and `LifecycleTask` are authored here (not in §6.2) so §6.1's components typecheck self-contained against `LifecycleTask[]` without §6.2's `GobbyTask.stages` extension; §6.2 re-imports both types when extending `GobbyTask`. §6.1's components (`LifecycleBoard.tsx`, `StageColumn.tsx`, `StageCard.tsx`) import the types and helpers from this module; §6.2's `useTasks.ts` re-imports the same module without redefining any of the exports. file: `web/src/lib/stageActions.ts`. symbol: `StageState5`, `StageRowState`, `ReviewPolicy`, `StageAdvanceAction`, `StageStateView`, `LifecycleTask`, `resolveAdvanceAction`, `taskAtStage`, `taskStateAt`, `currentStage`. test: `web/src/lib/__tests__/stageActions.test.ts::test_resolve_advance_required_chain`, `web/src/lib/__tests__/stageActions.test.ts::test_resolve_advance_none_chain`, `web/src/lib/__tests__/stageActions.test.ts::test_resolve_advance_optional_chain`, `web/src/lib/__tests__/stageActions.test.ts::test_resolve_advance_returns_null_on_done`, `web/src/lib/__tests__/stageActions.test.ts::test_stage_row_state_aliases_stage_state5`, `web/src/lib/__tests__/stageActions.test.ts::test_stage_state_view_shape`, `web/src/lib/__tests__/stageActions.test.ts::test_lifecycle_task_minimal_shape`, `web/src/lib/__tests__/stageActions.test.ts::test_task_helpers_consume_lifecycle_task`, `web/src/lib/__tests__/stageActions.test.ts::test_module_is_only_authoring_site` (negative — greps `web/src/hooks/useTasks.ts` and other §6.2 targets to assert no duplicate `export function resolveAdvanceAction`, `export type StageAdvanceAction`, `export interface StageStateView`, or `export interface LifecycleTask` declarations exist outside `web/src/lib/stageActions.ts`).
- 6.1.6 — Swimlanes by `task_type` render with empty lanes hidden. test: `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_swimlanes`.
- 6.1.7 — Category filter (toolbar control) hides columns whose `task_stages_registry.category` is not in the active selection; deselecting all categories renders no columns. behavior: "category filter drives column visibility" verified in `web/src/components/tasks/__tests__/LifecycleBoard.test.tsx::test_category_filter_hides_columns`.
- 6.1.8 — Visual surfaces (column header chrome, tri-state palette, badge ramp, blocked overlay, swimlane dividers, drag-preview shadow) conform to the `impeccable` skill's tokens; `.impeccable.md` was consulted before authoring CSS/JSX. behavior: "design tokens consumed from impeccable" verified by code review notes in PR description and `web/src/styles/lifecycle-board.css` referencing only token variables (no raw hex values).
- 6.1.9 — Automated CSS lint test asserts that `web/src/styles/lifecycle-board.css` (and any sibling stylesheet introduced for `LifecycleBoard`/`StageColumn`/`StageCard`) contains no raw color literals — the test fails on any line outside a CSS comment matching `/#[0-9a-fA-F]{3,8}\b/` or RGB/HSL function form with literal numeric channels. Enforces token-only authoring without relying on PR-description review. file: `web/src/styles/lifecycle-board.css`. test: `web/src/__tests__/lifecycle-board-css-lint.test.ts::test_no_raw_color_literals`.

### 6.2 useTasks denormalized stage manifest + new filters [category: code] (depends: 2.4, 6.1)

`kind: deliverable`

Target: `web/src/hooks/useTasks.ts`, `web/src/hooks/useStagesRegistry.ts` (new)

Extend `GobbyTask` (`web/src/hooks/useTasks.ts:10-39` area) with:

```typescript
import type {
  StageState5,
  ReviewPolicy,
  StageStateView,
  LifecycleTask,
} from '../lib/stageActions'  // all shared types authored in §6.1's stageActions.ts; do NOT redefine here

export type { StageState5, ReviewPolicy, StageStateView, LifecycleTask }  // re-export for legacy import paths

export interface GobbyTask {
  // existing fields...
  stages?: StageStateView[]  // populated by GET /api/tasks?include_stages=1; type imported from §6.1's stageActions.ts. GobbyTask is structurally a superset of LifecycleTask, so consumers typed `LifecycleTask[]` accept `GobbyTask[]` without an explicit `extends` clause.
}
```

Update `fetchTasks` in `useTasks` to pass `include_stages=1` whenever the kanban view is mounted. Add `stage` and `stage_state` query params to `buildParams` for filtered fetches.

`StageAdvanceAction` and `resolveAdvanceAction` are imported from the shared helper authored in §6.1 (`web/src/lib/stageActions.ts`); §6.2 does NOT re-author them. The §6.1 drag handler resolves the action and `useTasks`' `advanceStage` mutator forwards it to the backend without re-resolving. Mutation helpers in `useTasks.ts`:

```typescript
import { type StageAdvanceAction } from '../lib/stageActions'  // single-source helper authored by §6.1

async function advanceStage(taskId: string, stageName: string, action: StageAdvanceAction): Promise<void> {
  await fetch(`${baseUrl}/api/tasks/${taskId}/stages/${stageName}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  })
  // The PATCH route maps `action` to the appropriate StageStatesManager
  // method per §2.4: start → start_stage; submit_for_review → submit_for_review;
  // approve_review → approve_review; complete → complete_stage. Any
  // illegal transition returns 422 with an IllegalStageTransitionError
  // payload (see §2.4 acceptance) which the drag handler surfaces as a
  // tooltip per §6.1.3.
}

async function failStage(taskId: string, stageName: string, reason: string): Promise<void> { ... }
async function startStage(taskId: string, stageName: string): Promise<void> { ... }
```

The drag handler in `LifecycleBoard` reads `(task.stages[stageName].state, stage.review_policy)` from the per-task manifest, calls `resolveAdvanceAction` to get the next legal action, then invokes `advanceStage(taskId, stageName, action)`. A `null` return from the resolver disables drag (the card stays in place). The PATCH route's 422 payload (typed `IllegalStageTransitionError` from §2.4) surfaces as the §6.1.3 tooltip.

New hook `useStagesRegistry` fetches `GET /api/stages/registry` once on mount, caches in module-level state, and returns `{registry, isLoading, error}`.

WebSocket `task_event` handler (`useTasks` line ~506 area, `useWebSocketEvent('task_event', ...)`) re-fetches on `stage_changed` events as well as existing `task_event` types. Backend already broadcasts these per 2.4.5.

**Acceptance:**

- 6.2.1 — `GobbyTask.stages` field populated when `include_stages=1` query param is set. file: `web/src/hooks/useTasks.ts`. test: `web/src/hooks/__tests__/useTasks.test.ts::test_stages_populated`.
- 6.2.2 — `advanceStage(taskId, stageName, action)` accepts a typed `StageAdvanceAction` parameter (imported from §6.1's `web/src/lib/stageActions.ts` — see acceptance 6.1.5a, which owns the resolver-behavior tests) and PATCHes `{ action }` to `/api/tasks/{taskId}/stages/{stageName}`. `failStage` and `startStage` mutators retain their existing signatures. The PATCH route's 422 IllegalStageTransitionError payload propagates to the drag handler. file: `web/src/hooks/useTasks.ts`. test: `web/src/hooks/__tests__/useTasks.test.ts::test_advance_stage_with_action_param`, `web/src/hooks/__tests__/useTasks.test.ts::test_advance_stage_imports_stage_advance_action_from_shared_helper`, `web/src/hooks/__tests__/useTasks.test.ts::test_advance_stage_422_propagates_typed_error`, `web/src/hooks/__tests__/useTasks.test.ts::test_fail_stage_mutator`, `web/src/hooks/__tests__/useTasks.test.ts::test_start_stage_mutator`.
- 6.2.3 — `useStagesRegistry` fetches once and caches. file: `web/src/hooks/useStagesRegistry.ts`. test: `web/src/hooks/__tests__/useStagesRegistry.test.ts::test_caches_response`.
- 6.2.4 — `stage_changed` WS events trigger task re-fetch. test: `web/src/hooks/__tests__/useTasks.test.ts::test_ws_stage_changed_refetches`.

### 6.3 Mount LifecycleBoard, retire `taskState.ts` legacy types [category: code] (depends: 6.1, 6.2)

`kind: deliverable`

Target: `web/src/components/tasks/TasksPage.tsx`, `web/src/lib/taskState.ts`, `web/src/lib/__tests__/taskState.test.ts` (existing — taskState unit test; legacy-symbol assertions removed alongside the symbol deletion in `taskState.ts`), `web/src/components/tasks/KanbanBoard.tsx`, `web/src/components/tasks/__tests__/KanbanBoard.test.tsx`, `web/src/components/tasks/PriorityBoard.tsx` (existing — non-kanban board surface; port consumer of `getTaskBucket`/`TASK_BUCKET_*`/`lifecycle_stage` to `current_stage` from `useTasks` and stage-trimmed `getCanonicalTaskState`), `web/src/components/tasks/ActivityPulse.tsx`, `web/src/components/tasks/TaskResults.tsx`, `web/src/components/tasks/TaskBadges.tsx`, `web/src/components/tasks/TaskStatusStrip.tsx`, `web/src/components/tasks/TaskDetail.tsx`, `web/src/components/tasks/TaskTree.tsx`, `web/src/components/tasks/GanttChart.tsx`, `web/src/components/tasks/DigestView.tsx`, `web/src/components/tasks/DependencyGraph.tsx`, `web/src/components/tasks/ReasoningTimeline.tsx`, `web/src/components/tasks/AuditLog.tsx` (existing — task-display surfaces; each consumer is ported from `TaskBucket`/`getTaskBucket`/`TASK_BUCKET_LABELS`/`TASK_BUCKET_ORDER`/`lifecycle_stage` to the §6.2 `useTasks` `current_stage` projection plus stage-trimmed `getCanonicalTaskState`. Display semantics preserved: bucket-grouping callers read `current_stage.name`; bucket-label callers read the registry entry's display label; stage-progression callers read `task.stages` per §6.2's projection), `web/src/components/activity/TasksTab.tsx`, `web/src/components/activity/__tests__/TasksTab.test.tsx`, `web/src/components/activity/TasksTabDetailPanel.tsx`, `web/src/components/agents/AgentPortfolioPage.tsx` (existing — agent/activity panels; same porting recipe as the task-display surfaces above), `web/src/hooks/useTasks.ts` (existing — §6.2 owns the `current_stage`/`task.stages` shape; §6.3 strips any residual `getTaskBucket`/`lifecycle_stage` reads from the hook itself if the §6.2 rewrite did not already remove them, and asserts the post-§6.3 hook surface no longer re-exports legacy types)

Replace the `viewMode === 'kanban'` branch in `TasksPage.tsx` (around line 601 — search the literal `viewMode === 'kanban'`) with `LifecycleBoard`. Pass `tasks`, the registry from `useStagesRegistry`, and the new `advanceStage`/`failStage` mutators from `useTasks`.

```typescript
} : viewMode === 'kanban' ? (
  <LifecycleBoard
    tasks={subtreeRootId ? kanbanTasks : displayTasks}
    registry={registry}
    onSelectTask={setSelectedTaskId}
    onAdvanceStage={advanceStage}     // (taskId, stageName, action) — caller supplies the resolved StageAdvanceAction
    onFailStage={failStage}
  />
) : ...
```

`LifecycleBoard` internally invokes `resolveAdvanceAction(currentState, reviewPolicy)` for the dragged card and forwards the resolved action to `onAdvanceStage`; cards whose resolver returns `null` are drag-disabled.

Delete from `web/src/lib/taskState.ts`:

- `TaskLifecycleStage` (line ~1)
- `TaskBucket` (lines ~39-45)
- `TASK_BUCKET_LABELS` (lines ~63-70)
- `TASK_BUCKET_ORDER` (lines ~54-61)
- `getTaskBucket` (lines ~136-144)
- `normalizeLifecycleStage` (lines ~90-96) and any other lifecycle helpers

Keep `CanonicalTaskState` minus the `lifecycle_stage` field; rename to make the omission obvious if useful (`TaskState` works). Keep `getCanonicalTaskState` for reading task state for badges.

Delete `web/src/components/tasks/KanbanBoard.tsx` and its test file `KanbanBoard.test.tsx`. The new `LifecycleBoard` test file from 6.1 replaces them.

The `moveTaskToBucket` function in `TasksPage.tsx` (around line 372 — search the literal `const moveTaskToBucket = useCallback`) is replaced by inline `advanceStage` / `failStage` calls bound to drag handlers in `LifecycleBoard`. Existing per-bucket transition functions (`reopenTask`, `deEscalateTask`, `claimTask`, `markTaskNeedsReview`, `markTaskReviewApproved`, `escalateTask`, `closeTask`) are no longer wired to drag — their other callers stay (sidebar buttons, modals, etc.).

**Acceptance:**

- 6.3.1 — `TasksPage.tsx` mounts `LifecycleBoard` for `viewMode === 'kanban'`. file: `web/src/components/tasks/TasksPage.tsx`. test: `web/src/components/tasks/__tests__/TasksPage.test.tsx::test_kanban_mode_renders_lifecycle_board`.
- 6.3.2 — `taskState.ts` legacy symbols deleted. file: `web/src/lib/taskState.ts`. test: TypeScript compile passes; grep regression `web/src/__tests__/test_legacy_symbols_removed.test.ts::test_no_task_bucket_imports`.
- 6.3.3 — `KanbanBoard.tsx` and `KanbanBoard.test.tsx` are deleted. behavior: "old kanban component absent" verified by `git status`/`grep -r "KanbanBoard"` returning no source matches in `web/src/`.
- 6.3.4 — `pnpm build` succeeds; `pnpm test` runs `LifecycleBoard.test.tsx` instead of the deleted `KanbanBoard.test.tsx`. test: CI pipeline output shows new file in coverage.
- 6.3.5 — Comprehensive web legacy-consumer sweep gate: after the per-consumer ports in this deliverable land, the audit grep `grep -rln 'getTaskBucket\|TaskBucket\|TASK_BUCKET_\|lifecycle_stage' web/src/` returns ZERO matches. This is the gate §5.3.8 depends on. The full set of consumers ported by §6.3 is the union of the target inventory above (the kanban board + the 18 listed display/board/activity/hook surfaces); any future consumer added between §6.3 and §5.3 is the responsibility of whichever stage adds it (it MUST land already manifest-native or §5.3.8 fails). file: every file in the §6.3 target inventory. behavior: legacy web consumers absent. test: `web/src/__tests__/test_legacy_symbols_removed.test.ts::test_no_task_bucket_imports_in_web_src`, `web/src/__tests__/test_legacy_symbols_removed.test.ts::test_no_lifecycle_stage_reads_in_web_src`, `web/src/__tests__/test_legacy_symbols_removed.test.ts::test_audit_grep_returns_zero_matches`.

## P7 Cleanup

`kind: framing`

**Goal**: Remove deprecated `stage-:<name>` label handling, temporary migration helpers, and dead lifecycle/status code. Documentation pass.

### 7.1 Remove `stage-:<name>` label handling and migration helpers [category: refactor] (depends: 5.3, 6.3)

`kind: deliverable`

Target: `src/gobby/build/service.py`, `src/gobby/dispatch/rules.py`, `src/gobby/storage/migrations.py`, `src/gobby/mcp_proxy/tools/tasks/_front_half.py`, `src/gobby/tasks/expansion/_compile.py`, `src/gobby/tasks/expansion/_apply.py`, `src/gobby/tasks/expansion/_common.py`, `src/gobby/tasks/expansion_service.py`, `src/gobby/storage/tasks/_crud.py`, anywhere else `stage-:` appears

Grep `stage-:` across the codebase. Every read site that interpreted these labels (build profile resolution, dispatcher skip checks, CLI/HTTP introspection) must be deleted; the data was migrated to `task_stage_states` in Phase 2.2 and the labels were dropped per 2.2.6.

Specific call sites to scrub (results from current grep, point of departure for the implementing agent):

- `src/gobby/dispatch/rules.py:20` (`_SKIP_PREFIX = "stage-:"`) — delete the constant; ripple through.
- Any helper in `src/gobby/build/service.py` that translated profiles to labels — replace with manifest skip lists (Phase 3.2 already does this; this task removes the legacy fallback).
- Migration helpers in `src/gobby/storage/migrations.py` that read `stage-:` labels (only the backfill helper from Phase 2.2 — keep that one as it's a frozen historical record).

`planning-round:N` and `qa-attempts:N` labels are now redundant (replaced by per-stage `work_attempt_count` and `review_round_count` on `task_stage_states`; `planning-round:N` was the planning stage's review-round counter, which now lives as `task_stage_states.review_round_count` on the `planning` row; `qa-attempts:N` was the QA review-round counter, which now lives as `task_stage_states.review_round_count` on the relevant work stage). Drop these from every task in a final cleanup migration (version 237). Read sites: any `_front_half.py` references to `PLANNING_ROUND_LABEL_PREFIX` are deleted; readers move to the appropriate stage-row counter (`work_attempt_count` for work-attempt callers; `review_round_count` for review-round callers).

**Acceptance:**

- 7.1.1 — `_SKIP_PREFIX` constant and all `stage-:` / `_skipped_stages` reads deleted from runtime code. The audit grep is scoped to `src/gobby/dispatch/`, `src/gobby/build/`, `src/gobby/cli/`, `src/gobby/mcp_proxy/`, `src/gobby/servers/`, `src/gobby/tasks/expansion/` (round-11 F2: `_common.py`, `_compile.py`, `_apply.py`), `src/gobby/tasks/expansion_service.py` (round-12 F2: facade currently re-exports `_skipped_stages`; the import and `__all__` entry are removed), `src/gobby/storage/tasks/` (round-11 F2: `_crud.py::_skipped_stages` and `cascade_build_state_to_subtree`), and bundled agent/skill instruction surfaces under `src/gobby/install/shared/workflows/agents/` and `src/gobby/install/shared/skills/` (any YAML/SKILL.md text mentioning `_skipped_stages` or `stage-:`). Combined audit returns zero matches across all listed paths. Migrations (`src/gobby/storage/migrations.py`) and migration-specific tests (`tests/storage/test_migration_*.py`) are explicitly EXEMPT — the migration-234 backfill helper preserves `stage-:<name>` label reads as a frozen historical record so pre-cutover databases replay correctly (acceptance 7.1.4 covers the positive regression; acceptance 7.1.5 covers manifest-based replacements for the deleted runtime readers). file: `src/gobby/dispatch/rules.py`, `src/gobby/tasks/expansion/_common.py`, `src/gobby/tasks/expansion/_compile.py`, `src/gobby/tasks/expansion/_apply.py`, `src/gobby/tasks/expansion_service.py`, `src/gobby/storage/tasks/_crud.py`. test: `tests/test_no_stage_skip_labels.py::test_grep_returns_empty_for_full_runtime_scope`, `tests/test_no_stage_skip_labels.py::test_grep_returns_empty_for_bundled_agent_instructions`, `tests/test_no_stage_skip_labels.py::test_expansion_service_facade_does_not_export_skipped_stages`, `tests/test_no_stage_skip_labels.py::test_migration_234_helper_intact_in_historical_scope`.
- 7.1.2 — Migration 237 drops `planning-round:N` and `qa-attempts:N` labels from every task. file: `src/gobby/storage/migrations.py`. test: `tests/storage/test_migration_237_label_cleanup.py::test_legacy_labels_dropped`.
- 7.1.3 — `PLANNING_ROUND_LABEL_PREFIX` constant deleted; readers updated to read the appropriate per-stage counter on `task_stage_states` — `review_round_count` for callers that previously read `planning-round:N` / `qa-attempts:N` (those labels tracked review iterations); `work_attempt_count` for callers that previously read a work-iteration counter. No reader reads a single `attempt_count` column (it does not exist after the §1.1 split). file: `src/gobby/mcp_proxy/tools/tasks/_front_half.py`. test: `tests/mcp_proxy/tools/tasks/test_front_half_attempt_count.py::test_no_label_reads`, `tests/mcp_proxy/tools/tasks/test_front_half_attempt_count.py::test_planning_round_callers_read_review_round_count`, `tests/mcp_proxy/tools/tasks/test_front_half_attempt_count.py::test_qa_attempts_callers_read_review_round_count`.
- 7.1.4 — Migration 234 backfill replay preservation: `_backfill_task_stage_states_from_legacy` STILL honors `stage-:<name>` skip labels when replayed against a pre-cutover fixture DB post-Phase-7.1 cleanup. The historical helper is a frozen migration record; its label-reading code paths are not deleted by 7.1.1's runtime cleanup. Test fixture: a synthetic pre-cutover task carrying labels like `stage-:test_arch` and `stage-:expansion_qa` produces a backfilled manifest equal to the task-type default minus those two stages. file: `src/gobby/storage/migrations.py` (helper preserved). test: `tests/storage/test_migration_234_backfill.py::test_replay_against_pre_cutover_db_honors_legacy_skip_labels`, `tests/storage/test_migration_234_backfill.py::test_skip_label_reads_in_helper_survive_phase_7_1_cleanup`.
- 7.1.5 — Manifest-based replacements for deleted runtime label readers (round-11 F2 / round-12 F2 positive regression). The runtime code paths that previously read `stage-:<name>` labels are ported to read the manifest (`task.stages` / `task_stage_states`) directly, with no behavior change in skipped-stage handling: (a) `src/gobby/tasks/expansion/_compile.py::_build_prompt_context` reads the parent task's `task.stages` to decide which stages to include in the expansion prompt context, no longer references `_skipped_stages` or `stage-:` labels. (b) `src/gobby/tasks/expansion/_apply.py::_complete_dev_only_run` calls `complete_stage(task_id, 'expansion')` on the parent task's manifest to advance the dev-only expansion bypass, no longer writes legacy `lifecycle = 'in_development'` (covered alongside acceptance 5.3.9). (c) `src/gobby/storage/tasks/_crud.py::cascade_build_state_to_subtree` writes child manifests via `StageStatesManager.initialize_manifest` rather than emitting `stage-:` labels. (d) **(round-12 F2)** `src/gobby/tasks/expansion_service.py` facade drops its `_skipped_stages` import and removes it from `__all__`; any test asserting the facade exposes `_skipped_stages` is updated to assert it does NOT (post-cleanup negative regression). The `_skipped_stages` helpers in both `_common.py` and `_crud.py` are deleted (covered by 7.1.1's grep). Bundled agent/skill instruction surfaces that reference `_skipped_stages` are rewritten to point at the manifest read path (or removed if the reference is no longer accurate). file: `src/gobby/tasks/expansion/_compile.py`, `src/gobby/tasks/expansion/_apply.py`, `src/gobby/tasks/expansion_service.py`, `src/gobby/storage/tasks/_crud.py`. test: `tests/tasks/expansion/test_compile_uses_manifest.py::test_prompt_context_reads_stages_not_labels`, `tests/tasks/expansion/test_apply_dev_only.py::test_complete_dev_only_run_via_complete_stage`, `tests/tasks/test_expansion_service_facade.py::test_facade_does_not_export_skipped_stages`, `tests/storage/tasks/test_cascade_build_state.py::test_cascade_uses_initialize_manifest`, `tests/storage/tasks/test_cascade_build_state.py::test_cascade_no_legacy_label_writes`.

### 7.2 Documentation pass [category: docs] (depends: 7.1)

`kind: deliverable`

Target: `CLAUDE.md`, `src/gobby/install/shared/skills/plan-draft/SKILL.md`, `docs/contracts/plan-coverage.md`, `docs/guides/dispatch.md` (new or extend)

Update written documentation to reflect the manifest model.

`CLAUDE.md` "Dispatch Architecture" section: replace any mention of `lifecycle` / `status` axes with stage-manifest semantics. Specifically the list of fields (`allow_automation`, `yolo`, `isolation`) gains `stages` (manifest) as a peer. Profile bundles documented as Phase 3.2.

`plan-draft/SKILL.md`: refresh the canonical stage list in its "Phasing" guidance to match the registry's 11 stages (`ideation, research, architecture, prd, planning, test_arch, expansion, development, holistic_qa, pr, merge`). The dropped stages `adversarial_review`, `expansion_qa`, `code_review_qa` are removed from the skill's prose, examples, and any embedded table; review-policy semantics on the surviving stages replace them.

`docs/contracts/plan-coverage.md`: no changes for the coverage contract grammar itself, but if the doc references retired status values (`needs_review` → `code_review_qa.in_progress`, etc.), update those examples to match.

`docs/guides/dispatch.md` (new file if absent; extend if present): one-page architecture diagram + prose covering: registry → manifest → rule → action chain. Include the canonical stage list and the readiness/blocking projection definition. The doc lives under `guides/` (operator-facing how-to) rather than `architecture/` to match the project's documentation convention.

Update tests to read documentation references (no tests for prose, but the verification phase below cross-checks).

**Acceptance:**

- 7.2.1 — `CLAUDE.md` "Dispatch Architecture" section reflects the manifest model with no remaining `lifecycle`/`status` semantics. file: `CLAUDE.md`. behavior: "doc names task_stage_states and registry; no `(lifecycle, status)` tuple references in dispatcher prose" verified by manual review noted in PR description.
- 7.2.2 — `plan-draft` skill canonical stage list matches the surviving 11-stage registry (`ideation, research, architecture, prd, planning, test_arch, expansion, development, holistic_qa, pr, merge`); the dropped stages `adversarial_review`, `expansion_qa`, `code_review_qa` are removed from the skill's prose, examples, and any embedded table. file: `src/gobby/install/shared/skills/plan-draft/SKILL.md`. behavior: "skill stage list = registry stage list (11 stages, dropped review stages absent)" verified in `tests/skills/test_plan_draft_stage_list.py::test_matches_registry`, `tests/skills/test_plan_draft_stage_list.py::test_dropped_stages_absent`.
- 7.2.3 — `docs/guides/dispatch.md` exists and covers registry, manifest, rule chain, readiness projection. file: `docs/guides/dispatch.md`.
- 7.2.4 — `docs/guides/dispatch.md` (or a sibling under `docs/guides/`) calls out the orthogonality of `is_escalated` / `is_blocked` projections vs. stage state, with a worked example of escalate-then-de-escalate preserving the manifest row. file: `docs/guides/dispatch.md`.

## V1 Verification

`kind: verification`

End-to-end acceptance covers:

- **Schema integrity**: every migration runs forward on a fresh DB and on a fixture DB with representative `(lifecycle, status, labels)` tuples; resulting `task_stage_states` rows match the mapping table 2.2.2; CHECK constraint on `state` accepts the 5-value enum on every row regardless of policy.
- **Storage invariants**: position uniqueness, registry FK, transition state machine, counter-split semantics (`work_attempt_count` / `review_round_count`), policy-aware legality matrix — all enforced by `StageStatesManager` tests. `IllegalStageTransitionError` raised on every illegal `(state, transition, policy)` triple.
- **Type→default-stages resolution**: every existing and new task type resolves to declared defaults via `get_task_type_defaults`; the surviving 11-stage registry has `holistic_qa` only in the `epic` manifest.
- **Build-time override merge**: `--stages`, `--add-stage`, `--skip-stage`, `--stage <name>:max_review_rounds=N`, profile bundles all compose as documented in 3.2 and 2.5.
- **Readiness equivalence**: contract tests run old vs. new `list_ready_tasks`, `list_blocked_tasks`, `suggest_next_task`, `list_automation_candidates`, and `state.is_blocked` against the same fixture DB and assert identical task ID sets. `review_approved` rows do NOT satisfy upstream-completion checks.
- **Per-stage rule fan-out**: each `policy=required` stage (`planning`, `expansion`, `development`, `holistic_qa`, `pr`) has three rules in the rules list (`<stage>_work_rule`, `<stage>_review_rule`, `<stage>_advance_rule`). For `holistic_qa` the `_review_rule` is a defensive resume hook (re-spawns the agent if it crashed between its internal `submit_for_review` and `approve_review`); the agent's normal path still produces `in_progress → needs_review → review_approved` from inside its own run. `policy=none` stages (`ideation`, `research`, `architecture`, `prd`, `test_arch`, `merge`) have a single rule. The full set of rules covers every cell of the (stage × state) reachable matrix.
- **Dispatcher chain**: full 5-state delivery walk for an `epic` `holistic_qa.review_approved → holistic_qa.done → pr.ready → pr.in_progress → pr.needs_review → pr.review_approved → pr.done → merge.ready → merge.in_progress → merge.done → task closed` covered end-to-end on a fresh DB; PR rejected-then-approved cycle exercised. Leaf `feature` task walks `planning.in_progress → planning.needs_review → planning.review_approved → planning.done → test_arch → expansion → expansion.needs_review → expansion.review_approved → expansion.done → development → development.needs_review → development.review_approved → development.done → pr → merge → closed`.
- **Heartbeat-advance smoke** (covers the §2.6 sequencing bridge): given a fixture task at `planning.in_progress` (review_policy=required), calling the rewired `mark_task_needs_review` produces `planning.needs_review`; the next dispatcher heartbeat fires `planning_review_rule` and spawns `plan-adversary`. After `mark_task_review_approved`, the row is at `review_approved`; the next heartbeat fires `planning_advance_rule` calling `complete_stage('planning')`, advancing to `planning.done` and promoting `test_arch.ready → in_progress` via `auto_advance_ready_rule`.
- **API surface**: `GET /api/tasks?stage=development&stage_state=in_progress` returns expected set; `PATCH /api/tasks/{id}/stages/{name}` enforces 5-state transitions per `review_policy`, returning 422 with the `IllegalStageTransitionError` payload on policy/state violations; `record_pr_verdict` maps `approved → approve_review`, `rejected/needs_changes → reject_review`; `record_merge_result` writes artifacts and routes through `complete_stage('merge')` to `_close_task_in_txn(cascade=True)`.
- **Terminal non-merge types**: `research_spike` and `prd_doc` walk to their terminal stage and close cleanly via the §2.1 invariant 8 generic close path without ever reaching `merge`. `disabled_agent_escalation_rule` surfaces the §1.3 placeholder gap with reason `<stage>_no_agent`.
- **UI**: LifecycleBoard renders with seeded registry, drag-to-advance updates state via PATCH respecting per-row `review_policy` (5-state chain on `required`, 3-state on `none`/`optional`), swimlane filter by task_type hides empty rows, and pre-existing migrated tasks render from their stage rows; blocked tasks render with badges in their current column.
- **Performance**: kanban board fetch SQL keeps p99 under existing `KanbanBoard` baseline (denormalized stage manifest in single query, indexed on `(task_id, position)` and `(stage_name, state)`).
- **Dead-code regression**: grep/static tests fail if code writes old `status` / `lifecycle` values or calls removed lifecycle PR/merge tools after the cutover. The dropped registry stages `adversarial_review`, `expansion_qa`, `code_review_qa` do not appear in any post-cutover code path.
- **No regressions**: targeted runs of `tests/dispatch/`, `tests/tasks/`, `tests/storage/`, `tests/servers/routes/`, `tests/mcp_proxy/tools/tasks/`, plus `pnpm test` and `pnpm build` for the web bundle.

## Out of scope

`kind: framing`

- **Real agent behavior for the four discovery stages.** This epic ships disabled placeholder YAMLs (1.3) and tracking tasks (5.4); it does NOT author working `analyst`, `researcher`, `architect`, or `product-manager` agents. That work is owned by the `Discovery-stage agent registry` epic created in 5.4.
- **PR-Agent / rizzler-style PR review behavior** — owned by #13552, which targets the stage contract this epic delivers.
- **Re-implementing existing agents.** `planner`, `plan-adversary`, `test-architect`, `expansion-qa`, `qa-reviewer`, `holistic-reviewer`, `merge-orchestrator`, `merge-worker`, `backend-developer`, `frontend-developer`, `default`, `developer` already exist; they are referenced by the registry's `default_agent` slot but their YAMLs are not modified beyond, at most, comment updates referencing the new stage names.
- Cross-project / multi-tenant kanban.
- Per-stage time tracking, SLAs, due dates.
- Drag-and-drop reordering of stages within a task's manifest. Drag-to-advance state is in scope; drag-to-reorder positions is not.
