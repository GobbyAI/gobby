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
snapshot** taken at spawn/persona activation, and a small
`definition_revisions` invalidation-state table backing commit-visible,
cross-daemon cache invalidation. No new discriminator, no generic
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
   no partial indexes (`crates/gcore/assets/schema/baseline.sql:2375-2376`,
   indexes at `:3176-3186`).
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
6. Bundled agent file inventory at HEAD (re-verified 2026-08-12, adversary
   round 2 APR2-004): **25 files total — 21 stepful + 4 step-less**
   (comms-agent, default, goal-taskmaster, triage-agent). The four retired
   stepful agents (nightly-linter, nightly-test-fixer,
   plan-review-researcher-taskless, wiki-researcher) have no bundled file and
   survive only as soft-deleted hub rows, which is why the hub holds 29 agent
   rows of which 25 are stepful (21 bundled + 4 retired). File counts and hub
   counts are different inventories: derive migration counts from the hub,
   never from the bundled file inventory, and derive conversion lists from the
   filesystem, never from hub row counts.
7. `docs/plans/workflow-refactor.md` is a conflicting older design (generic
   `definition_registry` + inline step workflows) — superseded and deleted by
   this plan. `docs/reviews/cli-build-ops.md:56-60` falsely claims the new
   tables already exist — corrected in P7.
8. The epic's referenced test paths (`tests/workflows/test_rule_engine.py`,
`tests/workflows/test_session_defaults.py`) both exist and are extended or
retargeted in P7; only the new legacy-reference absence audit is written from
scratch there.

Research corrections from the 2026-08-12 refresh (verified against HEAD):

9. **Schema authority moved to Rust gcore** (commits `e322fc28c` "flatten
   migrations into post-M0 baseline", `144a85fd2` "add gcore schema authority",
   `0617b44ca` "delegate PostgreSQL schema authority to gdaemon").
   `src/gobby/storage/postgres_baseline_schema.sql` and
   `src/gobby/storage/migrations/` no longer exist. All DDL lives in
   `crates/gcore/assets/schema/baseline.sql` (frozen `BASELINE_VERSION = 375`),
   embedded in gdaemon and applied out-of-process with a release-pinned
   identity check. A tripwire test
   (`tests/storage/test_schema_contract.py::test_production_python_has_no_persistent_postgres_ddl`)
   forbids persistent DDL in production Python. P1 and every migration
   deliverable were rewritten against this mechanism; see Constraints.
10. **`workflow_states` is already gone.** The table is absent from the
    baseline and `get_claimed_task_owners`
    (`src/gobby/cli/tasks/_utils/claims.py`) already reads
    `tasks JOIN sessions`. The former P7.1 rewrite/drop slice for it is
    removed from this plan; only stale docstring cleanup remains (7.3).
11. **`enabled_user_modified` reconciliation column added** (`9071a6209`,
    2026-08-02) with `update_from_sync()`: a sticky user-toggle provenance bit
    that bundled sync respects when propagating template `enabled` defaults.
    The four definition domain tables carry it under the clearer name
    `enabled_pinned` (Decision Record); the legacy column keeps its old name
    until the P7 drop removes it.
12. **Web pipeline-definition callers moved.** The pipeline editor's fetch
    layer now lives in
    `web/src/components/activity/pipelines/PipelinesDefsActions.ts`
    (`f04e1eafd`, 2026-08-09); `usePipelineDefs.ts` has never existed. 6.2
    retargeted.
13. **Drifted anchors fixed in place**: the `workflow_instances=` log key
    moved from `agent_cleanup.py:454` to
    `src/gobby/agents/terminal_cleanup.py:180`; the `stdio_proxy.py`
    `/api/workflows/variables` literals moved from `:462`/`:476` to
    `:500`/`:514`. Body line references elsewhere were spot-verified on
    2026-08-12; symbols in Targets are the durable anchors.
14. **The `gobby-workflows` MCP registry has grown to 31 tools**: the 10
    generic tools 5.2 deletes or re-scopes plus 21 domain tools that already
    exist, confirming 5.2's premise that domain CRUD needs no new tools.
15. **The root `gobby export`/`gobby import` CLI is gone** (adversary round 2,
    BR-008): `src/gobby/cli/export_import.py` sits in `DEAD_PYTHON_PATHS`
    within the import-hygiene meta test, and a CLI contract test pins the
    commands' absence from root help. The Jul-29 draft's export/import
    vocabulary migration in 6.1 referenced a surface that no longer exists and
    is dropped rather than resurrected.
16. **#18974 landed same-session daemon-stop resume** (closed 2026-07-27;
    adversary round 2 APR2-001). A daemon-stop-terminalized run is relaunched
    as a new run on the **same child session** via `prepare_terminal_resume`
    (`src/gobby/agents/spawn.py:227`, called from
    `src/gobby/agents/resume_executor.py:192` with a CAS session rebind), and
    `cleanup_agent_runtime_state` (`src/gobby/agents/runtime_cleanup.py`)
    deletes workflow-instance rows only when `terminal_reason !=
    'daemon_stop'` — daemon-stop retains them so the resumed run continues at
    its step. `tests/agents/test_spawn_prepare_resume.py` pins the seam. The
    Jul-29 draft's §3.2 exclusion ("resume creates a new child session; the
    row does not follow it") described the pre-#18974 runtime and is
    rewritten: resume continuity is now a live contract this epic must
    preserve, not a deferred feature.
17. **After-commit callbacks already exist in the ambient transaction layer**
    (adversary round 2 APR2-015): `Transaction.after_commit`
    (`src/gobby/storage/hub/postgres_pool.py:304`) queues callbacks that fire
    only after the outermost pooled transaction commits and never on rollback,
    and the adapter routes `after_commit` to the current ambient transaction
    (`src/gobby/storage/hub/postgres.py:365`). The commit-visible revision
    contract binds to this existing seam instead of "after the manager's
    transaction block", which under ambient nesting is earlier than the true
    commit.

Decisions made with the operator (2026-07-26, mechanism updated 2026-08-12):

- **Staged per-domain migrations via re-armed gcore `MIGRATIONS`** (mechanism
  updated 2026-08-12; the Jul 26 staging survives, its carrier changed): the
  eight new tables ride `crates/gcore/assets/schema/baseline.sql` with
  idempotent guards, so both shapes coexist in the baseline mid-epic; each
  domain ships a guarded copy as an `EmbeddedMigration` entry in the same
  commit as its code cutover; a final `-- gobby:destructive` drop migration
  removes the legacy tables behind the backup-gated, epoch-fenced
  destructive apply (7.1),
  with the same-commit baseline edit removing their DDL. The drop migration
  RAISEs if legacy rows were written after their copy or never copied at all
  (the directional `legacy_copy_ledger` backstop for mid-epic writes through
  old surfaces). Live hubs upgrade in place.
- **Migration flatten is deferred**: re-flattening the epic's numbered
  migrations into the baseline before 0.5.0 ships is an explicit out-of-scope
  release chore, not part of this epic (operator decision 2026-08-12).
- **`enabled` + `enabled_pinned` on all four definition domains**
  (rules, agents, variables, pipelines) with the sync guard;
  `agent_step_workflows` carries neither — the child follows its parent agent
  (operator decision 2026-08-12). The typed tables rename the legacy
  `enabled_user_modified` bit to `enabled_pinned` — same semantics: the user
  pinned the value, sync keeps its hands off (operator decision 2026-08-12,
  enhancement round 1).
- **One step instance per session**: `UNIQUE(session_id)`, no `priority`
  column. Persona activation with a different agent replaces the instance;
  the same agent preserves it (re-confirmed 2026-08-12).
- **Sequencing**: P1 lands after the in-flight gcode agent-auth/overlay
  baseline change merges; the schema-artifact lockstep in Constraints re-arms
  on top of whatever that lands as (operator decision 2026-08-12).
- **Enhancement round 1 accepted in full** (operator decision 2026-08-12):
  commit-visible revision bumps with dual-domain child bumps (E1 — 1.3, 1.4,
  2.4); a parameterized `enabled_pinned` reconciliation contract over all four
  definition managers with a sync seam per domain (E2 — 1.2, 1.3, 2.3,
  4.1–4.3); staged generic-surface kind rejection in the same commit as each
  domain cutover, serializing 4.3 after 4.2 (E3 — 2.3, 4.1–4.3, 5.1, 5.2);
  persistent per-domain revisions with cross-daemon LISTEN/NOTIFY
  invalidation and a two-daemon test (E4 — 1.1, 1.4, E1).
- **Adversary attempt (evidence 3d6e2eee, run f9553136) — findings accepted
  in full, round not countable** (operator decision 2026-08-12, all five
  findings): explicit delete/retarget/absorb dispositions for every
  legacy-manager and WorkflowLoader test seam (BR-002 — 2.3, 2.4, 4.1, 4.2,
  5.1, 5.2, 7.1; BR-003 — 4.3); complete DefinitionRevisionListener lifecycle
  with a shutdown seam and injectable connection factory (BR-006 — 1.4);
  export/import CLI work dropped as targeting a deleted surface (BR-008 —
  6.1); the retargeted web suites promoted into 6.2's Targets (BR-011 — 6.2).
  The reviewer returned a summarized coverage attestation instead of the
  canonical `validate_plan_review_coverage` output, so the round result fails
  the server's round-result validation and can never finalize; the evidence
  was expired as a dead attempt and `completed_plan_review_rounds` stays 0.
  The five findings stand as these operator-approved repairs; no V1
  verification entry exists for this attempt because no canonical checkpoint
  fence can.
- **Adversary round 2 (evidence cd52d419, run 038eb7bb) — all 15 findings
  accepted, judged unattended** (operator delegation 2026-08-12: "use your
  best judgment on the findings"): every finding was verified against the
  repository before acceptance. Two findings were accepted with a narrower
  repair than proposed, recorded here: APR2-015's fix binds to the
  **existing** `after_commit` seam (correction 17) instead of building a new
  callback mechanism; APR2-008's lockstep targets were added to the five
  deliverables that register `MIGRATIONS` entries (2.3, 3.2, 4.1, 4.2, 4.3)
  and not to 1.5, which creates the entry convention with an empty list and
  therefore leaves `root_hash()` and the expected identity unchanged.
  APR2-006's ledger ships as the companion file itself rather than a §7.2
  audit clause: `verify_bootstrap_ledger` and adversary review are the
  contract's enforcement points, and the §7.2 audit test scans legacy tokens,
  not coverage artifacts.

## Constraints
`kind: framing`

- Pre-0.5: clean API/YAML break. No compatibility shims, no dual-write. The
  single sanctioned scaffolding: `register_agent_step_workflow` keeps writing
  generated legacy rows (reading `body.step_workflow`) from P2 until P3 task
  3.2 deletes it — this keeps every intermediate commit a working daemon.
- **Schema mechanism (2026-08-12)**: all DDL lives in
  `crates/gcore/assets/schema/baseline.sql` (idempotent, pg_dump-normalized
  style), embedded in gdaemon; `src/gobby/storage/migrations/` and the Python
  baseline no longer exist, and a tripwire test forbids persistent DDL in
  production Python. Data migrations are `EmbeddedMigration` entries in
  `crates/gcore/src/schema/assets.rs` `MIGRATIONS` (re-armed by 1.5), with SQL
  assets under `crates/gcore/assets/schema/migrations/NNN_<name>.sql` and
  versions allocated `> 375` at implementation time. Relative order must hold:
  all domain copies < drop. Every copy migration is guarded (DO-block
  `information_schema` checks) so it is valid whether or not the legacy tables
  exist; every migration added by this epic must be **fresh-redundant** — its
  effect on an empty hub is subsumed by the current baseline (copies no-op,
  the drop targets tables the end-state baseline no longer creates).
- **Schema-artifact lockstep**: every `baseline.sql` edit re-arms, in one
  commit: `BASELINE_CHECKSUM` (`crates/gcore/src/schema/assets.rs`),
  `PREDECESSOR_BASELINE_CHECKSUM` and the `baseline_refresh_statement` prefix
  list (`crates/gcore/src/schema/runner.rs`), the fixture
  `crates/gcore/tests/fixtures/schema/predecessor_baseline.sql`,
  `crates/gcore/assets/schema/catalog.manifest.json` (regenerated via
  `UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --test
  catalog_manifest_freshness` against an isolated database), the pinned
  checksums in `crates/gcore/tests/schema_contract.rs`, and
  `src/gobby/storage/schema_expected_identity.json` (regenerated via
  `uv run python scripts/generate_schema_expected_identity.py --gdaemon
  target/release/gdaemon` after `cargo build --release -p gobby-daemon`).
  Reinstall gdaemon to `~/.gobby/bin/` afterward — a committed change is not
  live until the binary is reinstalled. Recipe precedents: commits
  `4a5b8f9e3` (smallest complete fan-out) and `d7707cacd` (adds tables);
  `.gobby/plans/reactive-config-store.md:182-195` documents the sequence.
  Hubs more than one baseline hop behind must recreate from a verified
  backup; each phase that edits the baseline is one hop.
- **Migration flatten is out of scope** (non-goal): folding this epic's
  numbered migrations back into the baseline is a pre-0.5.0 release chore
  owned outside this epic (operator decision 2026-08-12).
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
- **Every copy migration takes `LOCK TABLE <legacy source> IN ACCESS
  EXCLUSIVE MODE` inside its existence guard, before its first source
  read** (adversary round 4 APR4-008; guard-first ordering, adversary round
  5 APR5-001). The migration's first statement checks the legacy source
  exists (`to_regclass` inside the guarded `DO` block); only the guarded
  branch acquires the lock (`EXECUTE 'LOCK TABLE …'` — a table lock taken
  inside a `DO` block holds to transaction end), then copies, validates,
  and checkpoints under it. The ordering is load-bearing: the runner
  executes every pending non-destructive migration's SQL before stamping
  its receipt (`apply_pending_migrations`, `runner.rs:676-742`; 1.5.3 pins
  fresh-lineage receipted-no-op reapply), and after 7.1 the final baseline
  no longer creates the legacy tables, so an unconditional opening `LOCK
  TABLE` would abort every fresh install on `undefined_table`. With the
  guard first, an absent source records a receipted no-op. On live hubs the
  lock is still required: the
  runner's advisory lock (`pg_advisory_lock(hashtext('postgres_migrations_apply'),
  hashtext(current_schema()))`, `runner.rs:105-113`) serializes migration
  runners only; runtime writers hold disjoint one-arg per-row advisory keys or
  plain transactions (`postgres_pool.py:328-332`, `postgres.py:239-248`), and
  daemon startup applies migrations **before** the predecessor gate severs the
  old daemon's backends (`runner_init/helpers.py:94-135` vs
  `runner_lifecycle.py:166-169`), so a still-live predecessor can write the
  legacy table mid-copy. Under READ COMMITTED each statement takes a fresh
  snapshot, so an unfenced copy, equivalence guard, and ledger hash can each
  see different data — including a ledger hash recording a write the typed
  copy never saw, which would let 7.1's backstop pass over real drift. The
  table lock waits out in-flight writers and freezes the source for the
  migration's whole transaction, so copy, guard, and checkpoint read one
  snapshot.
- **Copy equivalence guard**: inside the guarded branch, after the copy,
  join every legacy source row to its typed target by the conflict-target
  bullet's join rule above (live rows on the live natural key, soft-deleted
  rows on the preserved `id`) and compare **both identity and
  payload** — `target.id = source.id`, then the payload after the migration's
  documented normalization and shape transformation — RAISEing with the
  offending source ids and names on mismatch. `ON CONFLICT DO NOTHING` plus a
  count check silently passes when a pre-existing typed row holds a divergent
  payload — the routine outcome of re-migrating a partially staged dev database
  — and the P7 drop backstop never consults typed rows at all (7.1 verifies
  only that each legacy row still matches its `legacy_copy_ledger` checkpoint),
  so it cannot catch that divergence either. Identity is checked because it is a promise
  this plan makes and targetless `ON CONFLICT DO NOTHING` is exactly what hides
  its violation: a pre-existing live typed row with the same natural key and an
  identical normalized payload but a **different UUID** suppresses the insert
  and then satisfies a payload-only guard, so the copy is reported complete
  while `id` silently changed. Since `agent_step_workflows.agent_definition_id`
  and `agent_step_instances.agent_step_workflow_id` are FKs onto these
  preserved ids, that is a lineage redirect that survives all the way to P7
  reporting equivalence. These identity and payload checks live **only inside
  the domain copy migrations** (adversary round 4 APR4-001): P7's drop
  backstop is a separate, purely directional predicate — every non-generated
  legacy row must hold a `legacy_copy_ledger` entry keyed by its preserved
  `id` whose recorded copy-time hash matches the row's current normalized
  payload, with no typed-row lookup (7.1). Each domain's focused migration test covers six cases:
  first run, rerun over live rows, rerun over soft-deleted rows, two
  soft-deleted rows sharing a natural key, a divergent payload conflict (loud
  failure), and a live same-key/same-payload/**different-UUID** row (loud
  failure).
- 1,000-line rule (counts re-verified 2026-08-12): `workflows/definitions.py`
  (965), `dry_run.py` (992), `spawn_agent/_implementation.py` (972),
  `mcp_proxy/tools/workflows/__init__.py` (826), `cli/agents.py` (861),
  `workflows/hooks.py` (804), `state_manager.py` (790) are near the cap — the
  named extractions in P2/P3/P5 are mandatory, not optional. Each extraction is scheduled in or before the first task that
  grows its file, never as a later cleanup: `definitions.py` splits in 2.1,
  `dry_run.py`'s trace helpers move in 2.4 (eight lines of headroom cannot absorb
  2.4's nested-agent rewrite), and `_implementation.py`'s step-state block moves
  in 3.2. A conditional "extract if it gets close" leaves an over-cap commit in
  between and is not used anywhere in this plan.
- **Companion coverage ledger** (adversary round 2 APR2-006):
  `.gobby/plans/split-workflow-definition-storage.coverage-ledger.yaml`
  enumerates every deliverable acceptance item and its expected implementation
  leaf per the Plan-Coverage Contract's Bootstrap Ledger section
  (`docs/contracts/plan-coverage.md`). It is maintained alongside this plan,
  adversary-reviewed before expansion, and verified against the generated
  manifest by `verify_bootstrap_ledger` at close time.
- Tests: focused runs only with `GOBBY_TEST_PROTECT=1`; never the full suite.
  Rust: `cargo test -p gobby-core <name>`; never bare `cargo test`.
- After each phase lands: rebuild and reinstall gdaemon if crate schema assets
  changed, then `uv run gobby restart` — startup applies the baseline refresh
  and pending non-destructive migrations. The P7 drop is destructive-gated: a
  normal restart refuses it loudly, and the operator applies it once per hub
  with `gobby hub-maintenance run schema-apply` — the epoch-opening
  orchestrator that stops the daemon, quiesces backends, validates the hub
  backup manifest, and runs the epoch-bound destructive apply (7.1; a raw
  `gdaemon schema apply --destructive` refuses without the epoch GUC,
  adversary round 5 APR5-002) — then restarts.

## P1: Storage Foundation
`kind: framing`

**Goal**: New tables exist (empty), typed managers and per-domain cache
revisions are available, nothing consumes them yet.

### 1.1 Domain-table DDL in the revisioned baseline [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: baseline checksum re-arm
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: predecessor checksum and refresh-statement prefix list re-arm
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: refresh-contract fixture and added-statement assertions
- `crates/gcore/src/baseline_refresh.rs::*` — scope-reason: refresh-statement acceptance re-arm for the added baseline statements (adversary round 3 APR3-013)
- `crates/gcore/tests/fixtures/schema/predecessor_baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog rows for the eight new tables
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pinned checksum and root-hash constants
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated release-pinned identity
- `tests/storage/test_domain_tables_schema.py`

Add the eight tables to `crates/gcore/assets/schema/baseline.sql` with
idempotent guards (`CREATE TABLE IF NOT EXISTS` / `CREATE UNIQUE INDEX IF NOT
EXISTS`), KEEPING `workflow_definitions` and `workflow_instances` in the
baseline until P7. Follow the checked-in pg_dump-normalized style — `USING
btree`, parenthesized `WHERE (...)` — because `catalog.manifest.json` stores
round-tripped definitions and the freshness test compares bytes. Grants ride
the grants section on the `workflow_definitions` precedent (`GRANT SELECT,
INSERT, DELETE, UPDATE ... TO gobby_daemon_runtime`);
`crates/gcode/security/managed_postgres_privileges.json` needs no change for
daemon-owned tables. Partial-unique live-name precedent in the baseline:
`idx_build_profiles_active_unique`. Re-arm the full schema-artifact lockstep
from Constraints in the same commit, then rebuild and reinstall gdaemon.

**Refresh-statement acceptance owner (adversary round 3 APR3-013).** The
refresh-hop acceptance for added baseline statements does not live in
`runner.rs`: `runner.rs::baseline_statement_for_state` (`:418-453`) only
delegates the `PredecessorBaseline` branch to
`crates/gcore/src/baseline_refresh.rs::baseline_refresh_statement`
(`baseline_refresh.rs:25-29`), which today accepts exactly one statement by
exact-string equality against a single `const` (`REFRESH_STATEMENT`,
`baseline_refresh.rs:4`). This deliverable extends that module to an
enumerated acceptance of exactly the added statements — the eight CREATE
TABLEs, their indexes, and their grants — so the set-difference tripwire in
`runner_tests.rs` passes on the real inventory rather than a relaxed match.
`runner.rs` keeps its unrelated `GcoreCodeIndex`/`GwikiStandalone` filtering
branches; only the predecessor-refresh acceptance changes, in its actual
implementation module.

```sql
CREATE TABLE IF NOT EXISTS rule_definitions (
    id UUID PRIMARY KEY,
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    name TEXT NOT NULL,
    description TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    enabled_pinned BOOLEAN NOT NULL DEFAULT FALSE,
    priority INTEGER NOT NULL DEFAULT 100,
    sources JSONB,
    definition_json JSONB NOT NULL,
    source TEXT NOT NULL DEFAULT 'installed' CHECK (source IN ('installed','custom','project')),
    tags JSONB,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rule_defs_project ON rule_definitions USING btree (project_id);
CREATE INDEX IF NOT EXISTS idx_rule_defs_event ON rule_definitions USING btree ((definition_json->>'event')) WHERE (deleted_at IS NULL);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_defs_live_name
    ON rule_definitions USING btree (name, project_id) NULLS NOT DISTINCT WHERE (deleted_at IS NULL);
```

`agent_definitions`: same shape minus `priority`/`sources` (body =
`AgentDefinitionBody` JSON **without** step fields). `pipeline_definitions`:
same shape minus `priority`/`sources`, plus `version TEXT NOT NULL DEFAULT
'1.0'` and `canvas_json JSONB`. `session_variable_defaults`: fully typed — no
JSON body: `id, project_id, name, description, enabled,
enabled_pinned, default_value JSONB, source, tags, deleted_at,
created_at, updated_at` (same indexes/partial unique pattern; `default_value`
holds any JSON scalar/object). All four definition tables carry `enabled` +
`enabled_pinned` (Decision Record: the sync guard from `9071a6209` survives
the split; the typed tables rename the legacy `enabled_user_modified` bit to
`enabled_pinned`); `agent_step_workflows` carries neither. Each definition
table gets `idx_<t>_project`, and partial unique `uq_<t>_live_name (name,
project_id) NULLS NOT DISTINCT WHERE (deleted_at IS NULL)`.

```sql
CREATE TABLE IF NOT EXISTS agent_step_workflows (
    id UUID PRIMARY KEY,
    agent_definition_id UUID NOT NULL UNIQUE
        REFERENCES agent_definitions(id) ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    steps_json JSONB NOT NULL,
    variables_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    exit_condition TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_step_instances (
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
CREATE INDEX IF NOT EXISTS idx_asi_step_workflow ON agent_step_instances USING btree (agent_step_workflow_id);

CREATE TABLE IF NOT EXISTS definition_revisions (
    domain TEXT PRIMARY KEY,
    revision BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS legacy_copy_ledger (
    legacy_id UUID PRIMARY KEY,
    domain TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    copied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`definition_revisions` (enhancement E4) is cross-daemon invalidation state:
one row per `DefinitionDomain` (1.4), created lazily by 1.4's UPSERT bump. It
needs no seed rows, no copy migration, and no partial-unique index; it rides
the same grants section as the other daemon-owned tables.

`legacy_copy_ledger` (adversary round 3 APR3-011) is the copy-time checkpoint
that makes P7's drop backstop **directional**: each definition copy migration
(2.3, 4.1, 4.2, 4.3) inserts one row per copied legacy source row — the
preserved legacy UUID, its domain, and the MD5 of that domain's normalized
source payload (`jsonb` text output is deterministic, so the hash is a stable
equality proof) — with `ON CONFLICT (legacy_id) DO NOTHING`, so reruns keep
the first copy-time hash. At P7 the drop backstop compares each surviving
legacy row against its checkpoint hash instead of against the (legitimately
evolving) typed rows. The table exists only to prove the drop safe: 7.1's
destructive migration drops it together with the legacy tables and removes
its DDL from the baseline in the same commit, so fresh lineages never create
it after P7. It rides the same grants section as the other daemon-owned
tables and needs no partial-unique index.

The pre-refresh plan's dual-DDL catalog-equivalence test is obsolete: tables
ride only the baseline now, so there is no second DDL copy to drift.
Fresh-vs-refresh lineage equivalence is enforced by gcore's existing contract
tests — the set-difference tripwire
(`baseline_refresh_accepts_exactly_the_predecessor_statement_difference`)
fails until the refresh prefix list names exactly the added statements, and
the nondestructive-reapply test proves the refresh hop preserves data. New
`tests/storage/test_domain_tables_schema.py` pins the Python-visible surface
against `catalog.manifest.json` (the checked-in catalog of the applied
schema): the eight tables exist, the four definition tables carry
`enabled_pinned`, `agent_step_workflows` carries no enabled columns,
and the four partial unique live-name indexes keep their `WHERE (deleted_at
IS NULL)` predicate and `NULLS NOT DISTINCT` key.

**Acceptance:**

- 1.1.1 - Baseline contains the eight new tables (including legacy_copy_ledger) with partial unique live-name indexes and `enabled_pinned` on the four definition domains, and retains the legacy tables. file: `crates/gcore/assets/schema/baseline.sql`.
- 1.1.2 - The schema-artifact lockstep is re-armed: the refresh-statement acceptance in its implementation module enumerates exactly the added statements and the predecessor fixture matches its pinned checksum. file: `crates/gcore/src/baseline_refresh.rs`. file: `crates/gcore/src/schema/runner.rs`. file: `crates/gcore/src/schema/runner_tests.rs`.
- 1.1.3 - The regenerated catalog manifest carries the eight tables and the freshness test passes without the update flag; the regenerated expected identity matches the rebuilt gdaemon. file: `crates/gcore/assets/schema/catalog.manifest.json`. file: `src/gobby/storage/schema_expected_identity.json`.
- 1.1.4 - The applied-schema catalog pins the eight tables, the reconciliation columns, and the partial unique predicates. test: `tests/storage/test_domain_tables_schema.py`.

### 1.2 Typed definition managers package [category: code] (depends: 1.1, 1.4)
`kind: deliverable`

Targets: `src/gobby/storage/definitions/__init__.py` (new),
`src/gobby/storage/definitions/_shared.py` (new),
`src/gobby/storage/definitions/rules.py` (new),
`src/gobby/storage/definitions/variables.py` (new),
`src/gobby/storage/definitions/pipelines.py` (new),
`tests/storage/definitions/test_rules_manager.py` (new),
`tests/storage/definitions/test_variables_manager.py` (new),
`tests/storage/definitions/test_pipelines_manager.py` (new),
`tests/storage/definitions/test_enabled_reconciliation.py` (new)

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
pre-check), and `update_from_sync(**fields)`. The reconciliation semantics
port from `storage/workflow_definitions.py` (`9071a6209`) with the bit
renamed `enabled_pinned`, as a three-clause contract shared by every
definition manager: (a) `update` and `toggle_enabled` stamp `enabled_pinned =
TRUE` whenever they change `enabled`; (b) `update_from_sync` restricts writes
to the sync allow-list, propagates a changed template `enabled` default only
while `enabled_pinned` is FALSE, and never sets the flag; (c) once pinned, a
user's value survives sync even when it equals the template default. New
`tests/storage/definitions/test_enabled_reconciliation.py` proves the
contract parameterized over `RuleDefinitionManager`,
`SessionVariableDefaultManager`, and `PipelineDefinitionManager`
(`AgentDefinitionManager` joins the same parameterization in 1.3). The
bundled-sync tasks (2.3, 4.1, 4.2, 4.3) route template refreshes through
`update_from_sync` so user toggles survive template drift on every domain. Domain additions: `RuleDefinitionManager.list_by_event(event,
project_id, enabled)` and `list_by_group(group, ...)` using native
`definition_json->>'event'`, `ORDER BY priority, name` (ports of
`storage/workflow_definitions.py:380-438`);
`SessionVariableDefaultManager.get_defaults_map(project_id=None,
enabled_only=True) -> dict[str, Any]` reading typed `name`/`default_value`
columns (no `source` filter — see 4.2); no `duplicate` for variables.
`PipelineDefinitionManager.update` additionally allows `version`,
`canvas_json`. Every mutator follows 1.4's commit-visible revision contract:
it advances the persistent revision inside its transaction and registers the
process-local bump on the transaction's `after_commit` seam (Epic Review
Notes correction 17), which the ambient layer fires only after the
**outermost** transaction commits — so a mutation nested inside a caller's
ambient transaction bumps nothing until that outer transaction truly commits,
and a rollback at any nesting depth publishes no revision and fires no
listener.

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
- 1.2.8 - The parameterized reconciliation contract holds for rules, variables, and pipelines: user update/toggle stamps enabled_pinned, sync adopts a changed template enabled default while unpinned, and sync preserves the user's value while pinned. test: `tests/storage/definitions/test_enabled_reconciliation.py`.
- 1.2.9 - Manager mutators bump post-commit only: a mutator that raises mid-transaction leaves the persistent revision, the local counter, and the listeners untouched — including when the mutator runs nested inside a caller's ambient transaction that later rolls back. test: `tests/storage/definitions/test_rules_manager.py`.

### 1.3 Agent definition manager with step-workflow child [category: code] (depends: 1.2)
`kind: deliverable`

Targets: `src/gobby/storage/definitions/agents.py` (new),
`tests/storage/definitions/test_agents_manager.py` (new),
`tests/storage/definitions/test_enabled_reconciliation.py` (new)

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
  child).
- `set_step_workflow(agent_definition_id, step_workflow: dict | None)`
  primitive for surfaces that already hold the parent; one `db.transaction()`.
- `get_step_workflow(agent_definition_id) -> AgentStepWorkflowRow | None`.

**Soft delete preserves the child (adversary round 3 APR3-001)**: the parent
body no longer carries step fields, so the child row is the **only**
restorable step-workflow payload. `AgentDefinitionManager.delete` (soft
delete) therefore only hides the parent (`deleted_at`); the child row is
untouched, and `restore` returns the parent with its step workflow intact.
The child is removed through exactly four paths: `set_step_workflow(None)`,
`upsert_with_steps` without steps, parent `hard_delete` (FK cascade), and the
scheduled soft-deleted-definition purge (7.1.5, also by cascade). A
delete→restore round trip preserves the child payload byte-for-byte;
`tests/storage/definitions/test_agents_manager.py` pins that round trip and
its revision behavior (soft delete and restore bump `agents`; the untouched
child bumps nothing by itself).

**Dual-domain bump contract (enhancement E1)**: every write that creates,
updates, or deletes a child row — `upsert_with_steps`, `set_step_workflow`,
and child deletion via parent hard-delete or purge cascade — bumps **both**
`agent_step_workflows` and `agents` under 1.4's commit-visible contract,
because hydrated agent bodies embed the child and 2.4's engine cache keys on
the `agents` revision alone. A child-only edit must therefore invalidate a
cached hydrated parent. `AgentDefinitionManager` also joins the parameterized
`enabled_pinned` reconciliation contract from 1.2 in
`tests/storage/definitions/test_enabled_reconciliation.py`.

**Acceptance:**

- 1.3.1 - Reads hydrate the nested step_workflow from the child table in one query. symbol: `AgentDefinitionManager`. file: `src/gobby/storage/definitions/agents.py`.
- 1.3.2 - `upsert_with_steps` writes parent and child atomically, deleting the child when steps are removed. test: `tests/storage/definitions/test_agents_manager.py::test_upsert_with_steps_atomic`.
- 1.3.3 - Child cascade on parent hard-delete and orphan-free child lifecycle are covered. test: `tests/storage/definitions/test_agents_manager.py`.
- 1.3.6 - Soft delete leaves the child row in place and a delete→restore round trip returns the parent with its step workflow payload intact. test: `tests/storage/definitions/test_agents_manager.py::test_soft_delete_restore_preserves_child`.
- 1.3.4 - A child-only create, update, or delete bumps both the agent_step_workflows and agents revisions after commit; a rolled-back child write bumps neither. test: `tests/storage/definitions/test_agents_manager.py`.
- 1.3.5 - AgentDefinitionManager satisfies the parameterized enabled_pinned reconciliation contract. test: `tests/storage/definitions/test_enabled_reconciliation.py`.

### 1.4 Domain cache revisions with cross-daemon invalidation [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/definitions/revisions.py` (new)
- `src/gobby/storage/definitions/notifications.py` (new)
- `src/gobby/runner_init/storage.py::*` — scope-reason: definition-revision listener construction during storage init
- `src/gobby/runner_init/services.py::*` — scope-reason: listener start in the async stateful-services phase (adversary round 3 APR3-014)
- `src/gobby/runner_rollback.py::*` — scope-reason: listener rollback branch for construction failures (adversary round 3 APR3-014)
- `src/gobby/runner_lifecycle_shutdown.py::*` — scope-reason: definition-revision listener stop in the graceful-shutdown tail
- `tests/test_runner_init.py::*` — scope-reason: init-rollback coverage for the listener branch
- `tests/storage/definitions/test_revisions.py` (new)
- `tests/integration/definitions/test_definition_revisions_multi_daemon.py` (new)

```python
DefinitionDomain = Literal["rules", "agents", "agent_step_workflows", "variables", "pipelines"]
def get_definitions_revision(domain: DefinitionDomain) -> int: ...
def bump_definitions_revision(*domains: DefinitionDomain) -> None: ...   # registered via Transaction.after_commit: local counter + listeners
def advance_persistent_revision(conn, *domains: DefinitionDomain) -> None: ...  # in-transaction: UPSERT + pg_notify
def register_revision_listener(domain: DefinitionDomain, cb: Callable[[], None]) -> None: ...
```

Thread-safe per-domain process-local counters (same semantics as today's
`_WORKFLOW_DEFINITIONS_REVISION`) plus persistent cross-daemon state
(enhancement E4 — two daemons share one PostgreSQL hub, and a process-local
counter in daemon A cannot invalidate daemon B). This lands before 1.2 so the
typed managers can call both halves in the same commit that introduces them.

**Commit-visible revision contract (enhancement E1)**, binding on every
manager mutator in 1.2/1.3:

- Inside its transaction the mutator calls `advance_persistent_revision`:
  `INSERT INTO definition_revisions (domain, revision) VALUES (%s, 1) ON
  CONFLICT (domain) DO UPDATE SET revision = definition_revisions.revision +
  1, updated_at = NOW()` plus `SELECT pg_notify('gobby_definition_revisions',
  '<domain>:<new revision>')`. PostgreSQL delivers NOTIFY only on commit, so a
  rolled-back mutation publishes nothing.
- Inside the same transaction the mutator registers
  `bump_definitions_revision` through the transaction's `after_commit` seam
  (`Transaction.after_commit`, `postgres_pool.py:304` — Epic Review Notes
  correction 17). The ambient layer fires registered callbacks only after the
  **outermost** pooled transaction commits, and a rollback at any depth
  discards them, so the local counter and listeners advance if and only if
  the persistent revision and the definition write themselves became durable.
  "After the manager's transaction block exits" is NOT the contract: under
  ambient nesting that point precedes the true commit, and a later outer
  rollback would strand an advanced local counter and fired listeners against
  a rolled-back write. Listeners never fire inside a transaction and never
  for a rolled-back one.

`notifications.py`: `DefinitionRevisionListener`, modeled on
`src/gobby/storage/config_notifications.py` (pool-exempt LISTEN connection,
poll-healing loop). On NOTIFY, if the payload revision exceeds the last
observed persistent revision for that domain, it records it and calls
`bump_definitions_revision(domain)`. The poll-healing pass SELECTs
`definition_revisions` on the same interval pattern as config notifications
and bumps any domain whose persistent revision advanced past the observed
one, healing dropped notifications and connection gaps. Startup seeds the
observed map from the current table so a fresh daemon fires no spurious
listeners. Self-notifications are not deduplicated — the writer daemon's own
NOTIFY causes one extra local bump, a harmless extra cache reload.

**Listener lifecycle ownership (adversary BR-006)**. The config precedent is
ownership, and construction alone: `ConfigNotificationListener` is built in
`runner_init/storage.py` and handed to `ConfigRuntime`, which starts its
listen/poll tasks and is closed by the shutdown tail
(`runner_lifecycle_shutdown.py:788-790` via `_best_effort`).
`DefinitionRevisionListener` gets the same complete shape as a runner-owned
service: `start()` spawns the LISTEN task and the poll-healing task;
`close()` cancels both and closes the pool-exempt connection.
**Construction and start are separate phases (adversary round 3 APR3-014)**:
`init_storage_and_config` (`runner_init/storage.py:99`, a plain `def`) runs
during synchronous `GobbyRunner` construction — including the direct
synchronous constructor the runner-init tests use — so it may only
**construct** the listener and hang it on runner state; `start()` spawns
asyncio tasks and is called from the async stateful-services phase
(`runner_init/services.py::init_stateful_services`, reached via
`GobbyRunner.create`), exactly the config-runtime split the precedent uses.
`rollback_runner_resources` (`runner_rollback.py:40-93`) gains a listener
branch beside its config-runtime branch, so a later construction failure
cancels any started tasks and closes the pool-exempt connection; the
graceful-shutdown tail calls `close()` through `_best_effort` beside the
config-runtime close, so a failed
listener stop never blocks the rest of shutdown. A listen-task exception is
logged, the connection is dropped and re-established with backoff, and
poll-healing covers the gap — the mutating writer is never affected because
listener callbacks already run outside the write path. The connection factory
is injectable (the `ConnectionFactory` protocol pattern from
`config_notifications.py`), which is the unit-test seam: NOTIFY delivery,
crash-reconnect, and shutdown cancellation are all driven through a fake
factory without a live LISTEN connection.

The listener registry replaces the current storage→hooks import cycle
(`storage/workflow_definitions.py:49-56` importing
`clear_active_rule_names_cache`). Reader wiring lands with each consumer phase
(engine agent cache in 2.4, active-rule-names listener in 4.1, variables TTL
invalidation in 4.2, pipeline loader in 4.3). Listener exceptions are caught
and logged, never propagated to the mutating caller. The two-daemon suite
follows the `two_daemons` cluster pattern from
`tests/integration/config/conftest.py`.

**Acceptance:**

- 1.4.1 - Per-domain counters, the persistent advance/notify half, and the listener registry exist with thread-safety. file: `src/gobby/storage/definitions/revisions.py`.
- 1.4.2 - Bumping one domain fires only that domain's listeners and leaves other domains' revisions unchanged. test: `tests/storage/definitions/test_revisions.py`.
- 1.4.3 - A committed advance_persistent_revision advances definition_revisions and delivers exactly one notification; a rolled-back transaction leaves the table unchanged and delivers none. test: `tests/storage/definitions/test_revisions.py`.
- 1.4.4 - The listener service maps observed persistent revisions into local bumps, and poll-healing recovers a missed notification. file: `src/gobby/storage/definitions/notifications.py`. test: `tests/storage/definitions/test_revisions.py`.
- 1.4.5 - A definition mutation through daemon A is observed by daemon B without a restart, in an isolated two-daemon cluster. test: `tests/integration/definitions/test_definition_revisions_multi_daemon.py`.
- 1.4.6 - The listener service has a complete lifecycle: synchronous storage init only constructs it, the async stateful-services phase starts it, the graceful-shutdown tail closes it cancelling both tasks and closing the LISTEN connection, and a listen-task crash reconnects with poll-healing covering the gap — proven through the injectable connection-factory fake. file: `src/gobby/runner_init/services.py`. file: `src/gobby/runner_lifecycle_shutdown.py`. test: `tests/storage/definitions/test_revisions.py`.
- 1.4.8 - Direct synchronous GobbyRunner construction succeeds with no event loop, and a construction failure after listener creation rolls the listener back with the other runner resources. file: `src/gobby/runner_rollback.py`. test: `tests/test_runner_init.py`.
- 1.4.7 - A mutation nested inside an outer ambient transaction bumps and notifies exactly once when the outer transaction commits, and neither bumps, notifies, nor fires listeners when the outer transaction rolls back. test: `tests/storage/definitions/test_revisions.py::test_ambient_nested_commit_visibility`.

### 1.5 Re-arm the embedded migration machinery [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: MIGRATIONS include_str wiring and checksum constants
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: thread the classified lineage into pending-migration application
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: fresh-stamp, destructive-refusal, and receipt coverage
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: source_inventory refresh if runner call counts change

`MIGRATIONS` is deliberately empty pre-0.5.0 ("numbered migrations resume
after the release boundary", `assets.rs:19-21`); the recorded policy names
destructive or transforming DDL as the trigger for re-arming, and this epic is
that trigger (Decision Record 2026-08-12). Re-arm without changing the
checksum/receipt protocol:

- Create `crates/gcore/assets/schema/migrations/` and the entry convention:
  `EmbeddedMigration { version, filename, checksum, sql:
  include_str!("../../assets/schema/migrations/NNN_<name>.sql") }` with
  versions allocated `> 375` in list order and sha256 checksums pinned in
  `assets.rs`. `root_hash()` already folds MIGRATIONS entries; the pinned
  root hashes in `crates/gcore/tests/schema_contract.rs` and the expected
  identity re-arm with each added entry via the Constraints lockstep.
- **Fresh lineages receipt-stamp destructive migrations instead of refusing.**
  `apply_pending_migrations` (`runner.rs:676-742`) refuses any destructive
  entry without authorization on every lineage, which would break fresh
  installs once P7's drop lands: a fresh hub has no legacy tables to drop —
  the end-state baseline never creates them — yet the static directive check
  aborts the apply. Thread the classified baseline state into
  `apply_pending_migrations`; on a fresh lineage, write the receipt through
  the existing `insert_receipt` path without executing the destructive entry.
  Sound because of the Constraints fresh-redundancy invariant: every
  migration this epic adds has no effect on an empty hub. Non-destructive
  entries still execute on every lineage (the copy migrations are
  DO-block-guarded no-ops on fresh).
- Existing-hub semantics unchanged: pending non-destructive migrations apply
  on daemon restart; destructive ones require `--destructive` plus a verified
  backup manifest matching the live DB, and destructive-plus-non-transactional
  stays rejected.
- Update `managed_postgres_privileges.json`'s `source_inventory` only if the
  runner's postgres call counts change — the exact-equality privilege test
  recomputes the inventory over all production Rust.

**Acceptance:**

- 1.5.1 - The migrations asset directory and include_str wiring exist with versions above the baseline and pinned checksums. file: `crates/gcore/src/schema/assets.rs`.
- 1.5.2 - A destructive migration on a fresh lineage is receipt-stamped without executing, and still refuses without authorization on an existing lineage. file: `crates/gcore/src/schema/runner_tests.rs`.
- 1.5.3 - A guarded non-destructive migration applies on fresh and predecessor lineages and re-applies as a receipted no-op. file: `crates/gcore/src/schema/runner_tests.rs`.

## P2: Agent Definition Shape
`kind: framing`

**Goal**: `AgentDefinitionBody.step_workflow` replaces the top-level step
fields end-to-end; agent storage reads/writes the typed tables; generated-row
scaffolding remains only for the P3 runtime readers.

### 2.1 Model split and nested step_workflow model [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/workflows/agent_models.py` (new)
- `src/gobby/workflows/pipeline_models.py` (new)
- `src/gobby/workflows/definitions.py::*` — scope-reason: model split with permanent re-exports
- `tests/workflows/test_agent_models.py` (new)

This deliverable is **additive only** (adversary round 2 APR2-002): it splits
the module and introduces the nested model while the legacy top-level step
fields remain in place and populated, so every intermediate reader, the
bundled flat YAML, and sync all keep working. The breaking half — field
deletion, the rejecting validator, the reader rewrite, and the YAML
conversion — is one atomic cutover owned entirely by 2.2, because a model
that has dropped its step fields cannot load the checked-in flat YAML:
with the validator the bundled sync fails loudly on all 21 stepful agents,
and without it `extra="ignore"` silently discards their steps. Either way an
intermediate commit that changes the model ahead of the YAML is not a working
daemon.

`workflows/definitions.py` is 965 lines with 20 top-level models — split
before growing. Move `AgentSelector`, `AgentWorkflows`, `AgentDefinitionBody`
(`definitions.py:441-573`) to `agent_models.py`; move `WebhookEndpoint`,
`WebhookConfig`, `PipelineApproval`, `MCPStepConfig`, `PipelineStep`,
`PipelineDefinition` (`definitions.py:700-915`) to `pipeline_models.py`.
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

`AgentDefinitionBody`: add
`step_workflow: AgentStepWorkflowBody | None = None` **alongside** the legacy
top-level `steps`, `step_variables`, and `exit_condition` fields, which remain
present and populated in this deliverable. Deleting them, and the
`mode="before"` validator that rejects them, land atomically with the YAML
conversion in 2.2.

**Acceptance:**

- 2.1.1 - AgentStepWorkflowBody exists and AgentDefinitionBody carries the optional nested step_workflow field alongside the still-present legacy fields. symbol: `AgentStepWorkflowBody`. file: `src/gobby/workflows/agent_models.py`.
- 2.1.2 - Pipeline models live in their own module with definitions.py re-exports intact. file: `src/gobby/workflows/pipeline_models.py`.
- 2.1.3 - definitions.py is under 1,000 lines after the split. file: `src/gobby/workflows/definitions.py`.
- 2.1.5 - Model validation round-trips nested YAML (stepful and step-less). test: `tests/workflows/test_agent_models.py::test_step_workflow_nesting`.

### 2.2 Atomic model-and-YAML step_workflow cutover [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/workflows/agent_models.py`
- `src/gobby/agents/step_workflow.py::*` — scope-reason: scaffolded nested-shape read
- `src/gobby/agents/sync.py::*` — scope-reason: nested-shape read in sync validation
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: step_workflow reader rewrite
- `src/gobby/cli/agents.py::*` — scope-reason: step_workflow reader rewrite
- `src/gobby/dispatch/skill_composition.py::*` — scope-reason: step_workflow reader rewrite
- `src/gobby/dispatch/spawn.py::*` — scope-reason: step_workflow reader rewrite
- `src/gobby/mcp_proxy/tools/apply_persona.py::*` — scope-reason: step_workflow reader rewrite
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: step_workflow reader rewrite
- `src/gobby/workflows/dry_run.py::*` — scope-reason: agent-branch step_workflow rewrite
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated hashes for the rewritten agent YAMLs
- `tests/agents/test_discovery_agents.py::*` — scope-reason: nested-shape assertions
- `tests/agents/test_doc_reviewer_definition.py::*` — scope-reason: nested-shape retarget of raw-key assertions
- `tests/agents/test_epic_reviewer_definition.py::*` — scope-reason: nested-shape retarget of raw-key assertions
- `tests/agents/test_qa_reviewer_definition.py::*` — scope-reason: nested-shape retarget of raw-key assertions
- `tests/agents/test_tech_writer_definition.py::*` — scope-reason: nested-shape retarget of raw-key assertions
- `tests/agents/test_merge_lifecycle.py::*` — scope-reason: nested-shape retarget of raw-key assertions
- `tests/agents/test_merge_orchestrator_contract.py::*` — scope-reason: nested-shape retarget of raw-key assertions
- `tests/agents/test_sync.py::*` — scope-reason: raw step fixture retarget
- `tests/agents/test_lifecycle_monitor.py::*` — scope-reason: raw step fixture retarget
- `tests/agents/test_lifecycle_monitor_extra.py::*` — scope-reason: raw step fixture retarget
- `tests/agents/test_plan_adversary_taskless_definition.py::*` — scope-reason: raw steps-key reader retarget
- `tests/dispatch/test_skill_composition.py::*` — scope-reason: direct constructor passing a removed field
- `tests/mcp_proxy/tools/test_apply_persona.py::*` — scope-reason: direct constructor passing a removed field
- `tests/workflows/test_agent_definitions_v2.py::*` — scope-reason: field-inventory assertions moved to the nested body
- `tests/workflows/test_agent_models.py`
- `src/gobby/install/shared/workflows/agents/analyst.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/architect.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/backend-developer.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/doc-reviewer.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/epic-reviewer.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/expansion-qa.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/frontend-developer.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/fullstack-developer.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/merge-worker.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/plan-enhancer.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/plan-enhancer-taskless.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/planner.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/product-manager.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/qa-dev.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/researcher.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/tech-writer.yaml::*` — scope-reason: nested step_workflow rewrite
- `src/gobby/install/shared/workflows/agents/trajectory-monitor.yaml::*` — scope-reason: nested step_workflow rewrite
- `tests/dispatch/test_bundled_agent_contract.py::*` — scope-reason: nested-key retarget of the per-file walker

One atomic cutover (adversary round 2 APR2-002): the model's legacy fields,
the bundled YAML, every direct reader, and every shape-asserting suite change
in the same commit, because each half is broken without the other.

**Model half.** In `agent_models.py`, delete the top-level `steps`,
`step_variables`, and `exit_condition` fields from `AgentDefinitionBody`.
Keep `extra="ignore"` for unrelated stale metadata, and add a `mode="before"`
model validator that **rejects** exactly those three removed top-level keys
with an actionable message naming `step_workflow.steps`,
`step_workflow.variables`, and `step_workflow.exit_condition`. Without it
`extra="ignore"` discards a hand-authored or imported step workflow and still
reports success — a silent data loss the audited bundled YAML does nothing to
prevent. The validator lives on the model, so every HTTP, MCP, sync, and
import consumer inherits the fail-loud behavior with no per-surface check and
no compatibility layer.

**YAML half.** Nest top-level `steps:` → `step_workflow.steps:`,
`step_variables:` → `step_workflow.variables:`, `exit_condition:` →
`step_workflow.exit_condition:` in the **21** stepful agents (Epic Review
Notes correction 6): analyst, architect, backend-developer, doc-reviewer,
epic-reviewer, expansion-qa, frontend-developer, fullstack-developer,
merge-orchestrator, merge-worker, plan-adversary, plan-adversary-taskless,
plan-enhancer, plan-enhancer-taskless, planner, product-manager, qa-dev,
qa-reviewer, researcher, tech-writer, trajectory-monitor. Verify the 4
step-less agents (comms-agent, default, goal-taskmaster, triage-agent) carry no
stray legacy keys — with `extra="ignore"` a stray key would be silently
dropped. Bump each rewritten file's `version` so template drift detection
re-syncs. Regenerate `src/gobby/install/bundled_content_manifest.json` in the
same commit (adversary round 2 APR2-003): the manifest hashes every bundled
file, so the YAML rewrite otherwise leaves the checked-in index stale and the
freshness test red. This deliverable's validation includes loading all 25
bundled definitions through `AgentDefinitionBody` — the atomic cutover is
proven by the checked-in artifacts parsing under the new model.

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

Suites that parse these YAML files directly rather than through the model —
`tests/dispatch/test_bundled_agent_contract.py`, which walks
`data.get("steps")` per file, and the per-agent contract tests in the test
inventory above — are retargeted in the same commit as the rewrite. A
YAML-shape change and the assertions about that shape cannot land in different
commits without leaving the suite red in between.

**Acceptance:**

- 2.2.1 - All 21 stepful agent YAMLs use the nested step_workflow shape and none of the 25 bundled files carries top-level steps/step_variables/exit_condition. file: `src/gobby/install/shared/workflows/agents/planner.yaml`.
- 2.2.2 - Every rewritten YAML still validates through AgentDefinitionBody with a populated step_workflow, and all 25 bundled definitions load under the new model. test: `tests/agents/test_sync.py::test_bundled_agents_nested_step_workflow`.
- 2.2.3 - The bundled-agent contract suite reads steps from the nested key and passes against the rewritten YAML in the same commit. test: `tests/dispatch/test_bundled_agent_contract.py`.
- 2.2.4 - AgentDefinitionBody carries no top-level step fields, and validating a body with top-level steps, step_variables, or exit_condition raises with a message naming the nested replacement key. symbol: `AgentDefinitionBody`. file: `src/gobby/workflows/agent_models.py`. test: `tests/workflows/test_agent_models.py::test_legacy_step_keys_rejected`.
- 2.2.5 - Every direct-access site reads through step_workflow and handles the step-less case, across the CLI, dispatch, persona, and spawn readers. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`. file: `src/gobby/cli/agents.py`. file: `src/gobby/dispatch/spawn.py`.
- 2.2.6 - Agent-shape tests and mocks assert the nested shape with no residual top-level step fields, including direct AgentDefinitionBody constructors that passed removed fields and the field-inventory assertions, which move to the nested body rather than being deleted. test: `tests/agents/test_discovery_agents.py`. test: `tests/dispatch/test_skill_composition.py`. test: `tests/workflows/test_agent_definitions_v2.py`.
- 2.2.7 - The bundled-definition contract suites and raw step fixtures read the nested shape and none asserts a top-level steps or step_variables key. test: `tests/agents/test_qa_reviewer_definition.py`. test: `tests/agents/test_merge_orchestrator_contract.py`. test: `tests/agents/test_sync.py`. test: `tests/agents/test_plan_adversary_taskless_definition.py`.
- 2.2.8 - The bundled content manifest is regenerated in the same commit and its freshness test passes without an update flag. file: `src/gobby/install/bundled_content_manifest.json`. test: `tests/install/test_bundled_content_manifest.py`.
- 2.2.9 - Scaffolded register_agent_step_workflow reads the nested shape. symbol: `register_agent_step_workflow`. file: `src/gobby/agents/step_workflow.py`.

### 2.3 Agent sync, write surfaces, and agent copy migration [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/agents/sync.py::*` — scope-reason: sync cutover to the typed manager
- `src/gobby/mcp_proxy/tools/workflows/_agents.py::*` — scope-reason: agent MCP CRUD cutover
- `src/gobby/servers/routes/agents.py::*` — scope-reason: agent HTTP route cutover
- `src/gobby/workflows/template_hashes.py::*` — scope-reason: nested-body hashing
- `crates/gcore/assets/schema/migrations/NNN_copy_agent_definitions.sql` (new)
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: MIGRATIONS entry registration per 1.5
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pinned root hashes re-armed for the added MIGRATIONS entry
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated release-pinned identity for the added MIGRATIONS entry
- `src/gobby/servers/routes/workflows.py::*` — scope-reason: agent-kind rejection on the generic router
- `src/gobby/mcp_proxy/tools/workflows/_definitions.py::*` — scope-reason: agent-kind rejection on the generic MCP tools
- `src/gobby/workflows/imports.py::*` — scope-reason: agent-kind import rejection until 4.3's typed dispatch
- `tests/servers/routes/test_workflows.py::*` — scope-reason: agent-kind rejection cases
- `tests/mcp_proxy/tools/test_workflow_crud.py::*` — scope-reason: agent-kind rejection cases
- `tests/workflows/test_imports.py::*` — scope-reason: agent-kind import rejection fixture (adversary round 3 APR3-003; the suite today exercises only `type: step` and stubs the domain syncs, so the rejection case is added, not retargeted)
- `tests/agents/test_sync.py::*` — scope-reason: typed-manager retarget of the agent sync suite
- `tests/servers/routes/test_agents_routes.py::*` — scope-reason: typed-manager retarget of the agent routes suite
- `tests/mcp_proxy/tools/test_agent_definitions.py::*` — scope-reason: typed-manager retarget of the agent MCP CRUD suite
- `tests/storage/test_agent_copy_migration.py` (new)

- `sync_bundled_agents` (`agents/sync.py:95-274`): validate YAML →
  `AgentDefinitionManager.upsert_with_steps` (parent body stripped of step
  fields, child from `step_workflow`). Template refreshes keep the
  `_build_agent_update_fields` guard semantics (legacy `enabled_user_modified`,
  now `enabled_pinned`) through the manager's `update_from_sync` path (1.2).
  KEEP the
  `_refresh_step_workflow` call (generated legacy row) as P3 scaffolding.
  Orphan cleanup moves to the typed table.
- MCP `update_agent_steps` (`_agents.py:375-419`) → rename
  `update_agent_step_workflow(name, step_workflow: dict | None)`: validate
  `AgentStepWorkflowBody`, call `set_step_workflow`; all `_agents.py` CRUD and
  `_agent_detail` (flat keys → one nested `step_workflow` key) move to
  `AgentDefinitionManager`.
- HTTP agent routes (`routes/agents.py`): list/get/create/PUT/delete/restore/
  import move to the typed manager; the PUT merge (`:446-454`) accepts the
  `step_workflow` key wholesale (validated) instead of three flat keys.
- `template_hashes.py::_load_agents` hashes the nested body shape.
- Copy migration (an `EmbeddedMigration` SQL asset per 1.5, guarded by an
  `information_schema` check for `workflow_definitions`; skips if absent —
  which also makes it a fresh-lineage no-op): validate no duplicate live
  `(name, project_id)` among `workflow_type='agent'` rows.
  **The migration normalizes two source shapes (adversary round 3 APR3-007,
  fixer-induced by the APR2-002 atomic cutover)**: rows written before 2.2
  carry flat top-level `steps`/`step_variables`/`exit_condition`, while rows
  written by 2.2's cutover commit — bundled sync and MCP/HTTP writes still
  landing in `workflow_definitions` until this deliverable's cutover — nest
  them under `step_workflow`. INSERT parents with the body stripped of
  **either** representation (`- 'steps' - 'step_variables' -
  'exit_condition' - 'step_workflow'`, source normalized, UUID/timestamps
  preserved) with targetless `ON CONFLICT DO NOTHING` per the Constraints
  conflict-target rule (tolerates rows sync already created and reruns over
  soft-deleted rows); INSERT children from the nested
  `definition_json->'step_workflow'->'steps'` when
  `jsonb_typeof(...) = 'array' AND jsonb_array_length(...) > 0`, otherwise
  from the flat `definition_json->'steps'` under the same
  `jsonb_typeof`/length guard — the four step-less rows store `"steps": null`,
  and `jsonb_array_length` on a JSON scalar aborts the migration with
  `cannot get array length of a scalar`, so the type guard is required on
  both branches, not defensive — including soft-deleted parents so restore
  keeps steps; validate counts and run the Constraints equivalence guard
  across both parent bodies and child step rows **per source shape**; RAISE
  on mismatch. `tests/storage/test_agent_copy_migration.py` seeds all three
  populations — pre-2.2 flat rows, a 2.2-shape row produced by running the
  cutover sync, and a mixed set — and proves each parent is stripped and each
  child extracted equivalently. Generated
  `workflow_type='workflow'` rows are NOT copied — "generated" is the exact
  signature `name ~ '-steps$' AND source = 'agent'`, and 7.1's drop preflight
  RAISEs on any `workflow_type='workflow'` row that does not match it
  (adversary round 2 APR2-005), so exclusion here cannot silently strand an
  unsupported standalone workflow row. Inside the existence guard — before
the first source read — the migration takes `LOCK TABLE
workflow_definitions IN ACCESS EXCLUSIVE MODE` per the Constraints
copy-fence rule (adversary round 4 APR4-008; guard-first ordering round 5
APR5-001), so on a live hub the copy, the equivalence guard, and the
ledger hashes read one frozen snapshot with no concurrent agent-row write
torn between them, while a post-7.1 fresh lineage with no
`workflow_definitions` records a receipted no-op instead of failing on the
lock.
The migration also writes one `legacy_copy_ledger` row per copied
  source row — preserved legacy id, domain `'agents'`, MD5 of the normalized
  source payload — with `ON CONFLICT (legacy_id) DO NOTHING`, the copy-time
  checkpoint 7.1's directional drop backstop verifies against (adversary
  round 3 APR3-011). Registering the `EmbeddedMigration`
  entry changes `root_hash()` (1.5), so the same commit re-arms the pinned
  root hashes in `crates/gcore/tests/schema_contract.rs` and regenerates
  `src/gobby/storage/schema_expected_identity.json` against the rebuilt,
  reinstalled gdaemon per the Constraints lockstep (adversary round 2
  APR2-008).

- **Generic-surface shrink for kind `agent` (enhancement E3, same commit)**:
  after this cutover the legacy tables must gain no agent rows the typed
  tables never see. The generic router (`routes/workflows.py`) omits
  `workflow_type='agent'` rows from list/get and rejects
  create/update/delete/toggle/import/restore for that kind with an error
  naming `/api/agents`; the generic MCP tools (`_definitions.py`) reject kind
  `agent` naming the agent domain tools; `imports.py::sync_imported_definition`
  refuses agent definitions naming the agent import path (typed per-kind
  dispatch arrives in 4.3). Accepted dev-window caveat: agents disappear from
  generic listings until the web UI retargets in 6.2 — the domain surfaces cut
  over in this commit are the read path.

Known dev-window caveat (accepted): internal agent reads still hit legacy
until 2.4 lands; the P7 drop migration backstop catches any legacy-only
stragglers.

**Acceptance:**

- 2.3.1 - Agent sync upserts parent and child in one transaction and no longer manages step data in the parent body. symbol: `sync_bundled_agents`. file: `src/gobby/agents/sync.py`.
- 2.3.2 - MCP agent CRUD operates on the typed manager with a nested step_workflow surface. symbol: `update_agent_step_workflow`. file: `src/gobby/mcp_proxy/tools/workflows/_agents.py`.
- 2.3.3 - HTTP agent definition routes read and write the typed tables. file: `src/gobby/servers/routes/agents.py`.
- 2.3.4 - Copy migration migrates every agent row and one child per row carrying a non-empty steps array (29 and 25 at planning time), preserves soft-deleted rows, skips the four `"steps": null` rows without a scalar-length error, and fails loudly on count mismatch. test: `tests/storage/test_agent_copy_migration.py`.
- 2.3.5 - Sync produces child workflows for all 21 stepful bundled agents, none for the 4 step-less, and leaves no stale child rows (filesystem-derived counts; the 29-row/25-child hub-derived counts belong to the copy migration in 2.3.4 and E1). test: `tests/agents/test_sync.py`.
- 2.3.6 - The equivalence guard succeeds idempotently on an identical pre-existing typed row and fails loudly on a divergent one. test: `tests/storage/test_agent_copy_migration.py`.
- 2.3.9 - Rerunning the copy over an already-migrated soft-deleted agent row completes without a primary-key abort, and two soft-deleted rows sharing a natural key each match their own target by preserved id. test: `tests/storage/test_agent_copy_migration.py::test_rerun_over_soft_deleted_rows`.
- 2.3.7 - A public agent write carrying legacy top-level step keys is rejected instead of silently dropping the step workflow. test: `tests/servers/routes/test_agents_routes.py`.
- 2.3.8 - Template hashing reads the nested body shape, so a step_workflow edit registers as drift. symbol: `TemplateHashCache._load_agents`. file: `src/gobby/workflows/template_hashes.py`.
- 2.3.10 - No generic surface can create or mutate a legacy agent row post-cutover: the generic HTTP routes, generic MCP tools, and the import path each reject kind `agent` naming the surviving domain surface. test: `tests/servers/routes/test_workflows.py`. test: `tests/mcp_proxy/tools/test_workflow_crud.py`. test: `tests/workflows/test_imports.py`.
- 2.3.11 - Bundled agent sync reaches the typed table through update_from_sync: a changed template enabled default is adopted on an untouched row and preserved on a pinned row. test: `tests/agents/test_sync.py`.
- 2.3.12 - The pinned schema root hashes and the release-pinned expected identity match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`. file: `src/gobby/storage/schema_expected_identity.json`.
- 2.3.13 - The copy migration strips and extracts both the flat pre-2.2 shape and the nested 2.2 shape equivalently: a row synced by the 2.2 cutover then migrated yields a stripped parent and a correct child, and mixed-shape populations migrate without child loss or child data retained in a parent body. test: `tests/storage/test_agent_copy_migration.py::test_nested_and_flat_source_shapes`.
- 2.3.14 - The copy migration writes one legacy_copy_ledger row per copied agent source row with the normalized payload hash, and reruns keep the copy-time hash. test: `tests/storage/test_agent_copy_migration.py`.
- 2.3.15 - The copy migration holds ACCESS EXCLUSIVE on workflow_definitions: a concurrent second-connection agent-row write blocks until the migration commits, and its post-commit landing is the post-copy drift the 7.1 backstop refuses. test: `tests/storage/test_agent_copy_migration.py::test_copy_lock_fences_concurrent_writes`.

### 2.4 Agent read-consumer rewiring [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/agent_resolver.py::*` — scope-reason: typed-manager resolution rewrite
- `src/gobby/workflows/engine/core.py::*` — scope-reason: agent cache on the typed manager and domain revision
- `src/gobby/dispatch/context.py::*` — scope-reason: bulk loader cutover
- `src/gobby/tasks/expansion/_common.py::*` — scope-reason: bulk loader cutover
- `src/gobby/cli/agents.py::*` — scope-reason: listing and detail-dict cutover
- `src/gobby/agents/dry_run.py::*` — scope-reason: delete the untyped name lookup
- `src/gobby/workflows/dry_run.py::*` — scope-reason: agent evaluation from step_workflow after trace extraction
- `src/gobby/dispatch/skill_composition.py::*` — scope-reason: required-skills read through step_workflow
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: resolve_agent consumer
- `src/gobby/dispatch/spawn.py::*` — scope-reason: resolve_agent consumer
- `src/gobby/mcp_proxy/tools/apply_persona.py::*` — scope-reason: resolve_agent consumer
- `src/gobby/hooks/event_handlers/_session_start/agents.py::*` — scope-reason: resolve_agent consumer
- `src/gobby/hooks/event_handlers/_agent.py::*` — scope-reason: resolve_agent consumer
- `src/gobby/build/observability.py::*` — scope-reason: resolve_agent consumer
- `src/gobby/servers/routes/agent_spawn.py::*` — scope-reason: resolve_agent consumer
- `src/gobby/hooks/factory.py::*` — scope-reason: manager construction wiring
- `src/gobby/tasks/expansion_service.py::*` — scope-reason: loader-output consumer
- `src/gobby/tasks/expansion/_compile.py::*` — scope-reason: loader-output consumer
- `src/gobby/workflows/dry_run_trace.py` (new)
- `tests/workflows/test_agent_resolver.py::*` — scope-reason: typed-resolution retarget of the resolver suite
- `tests/agents/test_dry_run.py::*` — scope-reason: deleted _load_agent_body retarget
- `tests/cli/test_agent_definitions_cli.py::*` — scope-reason: typed listing and nested step_workflow CLI retarget

- `resolve_agent` (`agent_resolver.py:17-53`) →
  `AgentDefinitionManager.get_by_name` (hydrated). Add
  `resolve_agent_with_row(name, db, project_id, cli_source) ->
  tuple[AgentDefinitionBody, AgentDefinitionRow]` for the spawn path (3.2
  needs `row.step_workflow_id`); plain `resolve_agent` keeps its signature for
  the other callers (spawn factory, dispatch/spawn, apply_persona,
  `_session_start/agents.py`, `_agent.py`, build/observability).
- `RuleEngine` (`engine/core.py`; construction at `:128`, cache invalidation
  at `:689-715`): `self.agent_manager = AgentDefinitionManager(db)`;
  `_agent_def_cache` invalidates on `get_definitions_revision("agents")`.
  Child-only step-workflow mutations reach this cache because every child
  write bumps both `agent_step_workflows` and `agents` (1.3's dual-domain
  bump contract).
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
- 2.4.9 - A child-only step-workflow edit or delete invalidates the cached hydrated agent, and the next resolution returns the updated body. test: `tests/workflows/test_agent_resolver.py`.

## P3: Runtime Snapshot Cutover
`kind: framing`

**Goal**: enforcement, transitions, completion, context, recovery, and cleanup
run entirely from immutable per-session snapshots; generated `-steps` rows and
the `-steps` name coupling are gone.

### 3.1 Step instance model and manager [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/workflows/step_instances.py` (new)
- `src/gobby/storage/hub/protocol.py::*` — scope-reason: mutation-lock rename and lock users
- `tests/workflows/test_step_instances.py` (new)

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
                                        # agent_step_workflow_id, or created_at,
                                        # and REJECTS an agent_name differing from
                                        # the stored row as a stale identity write
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
`agent_name` is therefore part of the immutable identity, not merely
`snapshot_json` (adversary round 2 APR2-012): a `save` whose `agent_name`
differs from the stored row is rejected loudly as a stale identity write —
silently keeping the stored name would hide the caller's bug, and writing the
new name would relabel the prior agent's snapshot and lineage.
`replace_for_session` is the only mutator that may change `agent_name`,
`snapshot_json`, or `agent_step_workflow_id`, and it changes them together.

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
  the mutators inside double-acquiring it. The re-entrancy is load-bearing
  adapter behavior, not a new mechanism (adversary round 3 APR3-008): a
  nested `transaction_immediate` on the **same adapter instance** joins the
  ambient transaction (`storage/hub/_ambient.py:43-49`) and routes through
  `_PostgresTransaction.acquire_additional_lock`, whose exact-target equality
  short-circuit (`postgres_pool.py:313-314`; `LockTarget` cases are frozen
  dataclasses, so `in` is value equality) returns without touching the
  priority check. Two constraints follow and are pinned here: the re-entrancy
  test MUST run through the real `PostgresHubDatabase` adapter with ambient
  nesting — a fake proves nothing about the short-circuit — and every caller
  that wraps a span MUST reuse the injected adapter instance, because a
  second adapter instance opens a second transaction and self-deadlocks on
  the advisory lock instead of re-entering.
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
- 3.1.4 - `replace_for_session` swaps snapshot, lineage id, agent name, and step position together, and is the only mutator that changes snapshot, lineage, or agent identity. test: `tests/workflows/test_step_instances.py::test_replace_for_session_swaps_snapshot_and_lineage`.
- 3.1.5 - A step-scope variable merge concurrent with an enforcement save is not lost. test: `tests/workflows/test_step_instances.py::test_merge_variables_serializes_against_save`.
- 3.1.6 - The mutation lock is re-entrant through the real Postgres adapter with ambient nesting on one shared adapter instance: a caller-held section wrapping a read and its computed save does not deadlock the mutators, and a merge committed outside that section cannot interleave into it. test: `tests/workflows/test_step_instances.py::test_mutation_lock_is_reentrant`.
- 3.1.7 - A save carrying a compare-and-set precondition from a pre-persona read is rejected as stale rather than rewriting the replaced instance's step position and variables. test: `tests/workflows/test_step_instances.py::test_stale_save_after_persona_replacement_rejected`.
- 3.1.8 - A save whose agent_name differs from the stored row is rejected as a stale identity write, with or without the compare-and-set precondition. test: `tests/workflows/test_step_instances.py::test_save_rejects_agent_identity_change`.

### 3.2 Data-plane cutover and instance copy migration [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py` (new)
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: spawn cutover, extraction, and failure-boundary rework
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: registration deletion and prepared-spawn threading
- `src/gobby/dispatch/spawn.py::*` — scope-reason: registration deletion
- `src/gobby/mcp_proxy/tools/apply_persona.py::*` — scope-reason: persona instance transitions and cross-row atomicity
- `src/gobby/workflows/engine/enforcement_checks.py::*` — scope-reason: snapshot reader rewrite
- `src/gobby/workflows/engine/enforcement_completion.py::*` — scope-reason: snapshot writer rewrite
- `src/gobby/workflows/engine/enforcement_handlers.py::*` — scope-reason: snapshot writer rewrite
- `src/gobby/workflows/engine/enforcement.py::*` — scope-reason: unpacker signature updates
- `src/gobby/workflows/engine/core.py::*` — scope-reason: instance-manager construction cutover
- `src/gobby/hooks/factory.py::*` — scope-reason: instance-manager wiring
- `src/gobby/workflows/step_context.py::*` — scope-reason: snapshot reader rewrite
- `src/gobby/hooks/session_coordinator.py::*` — scope-reason: exit-condition read from the snapshot
- `src/gobby/workflows/hooks.py::*` — scope-reason: context-injection field rename
- `src/gobby/agents/idle_check_handler.py::*` — scope-reason: idle-reprompt field rename
- `src/gobby/agents/step_workflow.py::*` — scope-reason: module deleted in this task
- `src/gobby/agents/sync.py::*` — scope-reason: scaffolding call removal
- `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py::*` — scope-reason: process-termination compensation
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: prepared-spawn consumption at five call sites
- `src/gobby/agents/spawn.py::*` — scope-reason: preparation inversion
- `src/gobby/agents/spawn_models.py::*` — scope-reason: SpawnRequest gains the prepared-spawn field
- `src/gobby/agents/resume_executor.py::*` — scope-reason: same-session resume continuity preserved across the typed-instance cutover (#18974)
- `src/gobby/runner_lifecycle_agents.py::*` — scope-reason: daemon_stop terminal-reason anchor for resume continuity
- `src/gobby/dispatch/daemon_resume.py::*` — scope-reason: sole resume_agent_run caller; continuity anchor, no edit
- `src/gobby/workflows/state_manager.py::*` — scope-reason: legacy merge-path lock user
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py::*` — scope-reason: persona caller transaction audit
- `src/gobby/servers/websocket/chat/_session.py::*` — scope-reason: fail-closed persona propagation
- `crates/gcore/assets/schema/migrations/NNN_copy_agent_step_instances.sql` (new)
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: MIGRATIONS entry registration per 1.5
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pinned root hashes re-armed for the added MIGRATIONS entry
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated release-pinned identity for the added MIGRATIONS entry
- `tests/storage/test_instance_copy_migration.py` (new)
- `tests/workflows/test_step_snapshot_semantics.py` (new)
- `tests/workflows/test_step_enforcement.py::*` — scope-reason: enforcement suite rewritten onto AgentStepInstanceManager
- `tests/workflows/test_step_runtime_transitions.py::*` — scope-reason: transition suite rewritten onto the typed instance (adversary round 3 APR3-015)
- `tests/workflows/test_step_enforcement_audit.py::*` — scope-reason: enforcement-audit fixtures rewritten onto the typed instance (adversary round 3 APR3-015)
- `tests/workflows/test_step_error_codes.py::*` — scope-reason: error-code fixtures rewritten onto the typed instance (adversary round 3 APR3-015)
- `tests/hooks/test_session_coordinator.py::*` — scope-reason: exit-condition coordinator fixtures rewritten onto the typed instance (adversary round 3 APR3-015)
- `tests/workflows/test_step_context.py::*` — scope-reason: WorkflowInstanceManager patch paths retargeted at the typed manager
- `tests/workflows/test_agent_workflow_completion.py::*` — scope-reason: completion-gate suite rewritten onto the typed instance
- `tests/agents/test_spawn_prepare_resume.py::*` — scope-reason: same-session resume seam; continuity assertions onto the typed instance
- `tests/agents/test_spawn.py::*` — scope-reason: moved preparation boundary
- `tests/agents/test_spawn_executor.py::*` — scope-reason: moved preparation boundary
- `tests/agents/test_spawn_executor_droid.py::*` — scope-reason: moved preparation boundary
- `tests/agents/test_srt_spawn.py::*` — scope-reason: moved preparation boundary
- `tests/agents/test_resume_executor.py::*` — scope-reason: moved preparation boundary
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py::*` — scope-reason: moved preparation boundary
- `tests/mcp_proxy/tools/test_apply_persona.py::*` — scope-reason: typed-instance retarget
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: deleted-symbol retarget
- `tests/dispatch/test_dispatcher.py::*` — scope-reason: dispatcher spawn assertion retargeted off the deleted _step_workflow_name seed (adversary round 4 APR4-005)

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

Hoisting preparation creates a new pre-launch failure envelope, and it gets
**one idempotent cleanup owner**, not per-boundary ad-hoc handling (adversary
round 2 APR2-013). The owner deletes whatever pre-launch acquisitions exist —
the child session row, its initial `session_variables` row, the `agent_runs`
row, the on-disk prompt file, and the `agent_step_instances` row — revokes a
never-launched run's managed credential, tolerates already-deleted rows so
repeated cleanup is safe, and is invoked on failure at every boundary between
preparation and provider launch:
(a) failure **inside** `prepare_terminal_spawn` (adversary round 3 APR3-012,
fixer-induced by APR2-013): the function acquires in sequence — child session
(`spawn.py:171`), initial variables (`:177-179`), the `agent_runs` row plus
pickup metadata and prompt file (`_prepare_run_for_session`, `:354-446`), and
the pre-launch credential (`_issue_prelaunch_credential`, `:329-351`) — and
today contains **no** exception handling, so a mid-sequence raise propagates
with no handle to the partial state and the caller cannot compensate what it
cannot see. The owner therefore lives **inside** `prepare_terminal_spawn`: an
exception path (`try`/`except` re-raise after teardown) that tears down every
acquisition already made, verified by fault injection after each acquisition
in `tests/agents/test_spawn.py`. `PreparedSpawn` remains success-only; no
partial-result type is introduced;
(b) failure of the step-instance save after successful preparation;
(c) failure between the save and the provider launch (spawn-context
construction, credential/bootstrap work) — this boundary also deletes the
already-saved instance row, which the save-failure branch alone would miss.
Boundaries (b) and (c) invoke the same owner from the caller, passing the
returned `PreparedSpawn`.
No provider process exists at any of these points, so the owner never invokes
post-launch termination cleanup; it leaves no durable spawn rows, no live
credentials attached to a never-launched run, and no claimed task. A cleanup
failure is surfaced with the surviving row ids for operator recovery rather
than hiding the leak behind the original error. Post-launch failures route
through the termination compensation below, which is the other half of the
same completeness requirement.

**`resume_executor.py:192` is excluded from the preparation inversion.** The
resume path calls `prepare_terminal_resume` (`spawn.py:227`), the #18974
sibling of `prepare_terminal_spawn` that relaunches a daemon-stopped run on
the **same child session** via a CAS session rebind; it is not on the
`SpawnRequest` path — `resume_agent_run` builds its own `spawn_context`
inline and its sole caller supplies no `PreparedSpawn`. Preparation stays
inside `resume_agent_run`.

    **Resume step-state continuity is a live contract this task must
    preserve** (#18974, closed 2026-07-27 — Epic Review Notes correction 16;
    adversary round 2 APR2-001). A daemon-stop resume reuses the same child
    session, and `cleanup_agent_runtime_state` (`runtime_cleanup.py`) deletes
    instance rows only when `terminal_reason != 'daemon_stop'`
    (`runner_lifecycle_agents.py` terminalizes shutdown-killed runs with
    `daemon_stop`), so the retained row is found again by the resumed run
    under the `UNIQUE(session_id)` key: the agent continues at its step with
    its variables. The cutover must carry all three halves of that contract
    onto `agent_step_instances`: (a) the copy migration's live-status filter
    includes the paused sessions daemon-stop parking produces; (b) 3.3's
    typed `delete_for_session` cleanup keeps the same `terminal_reason` gate;
    (c) the resumed run resolves its retained typed row rather than building
    a fresh snapshot — recovery's fresh-snapshot path (3.3) remains the
    fallback for a genuinely missing row, never the resume path's normal
    behavior. `tests/agents/test_spawn_prepare_resume.py` pins the seam today
    against the legacy table and retargets its instance assertions here.
    `dispatch/daemon_resume.py:70` is the sole caller of `resume_agent_run`
    and stays in Targets as the continuity anchor; this task makes no edit
    there.

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
- **Caller-variable reserved-key rejection (adversary round 4 APR4-007)**:
  the public `apply_persona` tool forwards an arbitrary `variables` dict
  (`agents_spawn_tools.py:90-111`) and `apply_persona_impl` overlays it
  **after** the authoritative persona delta (`apply_persona.py:289-296`), so
  a caller key wins every collision — a submitted `_agent_type` commits agent
  B's session identity beside agent A's requested snapshot even once the
  cross-row transaction lands, and `SessionVariableManager.merge_variables`
  (`state_manager.py:307-324`) applies updates with no validation. Before
  opening the transition transaction, reject any caller key that collides
  with the just-built persona delta or the task overlay (`set(variables) &
  (set(changes) | set(extra_vars))`) or that satisfies
  `is_reserved_workflow_variable` — the same guard the public `set_variable`
  tool already applies (`_variables.py:118-122`); the runtime-reserved set
  covers `_step_workflow_name` and the completion/step-state keys, and the
  delta collision covers `_agent_type` and the other persona-owned fields
  without hand-maintaining a second list. Rejection is a structured tool
  error that leaves session variables and the typed instance unchanged.
  Adversarial cases at both seams — the MCP tool wrapper and
  `apply_persona_impl` — prove `_agent_type`, `_step_workflow_name`, and
  `step_workflow_complete` smuggling is rejected with both rows untouched,
  and that non-reserved caller variables still merge.
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
- **Enforcement test-seam wave (adversary round 2 APR2-009)**: the
  enforcement-side legacy suites move with their production files in this
  commit — `tests/workflows/test_step_enforcement.py` constructs
  `WorkflowInstanceManager` and `WorkflowInstance` throughout,
  `tests/workflows/test_step_context.py` patches
  `step_context.WorkflowInstanceManager` at `:36` and `:204`, and
  `tests/workflows/test_agent_workflow_completion.py` seeds instances through
  the legacy manager. All three are rewritten onto `AgentStepInstanceManager`
  fixtures here; the cleanup-side suites move in 3.3 with their files.
- **Snapshot regression suite created here (adversary round 2 APR2-007)**:
  this task creates `tests/workflows/test_step_snapshot_semantics.py` with
  the behaviors its own acceptance cites — the fault-injection, atomicity,
  auto-claim, and web-chat propagation cases ((g)–(l) of the 3.4 matrix).
  3.3 adds its recovery and compaction cases; 3.4 completes and audits the
  full matrix. An acceptance command may depend only on test artifacts that
  exist when its deliverable executes.
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
- **Instance copy migration** (same commit; an `EmbeddedMigration` SQL asset
  per 1.5, `information_schema`-guarded and fresh-lineage no-op): one row per
  session, selected deterministically (adversary round 2 APR2-014):
  - **Qualification**: only rows with `workflow_name ~ '-steps$'` are
    agent-step candidates; `agent_name = regexp_replace(workflow_name,
    '-steps$', '')`. A live-session instance row that does not match the
    predicate RAISEs with its session and workflow name — after the split
    there is no table to hold it, and dropping it silently would be the
    mid-epic data loss the fail-loud contract forbids. (The known
    non-matching rows — `session-lifecycle`, `auto-task` — sit on dead
    sessions and are excluded by the live-status filter, not by this
    predicate.)
  - **Active-identity resolution before ordering (adversary round 3
    APR3-009, fixer-induced by APR2-014; disagreement semantics corrected by
    adversary round 4 APR4-006)**: a timestamp total order across
    ALL suffix-matching rows can deterministically pick the wrong agent,
    because `build_persona_changes` preserves an existing instance without
    touching `updated_at` (`apply_persona.py:132-139`) — an A→B→A persona
    sequence leaves B's row newest while A is active. The session's
    `_agent_type` variable is the **sole authoritative identity**:
    the public persona path (`build_session_persona_changes`,
    `apply_persona.py:146-165`) writes `_agent_type` alone and deliberately
    never touches `_step_workflow_name` (only the spawn-time writers set it),
    so after a legitimate A→B switch the session holds `_agent_type = B`
    beside a stale `A-steps` value. That state is normal, not corruption:
    a conflicting `_step_workflow_name` is stale metadata to ignore, never a
    reason to RAISE. Restrict candidates to rows whose derived `agent_name`
    equals `_agent_type`. When no candidate matches the active identity —
    the switched-to agent never had a legacy row — migrate nothing for the
    session: the stale rows belong to the previous persona and are dropped
    with the legacy table in P7, and 3.3's recovery (the plan's canonical
    missing-instance path) rebuilds a fresh snapshot on next activation
    exactly when the active agent is stepful, with its structured
    reconstructed-continuity warning. RAISE with the session id and the
    variable values only for genuinely unresolved identity: a live session
    with qualifying rows and **no** `_agent_type` at all — after the split
    there is no way to know which snapshot is authoritative, and guessing is
    the silent wrong-agent migration this repair exists to prevent. (A
    `_step_workflow_name` present without `_agent_type` identifies no active
    agent, so it stays in this loud branch rather than serving as an
    identity fallback.)
  - **Total order within the resolved identity**: among the matching rows,
    latest `updated_at` wins **regardless of `enabled`**, with `id` ascending
    as the final tie-break so equal timestamps cannot copy an arbitrary row.
  - **Zero candidates**: a live session with no qualifying row migrates
    nothing — a step-less or non-agent session legitimately has no instance,
    and 3.3's recovery builds a fresh snapshot only if the agent is stepful.
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
    `agent_step_workflows` via the derived `agent_name` **scope-aware**:
    resolve the agent definition project-first against the session's
    `project_id` with global fallback — the same precedence `get_by_name`
    uses at runtime — then take that definition's child row, so a
    project-scoped agent's instance cannot attach to the global agent's
    lineage. Populate it **whenever that child row exists**, since it is the typed lineage column
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
  - **Write fence (adversary round 4 APR4-008; guard-first ordering round 5
    APR5-001)**: inside the existence guard — before the first source read —
    the migration takes `LOCK TABLE workflow_instances IN ACCESS EXCLUSIVE
    MODE` (the Constraints copy-fence rule) and, before committing, installs
    an idempotent BEFORE INSERT/UPDATE/DELETE trigger on `workflow_instances`
    that RAISEs unconditionally. Lock and trigger both live inside the
    guarded branch, so a post-7.1 fresh lineage with no `workflow_instances`
    records a receipted no-op instead of failing on the lock. Unlike the definition domains, instance
    rows have no `legacy_copy_ledger` backstop — a post-copy legacy write is
    runtime state the typed table never saw, silently destroyed at 7.1 —
    and the writers that survive version skew (a predecessor daemon not yet
    severed, or a missed consumer) must fail loudly instead. The trigger
    drops with the table in 7.1. Two-connection fault tests: a write
    in flight when the migration starts blocks at the table lock and then
    fails on the trigger; a write attempted after commit fails on the
    trigger; both leave the typed rows untouched.

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
- 3.2.13c - The resume path keeps its inline prepare_terminal_resume call, and a daemon-stop resume returns on the same child session with its retained typed instance at the same step and variables — the #18974 continuity contract holds across the storage cutover. symbol: `resume_agent_run`. file: `src/gobby/agents/resume_executor.py`. test: `tests/agents/test_spawn_prepare_resume.py`.
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
- 3.2.23 - The pinned schema root hashes and the release-pinned expected identity match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`. file: `src/gobby/storage/schema_expected_identity.json`.
- 3.2.24 - Fault injection at every pre-launch boundary — after each acquisition inside preparation (child session, initial variables, run row and prompt file, credential), after the instance save, and between save and launch — leaves no session, variable, run, or instance rows, no prompt file, and no live credentials, and running the cleanup owner twice is safe. test: `tests/workflows/test_step_snapshot_semantics.py::test_prelaunch_faults_leave_no_rows`. test: `tests/agents/test_spawn.py`.
- 3.2.25 - Candidate selection is deterministic within the resolved active identity: equal-timestamp duplicates resolve by the id tie-break, child lineage resolves project-first with global fallback, a non-qualifying instance row on a live session fails loudly, and a live session with no qualifying row and no persona state migrates nothing. test: `tests/storage/test_instance_copy_migration.py::test_candidate_resolution_determinism`.
- 3.2.26 - The enforcement, transition, audit, error-code, coordinator, step-context, and completion-gate suites construct the typed instance manager with no WorkflowInstanceManager import or patch remaining. test: `tests/workflows/test_step_enforcement.py`. test: `tests/workflows/test_step_runtime_transitions.py`. test: `tests/workflows/test_step_enforcement_audit.py`. test: `tests/workflows/test_step_error_codes.py`. test: `tests/hooks/test_session_coordinator.py`. test: `tests/workflows/test_step_context.py`. test: `tests/workflows/test_agent_workflow_completion.py`.
- 3.2.27 - The instance copy resolves the active identity from _agent_type alone: an A→B→A persona history with a stale-newer B row migrates A's snapshot and agent_name; an A→B switch with a stale A-steps _step_workflow_name and no B row migrates nothing for stepful and step-less B alike, without RAISEing; qualifying rows with no _agent_type fail loudly; and ordering applies only within the resolved identity's rows. test: `tests/storage/test_instance_copy_migration.py::test_active_identity_resolution`.
- 3.2.28 - The dispatcher spawn test asserts _step_workflow_name is absent from initial variables while still pinning the selected agent and task identity. test: `tests/dispatch/test_dispatcher.py::test_spawn_action_uses_services_and_records_agent_run`.
- 3.2.29 - apply_persona rejects caller variables colliding with the persona delta, the task overlay, or the runtime-reserved set before the transition transaction opens, leaving session variables and the typed instance unchanged, at both the tool wrapper and impl seams, while non-reserved variables still merge. test: `tests/mcp_proxy/tools/test_apply_persona.py`. test: `tests/workflows/test_step_snapshot_semantics.py`.
- 3.2.30 - The instance copy holds ACCESS EXCLUSIVE on workflow_instances and installs the legacy write-rejection trigger: a concurrent second-connection write blocks at the lock and then fails on the trigger, a post-commit write fails on the trigger, and typed rows are untouched in both cases. test: `tests/storage/test_instance_copy_migration.py::test_legacy_write_fence`.

### 3.3 Recovery, cleanup, and auxiliary surfaces [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `src/gobby/hooks/session_activation.py::*` — scope-reason: recovery rewrite onto the typed instance
- `src/gobby/hooks/event_handlers/_session_end.py::*` — scope-reason: gated instance cleanup
- `src/gobby/agents/runtime_cleanup.py::*` — scope-reason: typed delete_for_session
- `src/gobby/agents/terminal_cleanup.py::*` — scope-reason: cleared-state log key rename
- `src/gobby/workflows/reserved_variables.py::*` — scope-reason: remove _step_workflow_name
- `src/gobby/mcp_proxy/tools/workflows/_variables.py::*` — scope-reason: scope-parameter rewrite
- `src/gobby/mcp_proxy/tools/workflows/_query.py::*` — scope-reason: get_step_status rewrite
- `src/gobby/mcp_proxy/tools/workflows/__init__.py::*` — scope-reason: typed-manager tool registrations
- `src/gobby/servers/routes/workflows.py::*` — scope-reason: runtime-variable routes onto the typed manager
- `src/gobby/workflows/state_manager.py::*` — scope-reason: WorkflowInstanceManager deletion
- `src/gobby/workflows/definitions.py::*` — scope-reason: WorkflowInstance model deletion
- `src/gobby/storage/session_lifecycle.py::*` — scope-reason: sweep retarget at agent_step_instances
- `src/gobby/storage/sessions/_lifecycle_delegate.py::*` — scope-reason: sweep delegate retarget
- `src/gobby/hooks/event_handlers/_session_start/flow.py::*` — scope-reason: in-place compact reactivation continuity
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: typed-instance queries
- `tests/hooks/test_session_activation_reconciliation.py::*` — scope-reason: typed-instance recovery retarget
- `tests/workflows/test_instance_manager.py::*` — scope-reason: legacy-manager CRUD suite deleted; superseded by test_step_instances.py
- `tests/workflows/test_agent_workflow_runtime_cleanup.py::*` — scope-reason: runtime-cleanup suite rewritten onto the typed manager with the daemon_stop gate
- `tests/workflows/test_session_end_cleanup.py::*` — scope-reason: session-end cleanup suite rewritten onto the typed manager
- `tests/workflows/test_session_variable_manager.py::*` — scope-reason: legacy instance-manager fixture replaced with the typed manager
- `tests/workflows/test_step_snapshot_semantics.py`
- `tests/storage/sessions/test_lifecycle.py::*` — scope-reason: typed-table retarget
- `tests/storage/sessions/test_pruning.py::*` — scope-reason: typed-table retarget
- `tests/hooks/test_session_end_handlers.py::*` — scope-reason: typed-table retarget
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: typed-table retarget
- `tests/hooks/test_session_handoff_handlers.py::*` — scope-reason: typed-table retarget
- `tests/agents/test_runtime_cleanup.py::*` — scope-reason: runtime-cleanup fixtures onto the typed manager (adversary round 3 APR3-015)
- `tests/agents/test_lifecycle_monitor.py::*` — scope-reason: instance fixtures onto the typed manager (adversary round 3 APR3-015)
- `tests/agents/test_lifecycle_monitor_extra.py::*` — scope-reason: instance fixtures onto the typed manager (adversary round 3 APR3-015)
- `tests/agents/test_merge_lifecycle.py::*` — scope-reason: instance fixtures onto the typed manager (adversary round 3 APR3-015)
- `tests/agents/test_merge_orchestrator_contract.py::*` — scope-reason: instance constructor onto the typed manager (adversary round 3 APR3-015)
- `tests/e2e/test_build_dispatcher_autonomy.py::*` — scope-reason: E2E instance fixture onto the typed manager (adversary round 3 APR3-015)
- `tests/hooks/test_block_observability.py::*` — scope-reason: observability fixtures onto the typed manager (adversary round 3 APR3-015)
- `tests/mcp_proxy/tools/workflows/test_query.py::*` — scope-reason: WorkflowInstance fixtures onto the typed manager at the class deletion, ahead of 5.2's rename retarget (adversary round 3 APR3-015)
- `tests/mcp_proxy/tools/workflows/test_variables.py::*` — scope-reason: scope-parameter and typed-instance retarget at the class deletion (adversary round 3 APR3-015)
- `tests/scheduler/test_cron_scheduler.py::*` — scope-reason: instance seeding onto the typed manager (adversary round 3 APR3-015)
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py::*` — scope-reason: server-cleanup instance constructors onto the typed manager (adversary round 3 APR3-015)
- `tests/workflows/test_definitions.py::*` — scope-reason: WorkflowInstance model cases deleted with the model (adversary round 3 APR3-015)
- `src/gobby/mcp_proxy/tools/agents_termination.py::*` — scope-reason: workflow_instances_deleted result key renamed off the legacy model (adversary round 4 APR4-003)
- `tests/mcp_proxy/tools/test_agents.py::*` — scope-reason: termination-result case gains the renamed-key assertion (adversary round 4 APR4-003)

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
  `delete_for_session`, **preserving the `terminal_reason != 'daemon_stop'`
  retention gate** in `cleanup_agent_runtime_state` (#18974 — adversary round
  2 APR2-001): daemon-stop terminalization retains the typed instance so the
  same-session resume returns at its step; every other terminal reason
  deletes it. `terminal_cleanup.py:180` logs the cleared-state
  summary under a `workflow_instances=` key (moved from `agent_cleanup.py`
  since the original draft); rename it to `agent_step_instances=`, since
  7.2's audit matches the token and nothing else owns this occurrence. The
  same rename class has one more externally returned occurrence (adversary
  round 4 APR4-003): the MCP termination wrapper
  `agents_termination.py:34` returns the cleanup count under a
  `workflow_instances_deleted` result key — rename it to
  `agent_step_instances_deleted`. No test asserts the old key today (the
  fakes set the `workflow_instance_rows` attribute only), so the rename
  gains its seam here: the termination-result case in
  `tests/mcp_proxy/tools/test_agents.py:735-755` additionally asserts the
  renamed key carries the fake's count.
- **Cleanup-side test-seam disposition (adversary round 2 APR2-009)**, same
  commit as the class deletion: `tests/workflows/test_instance_manager.py`
  (the legacy manager's CRUD suite) is deleted — its behaviors are superseded
  by `tests/workflows/test_step_instances.py` (3.1);
  `tests/workflows/test_agent_workflow_runtime_cleanup.py` rewrites onto the
  typed manager and keeps pinning the daemon_stop retention gate;
  `tests/workflows/test_session_end_cleanup.py` rewrites onto the typed
  manager; `tests/workflows/test_session_variable_manager.py` replaces its
  legacy instance-manager fixture with the typed one. **The full class-wide
  sweep (adversary round 3 APR3-015, fixer-induced by APR2-009's partial
  repair)** additionally dispositions every remaining `WorkflowInstance`/
  `WorkflowInstanceManager` import, constructor, and string patch under
  `tests/`: the transition/audit/error-code/coordinator suites move in 3.2;
  this task owns `tests/agents/test_runtime_cleanup.py`,
  `test_lifecycle_monitor.py`, `test_lifecycle_monitor_extra.py`,
  `test_merge_lifecycle.py`, `test_merge_orchestrator_contract.py`,
  `tests/e2e/test_build_dispatcher_autonomy.py`,
  `tests/hooks/test_block_observability.py` and
  `test_session_end_handlers.py` (string patches of
  `gobby.workflows.state_manager.WorkflowInstanceManager`),
  `tests/mcp_proxy/tools/workflows/test_query.py` and `test_variables.py`
  (their WorkflowInstance fixtures break at this commit, before 5.2's rename
  retarget), `tests/scheduler/test_cron_scheduler.py`,
  `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py`,
  and the `WorkflowInstance` model cases in
  `tests/workflows/test_definitions.py`, which are deleted with the model.
  The symbol-and-string sweep is re-run at implementation time and every
  remaining hit is dispositioned in this commit — together with 3.2's
  enforcement-side wave this dispositions every `WorkflowInstanceManager`
  seam under `tests/`, so the class deletion below leaves no red suite.
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
- Delete `WorkflowInstance` from `definitions.py:917-965` and
  `WorkflowInstanceManager` from `state_manager.py:68-194`; fix remaining
  test imports.

**Acceptance:**

- 3.3.1 - Restart recovery rebuilds step state from the agent definition without any -steps name parsing. symbol: `_ensure_step_instance`. file: `src/gobby/hooks/session_activation.py`.
- 3.3.2 - Session-end and agent-terminal cleanup delete the per-session instance. file: `src/gobby/hooks/event_handlers/_session_end.py`. file: `src/gobby/agents/runtime_cleanup.py`.
- 3.3.3 - Workflow-scoped variable tools use the scope parameter against the single instance. file: `src/gobby/mcp_proxy/tools/workflows/_variables.py`.
- 3.3.4 - WorkflowInstance and WorkflowInstanceManager no longer exist. file: `src/gobby/workflows/state_manager.py`.
- 3.3.5 - _step_workflow_name is absent from reserved variables and all rule/variable plumbing. file: `src/gobby/workflows/reserved_variables.py`.
- 3.3.6 - Fresh-snapshot recovery emits one structured warning carrying the session, agent name, resolved definition ids, and a stable recovery marker. test: `tests/workflows/test_step_snapshot_semantics.py`.
- 3.3.7 - step_workflow_complete is seeded from the typed instance after recovery creates it, with no reference to the removed variable. test: `tests/hooks/test_session_activation_reconciliation.py::test_completion_seed_after_step_instance_recovery`.
- 3.3.8 - The MCP tool registrations and generic runtime-variable routes use the typed manager before WorkflowInstanceManager is deleted, so the tree imports at that commit. file: `src/gobby/servers/routes/workflows.py`. file: `src/gobby/mcp_proxy/tools/workflows/__init__.py`.
- 3.3.9 - The agent terminal-cleanup log no longer names workflow_instances. file: `src/gobby/agents/terminal_cleanup.py`.
- 3.3.10 - Session-end cleanup keeps the terminal-outcome gate, so a COMPACT or IDLE web-chat end retains the typed instance and only an expired end deletes it. file: `src/gobby/hooks/event_handlers/_session_end.py`. test: `tests/hooks/test_session_end_handlers.py`.
- 3.3.12 - In-place compact reactivation (#18994) leaves the typed instance keyed to the same session across a compact restart, with no ownership move and no legacy table named. file: `src/gobby/storage/session_lifecycle.py`. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.
- 3.3.13 - Orphan handoff expiry only flips status; the marker-gated retention sweep deletes typed instances for sessions expired past the revival horizon, with no legacy table named. symbol: `expire_orphaned_handoff_sessions`. symbol: `prune_stale_compact_workflow_instances`. file: `src/gobby/storage/session_lifecycle.py`.
- 3.3.14 - A compacted mid-workflow agent resumes on its same session at the same nonzero step with the same variables after the port. test: `tests/storage/sessions/test_lifecycle.py`. test: `tests/workflows/test_step_snapshot_semantics.py`.
- 3.3.11 - The spawn initial-variables suite queries the typed instance instead of importing the deleted WorkflowInstanceManager, dropping its `<agent>-steps` name arguments. test: `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py`.
- 3.3.15 - Daemon-stop terminal cleanup retains the typed instance and a resumed run on the same session sees the same step and variables; every other terminal reason deletes the instance. file: `src/gobby/agents/runtime_cleanup.py`. test: `tests/workflows/test_agent_workflow_runtime_cleanup.py`.
- 3.3.16 - Every legacy instance-manager test seam is deleted or rewritten onto the typed manager and no test imports or patches WorkflowInstanceManager. test: `tests/workflows/test_instance_manager.py`. test: `tests/workflows/test_session_end_cleanup.py`. test: `tests/workflows/test_session_variable_manager.py`.
- 3.3.17 - The termination tool result reports the cleanup count as agent_step_instances_deleted, the old key is gone, and the termination-result case asserts the renamed key. file: `src/gobby/mcp_proxy/tools/agents_termination.py`. test: `tests/mcp_proxy/tools/test_agents.py`.

### 3.4 Snapshot behavior regression suite [category: test] (depends: 3.3)
`kind: deliverable`

Target: `tests/workflows/test_step_snapshot_semantics.py`

Standalone behavior-pinning suite (isolated test daemon state,
`GOBBY_TEST_PROTECT=1`). The file is created by 3.2 with the fault-injection
and atomicity cases its acceptance cites, extended by 3.3 with the recovery
and compaction cases, and completed here — this task adds the remaining
behaviors and audits the whole matrix, so every earlier acceptance command
ran against a file that existed when its deliverable executed (adversary
round 2 APR2-007): (a) definition edited during an active run — running
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
expired end still deletes it; (n) daemon-stop resume continuity (#18974) — a
daemon-stop-terminalized run's instance survives cleanup, and the resumed run
on the same session continues at the same nonzero step with the same
variables, while every other terminal reason deletes the instance.

**Acceptance:**

- 3.4.1 - All fourteen pinned behaviors pass against the snapshot runtime. test: `tests/workflows/test_step_snapshot_semantics.py`.
- 3.4.2 - Post-launch fault injection runs against the real spawn executor at all four failure points and proves no PID, tmux session, or attached lease survives. test: `tests/workflows/test_step_snapshot_semantics.py::test_post_launch_failure_terminates_process`.

## P4: Rules, Variables, Pipelines Rewiring
`kind: framing`

**Goal**: each remaining domain reads and writes only its typed table; each
task ships its copy migration in the same commit as its cutover.

**Ordering**: every P4 cutover depends on completed P3; 4.2 depends on 4.1,
and 4.3 depends on 4.2 (fully serialized). The domains are independent in
storage but not in source files. P2 and P3 still own `apply_persona.py`,
`engine/core.py`, `hooks/session_activation.py`, `workflows/hooks.py`,
`state_manager.py`, and the spawn factory while P4 would otherwise be free to
start; 4.1 and 4.2 overlap each other on `hooks.py`,
`_session_start/agents.py`, `apply_persona.py`, and `state_manager.py`.
Starting P4 off P1/P2 lets a rule or variable cutover edit the pre-P3 shape of
a file P3 is concurrently rewriting, which produces merge conflicts and
intermediate commits that do not run. 4.3 serializes after 4.2 because the E3
generic-surface shrink puts `routes/workflows.py`, `_definitions.py`, and
`workflows/imports.py` in every cutover's commit: 4.1 and 4.2 add their kind
rejections to those files, and 4.3 both adds the pipeline rejection and
replaces `sync_imported_definition`'s per-kind rejections with typed dispatch
— a rewrite that is only correct once every domain it dispatches to has cut
over.

### 4.1 Rules cutover and copy migration [category: code] (depends: P3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/engine/core.py::*` — scope-reason: rule loading onto the typed manager
- `src/gobby/workflows/sync_rules.py::*` — scope-reason: sync cutover with the enabled guard
- `src/gobby/hooks/session_activation.py::*` — scope-reason: rules revision listener registration
- `src/gobby/mcp_proxy/tools/workflows/_rules.py::*` — scope-reason: rule MCP CRUD cutover
- `src/gobby/servers/routes/rules.py::*` — scope-reason: rule HTTP route cutover
- `src/gobby/cli/rules.py::*` — scope-reason: rules CLI cutover
- `src/gobby/mcp_proxy/tools/apply_persona.py::*` — scope-reason: persona rule-load cutover
- `src/gobby/hooks/event_handlers/_session_start/agents.py::*` — scope-reason: session-start rule-load cutover
- `src/gobby/workflows/engine/effects.py::*` — scope-reason: RuleDefinitionRow type propagation
- `src/gobby/workflows/selectors.py::*` — scope-reason: RuleDefinitionRow type propagation
- `src/gobby/workflows/hooks.py::*` — scope-reason: RuleDefinitionRow type propagation
- `src/gobby/workflows/engine/evaluation.py::*` — scope-reason: RuleDefinitionRow import closure
- `src/gobby/workflows/reserved_variables.py::*` — scope-reason: RuleDefinitionRow import closure
- `crates/gcore/assets/schema/migrations/NNN_copy_rule_definitions.sql` (new)
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: MIGRATIONS entry registration per 1.5
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pinned root hashes re-armed for the added MIGRATIONS entry
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated release-pinned identity for the added MIGRATIONS entry
- `src/gobby/servers/routes/workflows.py::*` — scope-reason: rule-kind rejection on the generic router
- `src/gobby/mcp_proxy/tools/workflows/_definitions.py::*` — scope-reason: rule-kind rejection on the generic MCP tools
- `src/gobby/workflows/imports.py::*` — scope-reason: rule-kind import rejection until 4.3's typed dispatch
- `tests/servers/routes/test_workflows.py::*` — scope-reason: rule-kind rejection cases
- `tests/mcp_proxy/tools/test_workflow_crud.py::*` — scope-reason: rule-kind rejection cases
- `tests/workflows/test_imports.py::*` — scope-reason: rule-kind import rejection fixture (adversary round 3 APR3-003)
- `tests/workflows/test_rule_engine.py::*` — scope-reason: rule-behavior suite seeding moved off LocalWorkflowDefinitionManager (adversary round 3 APR3-004)
- `tests/workflows/test_session_defaults.py::*` — scope-reason: rule-sync-seeded halves retargeted with the sync cutover (adversary round 3 APR3-004; the variable-seeded halves move in 4.2)
- `tests/workflows/test_rule_yaml_sync.py::*` — scope-reason: typed-manager retarget of the rule sync suite
- `tests/servers/routes/test_rules_routes.py::*` — scope-reason: typed-manager retarget of the rule routes suite
- `tests/mcp_proxy/tools/test_rule_tools.py::*` — scope-reason: typed-manager retarget of the rule MCP suite
- `tests/storage/test_rule_copy_migration.py` (new)

- `RuleEngine`: `self.rule_manager = RuleDefinitionManager(db)`; `_load_rules`
  (`core.py:584-607`) → `list_by_event`; row type becomes `RuleDefinitionRow`
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
  `_has_gobby_rule_name_collision` → manager query. Template refreshes go
  through `update_from_sync` so `_build_rule_update_fields`'s guard semantics
  (legacy `enabled_user_modified`, now `enabled_pinned`) survive the cutover
  (1.2); `tests/workflows/test_rule_yaml_sync.py` retargets its
  adoption-while-unpinned and preservation-while-pinned cases at the typed
  manager.
- **Generic-surface shrink for kind `rule` (enhancement E3, same commit)**:
  the generic router omits `workflow_type='rule'` rows from list/get and
  rejects writes for that kind naming `/api/rules`; the generic MCP tools
  reject kind `rule` naming the rule domain tools;
  `imports.py::sync_imported_definition` refuses rule definitions naming the
  rule tools (typed dispatch arrives in 4.3).
- Register `clear_active_rule_names_cache` as a `"rules"` revision listener in
  `session_activation.py` (replaces the bump's hardcoded import).
- Rule surfaces swap managers internally (`_rules.py`, `routes/rules.py`,
  `cli/rules.py`, persona/session-start rule loads).
- Copy migration: guarded; dup-check; copy `workflow_type='rule'` rows
  (priority, sources, tags intact; source normalized); targetless `ON CONFLICT
  DO NOTHING` per the Constraints conflict-target rule; count validation and
  the Constraints equivalence guard, including `target.id = source.id`.
  Inside the existence guard — before the first source read — the migration
  takes `LOCK TABLE workflow_definitions IN ACCESS EXCLUSIVE MODE` per the
  Constraints copy-fence rule (adversary round 4 APR4-008; guard-first
  ordering round 5 APR5-001), so on a live hub the copy, the guard, and the
  ledger hashes read one frozen snapshot with no concurrent rule-row write
  torn between them, while a post-7.1 fresh lineage records a receipted
  no-op instead of failing on the lock.
  The migration also writes one `legacy_copy_ledger` row per copied source
  row (preserved legacy id, domain `'rules'`, normalized payload hash, `ON
  CONFLICT (legacy_id) DO NOTHING`) for 7.1's directional drop backstop
  (adversary round 3 APR3-011).
  `tests/storage/test_rule_copy_migration.py` covers all six cases in full,
  restated here rather than referenced, because an expansion agent receives
  only this deliverable section and cannot read Constraints: (1) first run;
  (2) rerun over already-migrated live rows; (3) rerun over already-migrated
  soft-deleted rows; (4) two soft-deleted rule rows sharing a natural key;
  (5) a pre-existing typed row with a divergent payload — loud failure;
  (6) a pre-existing live typed row with the same natural key and payload but
  a different UUID — loud failure.
- **Test-seam wave**: once the engine loads rules from the typed manager,
  every rule-behavior suite that seeds rules through the legacy manager stops
  observing its own fixtures, so this commit also moves that seeding to
  `RuleDefinitionManager` across the rule-behavior suites. The two known
  suites are exact Targets rather than an implementation-time discovery
  (adversary round 3 APR3-004): `tests/workflows/test_rule_engine.py` (its
  `manager` fixture at `:52-53` and `_insert_rule` at `:70-89` seed
  `LocalWorkflowDefinitionManager` across ~55 tests) and the rule-seeded
  halves of `tests/workflows/test_session_defaults.py`, whose fixtures reach
  the legacy store through `sync_rules.py`'s own manager construction
  (`sync_rules.py:70,136`) and go dark when this commit cuts that over. The
  grep-derived sweep still runs at implementation time for stragglers; 7.1
  deletes any leftovers and 7.2's audit asserts closure.

**Acceptance:**

- 4.1.1 - Rule evaluation loads through the typed rule manager with event/group filtering and priority order. symbol: `RuleEngine._load_rules`. file: `src/gobby/workflows/engine/core.py`.
- 4.1.2 - Bundled rule sync writes the typed table and the self-heal UPDATE is gone. file: `src/gobby/workflows/sync_rules.py`.
- 4.1.3 - Rule mutations invalidate the active-rule-names cache via the rules revision listener. file: `src/gobby/hooks/session_activation.py`.
- 4.1.4 - Copy migration migrates 160+ rules including soft-deleted rows with counts validated. test: `tests/storage/test_rule_copy_migration.py`.
- 4.1.5 - Rule HTTP routes behave identically on the typed manager. file: `src/gobby/servers/routes/rules.py`. test: `tests/servers/routes/test_rules_routes.py`.
- 4.1.5a - Rule MCP tools and the rules CLI behave identically on the typed manager. file: `src/gobby/mcp_proxy/tools/workflows/_rules.py`. file: `src/gobby/cli/rules.py`.
- 4.1.5b - The rule row type propagates as RuleDefinitionRow through effects, selectors, and the hook rule tuples. file: `src/gobby/workflows/engine/effects.py`. file: `src/gobby/workflows/selectors.py`. file: `src/gobby/workflows/hooks.py`.
- 4.1.6 - The equivalence guard fails when a pre-existing typed rule row diverges from its legacy source. test: `tests/storage/test_rule_copy_migration.py`.
- 4.1.7 - Rerunning the rule copy over already-migrated soft-deleted rows completes without a primary-key abort. test: `tests/storage/test_rule_copy_migration.py::test_rerun_over_soft_deleted_rows`.
- 4.1.8 - Rerunning the rule copy over already-migrated live rows is a clean no-op, and two soft-deleted rule rows sharing a natural key both migrate. test: `tests/storage/test_rule_copy_migration.py::test_rerun_over_live_rows`.
- 4.1.9 - A live typed rule row matching a legacy row on natural key and payload but carrying a different UUID fails the guard loudly. test: `tests/storage/test_rule_copy_migration.py::test_divergent_identity_fails`.
- 4.1.10 - EvaluationMixin and is_internal_rule accept RuleDefinitionRow, and no rule-path module imports WorkflowDefinitionRow. file: `src/gobby/workflows/engine/evaluation.py`. file: `src/gobby/workflows/reserved_variables.py`.
- 4.1.11 - No generic surface can create or mutate a legacy rule row post-cutover: the generic HTTP routes, generic MCP tools, and the import path each reject kind `rule` naming the surviving domain surface. test: `tests/servers/routes/test_workflows.py`. test: `tests/mcp_proxy/tools/test_workflow_crud.py`. test: `tests/workflows/test_imports.py`.
- 4.1.12 - Bundled rule sync reaches the typed table through update_from_sync: a changed template enabled default is adopted on an untouched row and preserved on a pinned row. test: `tests/workflows/test_rule_yaml_sync.py`.
- 4.1.13 - The pinned schema root hashes and the release-pinned expected identity match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`. file: `src/gobby/storage/schema_expected_identity.json`.
- 4.1.14 - The rule copy migration writes one legacy_copy_ledger row per copied source row and reruns keep the copy-time hash. test: `tests/storage/test_rule_copy_migration.py`.
- 4.1.15 - The rule-engine suite seeds rules through the typed manager and observes its own fixtures after the cutover. test: `tests/workflows/test_rule_engine.py`.
- 4.1.16 - The rule copy migration holds ACCESS EXCLUSIVE on workflow_definitions: a concurrent second-connection rule-row write blocks until the migration commits, and its post-commit landing is the post-copy drift the 7.1 backstop refuses. test: `tests/storage/test_rule_copy_migration.py::test_copy_lock_fences_concurrent_writes`.

### 4.2 Variables cutover and copy migration [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/workflows/variable_defaults.py` (new)
- `src/gobby/workflows/state_manager.py::*` — scope-reason: defaults path unification and revision invalidation
- `src/gobby/workflows/hooks.py::*` — scope-reason: lazy-backfill unification
- `src/gobby/hooks/event_handlers/_session_start/agents.py::*` — scope-reason: defaults path unification
- `src/gobby/mcp_proxy/tools/apply_persona.py::*` — scope-reason: defaults path unification
- `src/gobby/workflows/sync_variables.py::*` — scope-reason: typed-column sync with the enabled guard
- `src/gobby/mcp_proxy/tools/workflows/_variables.py::*` — scope-reason: typed-field CRUD cutover
- `crates/gcore/assets/schema/migrations/NNN_copy_session_variable_defaults.sql` (new)
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: MIGRATIONS entry registration per 1.5
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pinned root hashes re-armed for the added MIGRATIONS entry
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated release-pinned identity for the added MIGRATIONS entry
- `src/gobby/servers/routes/workflows.py::*` — scope-reason: variable-kind rejection on the generic router
- `src/gobby/mcp_proxy/tools/workflows/_definitions.py::*` — scope-reason: variable-kind rejection on the generic MCP tools
- `src/gobby/workflows/imports.py::*` — scope-reason: variable-kind import rejection until 4.3's typed dispatch
- `tests/servers/routes/test_workflows.py::*` — scope-reason: variable-kind rejection cases
- `tests/mcp_proxy/tools/test_workflow_crud.py::*` — scope-reason: variable-kind rejection cases
- `tests/workflows/test_imports.py::*` — scope-reason: variable-kind import rejection fixture (adversary round 3 APR3-003)
- `tests/workflows/test_session_defaults.py::*` — scope-reason: defaults-application and variable-seeded retarget (adversary round 3 APR3-004)
- `tests/workflows/test_sync.py::*` — scope-reason: typed-manager retarget of the variable sync suite
- `tests/mcp_proxy/tools/workflows/test_variables.py::*` — scope-reason: typed-field CRUD retarget of the variable MCP suite
- `tests/storage/test_variable_copy_migration.py` (new)

- New `variable_defaults.py`: `load_variable_defaults(db, project_id) ->
  dict[str, Any]` on `SessionVariableDefaultManager.get_defaults_map`.
  **Unify all four application paths on it** and drop the `source='installed'`
  filter inconsistency: `SessionVariableManager._get_variable_defaults`
  (`state_manager.py:227-256`), `workflows/hooks.py:705-750` lazy backfill
  (replace the inline loop; keep the `_variable_defaults_loaded` sentinel),
  `_session_start/agents.py:211-225`, `apply_persona.py:48-88`.
  **Project scope is part of the contract, not an optional parameter
  (adversary round 3 APR3-010)**: today no application path passes a project
  — `_get_variable_defaults` has no project parameter and its SQL carries no
  `project_id` predicate, the hook backfill and session-start paths call
  `list_all(workflow_type="variable")` without the `project_id` they hold in
  scope, and `build_persona_changes` has no project at all — so project
  overrides are invisible or leak across projects depending on the reader.
  Each path therefore resolves the session's `project_id` from its session
  row before calling the helper; `get_defaults_map` defines the merge as
  project-first with global fallback deduplicated by name (the `get_by_name`
  precedence); and the `SessionVariableManager` TTL cache is **keyed by
  `(project_id, variables revision)`** — one flat unkeyed dict would serve
  project A's defaults to project B — with the variables-revision listener
  (1.4) invalidating all keys. A regression alternates sessions in project A,
  project B, and no-project across all four application paths and asserts
  each sees exactly its own overrides plus globals.
- `sync_variables.py`: write typed columns (`name`, `default_value`,
  `description`) through `update_from_sync` on refresh; variable-definition
  MCP tools (`_variables.py` list/get/create/update/delete/export) move to
  typed fields. `TestSyncBundledVariables` in `tests/workflows/test_sync.py`
  retargets at the typed manager.
- **Generic-surface shrink for kind `variable` (enhancement E3, same
  commit)**: the generic router omits `workflow_type='variable'` rows from
  list/get and rejects writes for that kind; the generic MCP tools reject kind
  `variable`; `imports.py::sync_imported_definition` refuses variable
  definitions. Rejections name the variable domain MCP tools — the HTTP
  `/api/variables` router arrives in 5.1.
- Copy migration: guarded; copy `workflow_type='variable'` rows into typed
  columns (`name = COALESCE(definition_json->>'variable', name)`,
  `default_value = definition_json->'value'`); normalize the `source='gobby'`
  anomaly to `'installed'` (matches its bundled YAML; becomes sync-managed and
  reader-visible again); dup-check, targetless `ON CONFLICT DO NOTHING` per the
  Constraints conflict-target rule, count validation, and the Constraints
  equivalence guard including `target.id = source.id`.
  Inside the existence guard — before the first source read — the migration
  takes `LOCK TABLE workflow_definitions IN ACCESS EXCLUSIVE MODE` per the
  Constraints copy-fence rule (adversary round 4 APR4-008; guard-first
  ordering round 5 APR5-001), so on a live hub the copy, the guard, and the
  ledger hashes read one frozen snapshot with no concurrent variable-row
  write torn between them, while a post-7.1 fresh lineage records a
  receipted no-op instead of failing on the lock.
  The migration also writes one `legacy_copy_ledger` row per copied source
  row (preserved legacy id, domain `'variables'`, normalized payload hash,
  `ON CONFLICT (legacy_id) DO NOTHING`) for 7.1's directional drop backstop
  (adversary round 3 APR3-011).
  `tests/storage/test_variable_copy_migration.py` covers all six cases in full,
  restated here rather than referenced, because an expansion agent receives
  only this deliverable section: (1) first run; (2) rerun over already-migrated
  live rows; (3) rerun over already-migrated soft-deleted rows; (4) two
  soft-deleted variable rows sharing a natural key; (5) a pre-existing typed row
  with a divergent payload — loud failure; (6) a pre-existing live typed row
  with the same natural key and payload but a different UUID — loud failure.

**Acceptance:**

- 4.2.1 - One helper feeds all four default-application paths with identical visibility, each path resolving its session's project_id, with project-first global-fallback deduplication. symbol: `load_variable_defaults`. file: `src/gobby/workflows/variable_defaults.py`.
- 4.2.2 - The session-variables TTL cache is keyed by project_id and the variables revision, and invalidates on the variables domain revision. file: `src/gobby/workflows/state_manager.py`.
- 4.2.3 - Variable sync writes typed columns. file: `src/gobby/workflows/sync_variables.py`.
- 4.2.3a - Variable-definition MCP CRUD reads and writes typed columns. file: `src/gobby/mcp_proxy/tools/workflows/_variables.py`.
- 4.2.4 - Copy migration lands 42 variable rows including the normalized source anomaly. test: `tests/storage/test_variable_copy_migration.py`.
- 4.2.5 - The equivalence guard fails when a pre-existing typed variable row diverges from its legacy source. test: `tests/storage/test_variable_copy_migration.py`.
- 4.2.6 - Rerunning the variable copy over already-migrated soft-deleted rows completes without a primary-key abort. test: `tests/storage/test_variable_copy_migration.py::test_rerun_over_soft_deleted_rows`.
- 4.2.7 - Rerunning the variable copy over already-migrated live rows is a clean no-op, and two soft-deleted variable rows sharing a natural key both migrate. test: `tests/storage/test_variable_copy_migration.py::test_rerun_over_live_rows`.
- 4.2.8 - A live typed variable row matching a legacy row on natural key and payload but carrying a different UUID fails the guard loudly. test: `tests/storage/test_variable_copy_migration.py::test_divergent_identity_fails`.
- 4.2.9 - No generic surface can create or mutate a legacy variable row post-cutover: the generic HTTP routes, generic MCP tools, and the import path each reject kind `variable` naming the surviving domain surface. test: `tests/servers/routes/test_workflows.py`. test: `tests/mcp_proxy/tools/test_workflow_crud.py`. test: `tests/workflows/test_imports.py`.
- 4.2.10 - Bundled variable sync reaches the typed table through update_from_sync: a changed template enabled default is adopted on an untouched row and preserved on a pinned row. test: `tests/workflows/test_sync.py`.
- 4.2.11 - The pinned schema root hashes and the release-pinned expected identity match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`. file: `src/gobby/storage/schema_expected_identity.json`.
- 4.2.12 - Alternating sessions across project A, project B, and no-project see exactly their own overrides plus globals on all four application paths, with no cross-project cache leakage. test: `tests/workflows/test_session_defaults.py::test_project_scoped_defaults_isolation`.
- 4.2.13 - The variable copy migration writes one legacy_copy_ledger row per copied source row and reruns keep the copy-time hash. test: `tests/storage/test_variable_copy_migration.py`.
- 4.2.14 - The variable copy migration holds ACCESS EXCLUSIVE on workflow_definitions: a concurrent second-connection variable-row write blocks until the migration commits, and its post-commit landing is the post-copy drift the 7.1 backstop refuses. test: `tests/storage/test_variable_copy_migration.py::test_copy_lock_fences_concurrent_writes`.

### 4.3 Pipelines cutover and copy migration [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `src/gobby/workflows/pipeline_loader.py` (new)
- `src/gobby/workflows/loader.py::*` — scope-reason: module deleted in this task
- `src/gobby/workflows/loader_discovery.py::*` — scope-reason: module deleted in this task
- `src/gobby/workflows/loader_sync.py::*` — scope-reason: slimmed sync-mixin protocol
- `src/gobby/workflows/loader_cache.py::*` — scope-reason: revision-aware cache rewrite
- `src/gobby/workflows/loader_validation.py::*` — scope-reason: retained validation helpers re-homed under PipelineLoader; WorkflowLoader docstring reference removed
- `src/gobby/workflows/dry_run.py::*` — scope-reason: pipeline-only evaluation rewrite
- `src/gobby/workflows/sync_pipelines.py::*` — scope-reason: typed-manager sync with the enabled guard
- `src/gobby/workflows/imports.py::*` — scope-reason: per-kind import dispatch
- `src/gobby/workflows/pipeline_executor_steps.py::*` — scope-reason: loader call rewiring
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py::*` — scope-reason: loader call rewiring
- `src/gobby/mcp_proxy/tools/workflows/_pipeline_discovery.py::*` — scope-reason: loader call rewiring
- `src/gobby/mcp_proxy/tools/workflows/_pipelines.py::*` — scope-reason: loader call rewiring and pipeline CRUD cutover off the generic definitions helpers (adversary round 3 APR3-005)
- `src/gobby/mcp_proxy/tools/workflows/_query.py::*` — scope-reason: loader call rewiring
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: spawn workflow param to pipeline lookup
- `src/gobby/servers/routes/pipelines.py::*` — scope-reason: loader call rewiring
- `src/gobby/scheduler/executor.py::*` — scope-reason: loader call rewiring
- `src/gobby/dispatch/stage_pipeline.py::*` — scope-reason: loader call rewiring
- `src/gobby/cli/pipelines_catalog.py::*` — scope-reason: loader call rewiring
- `src/gobby/agents/dry_run.py::*` — scope-reason: loader call rewiring
- `src/gobby/hooks/factory.py::*` — scope-reason: PipelineLoader construction retype
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: PipelineLoader construction retype
- `src/gobby/runner.py::*` — scope-reason: PipelineLoader construction retype
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: PipelineLoader construction retype
- `src/gobby/app_context.py::*` — scope-reason: PipelineLoader construction retype
- `src/gobby/mcp_proxy/tools/agents_context.py::*` — scope-reason: PipelineLoader annotation retype
- `src/gobby/mcp_proxy/tools/agents_registry.py::*` — scope-reason: PipelineLoader annotation retype
- `src/gobby/cli/workflows/common.py::*` — scope-reason: CLI factory retype until its 6.1 deletion
- `src/gobby/mcp_proxy/tools/workflows/__init__.py::*` — scope-reason: PipelineLoader annotation retype
- `src/gobby/mcp_proxy/tools/workflows/_definitions.py::*` — scope-reason: PipelineLoader annotation retype
- `src/gobby/mcp_proxy/tools/workflows/_import.py::*` — scope-reason: PipelineLoader annotation retype
- `crates/gcore/assets/schema/migrations/NNN_copy_pipeline_definitions.sql` (new)
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: MIGRATIONS entry registration per 1.5
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pinned root hashes re-armed for the added MIGRATIONS entry
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated release-pinned identity for the added MIGRATIONS entry
- `src/gobby/servers/routes/workflows.py::*` — scope-reason: pipeline-kind rejection on the generic router
- `src/gobby/mcp_proxy/tools/workflows/_definitions.py::*` — scope-reason: pipeline-kind rejection on the generic MCP tools
- `tests/servers/routes/test_workflows.py::*` — scope-reason: pipeline-kind rejection cases
- `tests/mcp_proxy/tools/test_workflow_crud.py::*` — scope-reason: pipeline-kind rejection cases
- `tests/workflows/test_sync.py::*` — scope-reason: typed-manager retarget of the pipeline sync suite
- `tests/workflows/test_imports.py::*` — scope-reason: per-kind typed dispatch retarget
- `tests/workflows/conftest.py::*` — scope-reason: db_loader fixture retarget to PipelineLoader
- `tests/workflows/test_loader.py::*` — scope-reason: comprehensive WorkflowLoader suite absorbed into test_pipeline_loader.py
- `tests/workflows/test_loader_pipeline.py::*` — scope-reason: loader-validation suite absorbed into test_pipeline_loader.py
- `tests/workflows/test_loader_overrides.py::*` — scope-reason: override-behavior retarget onto PipelineLoader
- `tests/workflows/test_workflow_variables.py::*` — scope-reason: db_loader fixture consumer retarget
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: WorkflowLoader patch-path retarget
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: WorkflowLoader patch-path retarget
- `tests/workflows/test_workflow_hooks.py::*` — scope-reason: hooks-factory WorkflowLoader patch-path retarget
- `tests/mcp_proxy/tools/workflows/test_get_workflow_not_found.py::*` — scope-reason: loader-fixture retype ahead of its 5.2 deletion
- `tests/mcp_proxy/tools/workflows/test_import.py::*` — scope-reason: loader-fixture retype ahead of its 5.2 retarget
- `tests/mcp_proxy/tools/workflows/test_project_scope.py::*` — scope-reason: loader-fixture retype ahead of its 5.2 retarget
- `tests/storage/test_pipeline_copy_migration.py` (new)
- `tests/workflows/test_pipeline_loader.py` (new)
- `tests/mcp_proxy/tools/workflows/test_pipeline_crud.py` (new)

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
  existing then.
- **Helper and test-seam closure (adversary BR-003)**, same commit:
  `loader_validation.py` is retained — it is self-contained validation logic
  with no WorkflowLoader dependency — imported by `pipeline_loader.py`, with
  its "for WorkflowLoader" docstring reworded. The loader test seams each get
  an explicit disposition: the `db_loader` fixture in
  `tests/workflows/conftest.py` constructs `PipelineLoader`;
  `tests/workflows/test_loader.py` (the comprehensive WorkflowLoader suite)
  and `tests/workflows/test_loader_pipeline.py` (the loader-validation suite)
  are absorbed into the new `tests/workflows/test_pipeline_loader.py` with
  their behavior assertions preserved; `tests/workflows/test_loader_overrides.py`
  retargets its override cases onto `PipelineLoader`;
  `tests/workflows/test_workflow_variables.py` follows the retargeted
  `db_loader` fixture; `tests/mcp_proxy/tools/spawn_agent/test_factory.py` and
  `test_initial_variables.py` re-point their
  `gobby.workflows.loader.WorkflowLoader` patch paths;
  `tests/workflows/test_workflow_hooks.py` re-points its two
  `gobby.hooks.factory.WorkflowLoader` patches (`:92`, `:137`) onto the
  PipelineLoader seam in the same commit as the factory retype (adversary
  round 2 APR2-010); and the three
  workflows-tool suites (`test_get_workflow_not_found.py`, `test_import.py`,
  `test_project_scope.py`) retype their loader fixtures here ahead of their
  5.2 deletion/retarget, so the tree's tests import cleanly at this commit.
- Rewire callers: `pipeline_executor_steps.py:188,284`,
  `_pipeline_execution.py:334,428,659`, `_pipeline_discovery.py:27`,
  `routes/pipelines.py:299`, `scheduler/executor.py:379`,
  `dispatch/stage_pipeline.py:81`, `cli/pipelines_catalog.py:32,72,163`,
  `_pipelines.py:174,537-579` (dynamic `pipeline:<name>` tools),
  `_query.py:44`, `_factory.py:411-426` (spawn `workflow` param → pipeline
  lookup only), `agents/dry_run.py:181`.
- **Pipeline MCP CRUD cutover (adversary round 3 APR3-005, same commit)**:
  `_pipelines.py` imports `create/update/delete/export_workflow_definition`
  and `_resolve_definition` from the generic `_definitions.py` module
  (`_pipelines.py:13-19`) and `LocalWorkflowDefinitionManager`
  (`:35,140`) for its `create_pipeline`/`update_pipeline`/`delete_pipeline`/
  `export_pipeline` handlers — and 5.2 deletes `_definitions.py`, which would
  strand those imports. This task moves the four handlers and
  `_require_pipeline`/`_resolve_definition` resolution onto
  `PipelineDefinitionManager`, preserving the auto-export/auto-delete
  behavior the generic helpers performed internally
  (`_definitions.py:114-116,221-223,287`) by calling the `_auto_export.py`
  helpers directly from the pipeline handlers (5.2 then makes the kind
  argument explicit). After this commit `_pipelines.py` imports nothing from
  `_definitions.py` and no legacy manager. Focused CRUD coverage lands in
  `tests/mcp_proxy/tools/workflows/test_pipeline_crud.py`.
- `dry_run.py`: `evaluate_workflow` → `evaluate_pipeline_definition`; the
  step-workflow fallback branch dies (agent steps evaluate only via
  `evaluate_agent_definition`). The `_build_step_trace`/`_build_lifecycle_path`
  extraction into `workflows/dry_run_trace.py` already happened in 2.4; this
  task consumes it and does not repeat it. Retire the now-unowned
  `WorkflowEvaluation.workflow_type` field (`dry_run.py:101,111,189,200,264`) —
  the audit in 7.2 matches that token, and no other deliverable removes it.
- `sync_pipelines.py` → typed manager through `update_from_sync` on refresh
  (root `dev.yaml`/`qa.yaml`/`review.yaml` scan preserved;
  `TestSyncBundledPipelines` in `tests/workflows/test_sync.py` retargets at
  the typed manager). `imports.py::sync_imported_definition` dispatches
  per-kind to typed managers and refuses kind changes by table instead of
  stored type — this replaces the per-kind rejections staged by 2.3, 4.1, and
  4.2, restoring imports for all four kinds through typed managers (the
  reason 4.3 depends on 4.2).
- **Generic-surface shrink for kind `pipeline` (enhancement E3, same
  commit)**: the generic router omits `workflow_type='pipeline'` rows from
  list/get and rejects writes for that kind; the generic MCP tools reject
  kind `pipeline`. Rejections name the pipeline domain MCP tools — the HTTP
  `/api/pipelines/definitions` router arrives in 5.1. With all four kinds
  now rejected, the generic surfaces are exhausted; 5.1 and 5.2 delete them.
- Copy migration: guarded; copy `workflow_type='pipeline'` rows (version,
  canvas_json); dup-check; targetless `ON CONFLICT DO NOTHING` per the
  Constraints conflict-target rule; count validation; the Constraints
  equivalence guard including `target.id = source.id`.
  Inside the existence guard — before the first source read — the migration
  takes `LOCK TABLE workflow_definitions IN ACCESS EXCLUSIVE MODE` per the
  Constraints copy-fence rule (adversary round 4 APR4-008; guard-first
  ordering round 5 APR5-001), so on a live hub the copy, the guard, and the
  ledger hashes read one frozen snapshot with no concurrent pipeline-row
  write torn between them, while a post-7.1 fresh lineage records a
  receipted no-op instead of failing on the lock.
  The migration also writes one `legacy_copy_ledger` row per copied source
  row (preserved legacy id, domain `'pipelines'`, normalized payload hash,
  `ON CONFLICT (legacy_id) DO NOTHING`) for 7.1's directional drop backstop
  (adversary round 3 APR3-011).
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
- 4.3.7 - Pipeline sync and per-kind import dispatch write the typed tables and refuse a kind change by target table; imports work for all four kinds through typed managers. file: `src/gobby/workflows/imports.py`. file: `src/gobby/workflows/sync_pipelines.py`. test: `tests/workflows/test_imports.py`.
- 4.3.13 - No generic surface can create or mutate a legacy pipeline row post-cutover: the generic HTTP routes and generic MCP tools reject kind `pipeline` naming the surviving domain surface. test: `tests/servers/routes/test_workflows.py`. test: `tests/mcp_proxy/tools/test_workflow_crud.py`.
- 4.3.14 - Bundled pipeline sync reaches the typed table through update_from_sync: a changed template enabled default is adopted on an untouched row and preserved on a pinned row. test: `tests/workflows/test_sync.py`.
- 4.3.15 - loader_validation.py survives with no WorkflowLoader reference, and every former loader test seam is absorbed, retargeted, or retyped per the closure inventory, including the hooks-factory patch sites. file: `src/gobby/workflows/loader_validation.py`. test: `tests/workflows/test_pipeline_loader.py`. test: `tests/workflows/test_workflow_hooks.py`.
- 4.3.16 - The pinned schema root hashes and the release-pinned expected identity match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`. file: `src/gobby/storage/schema_expected_identity.json`.
- 4.3.17 - Pipeline MCP create/update/delete/export operate on PipelineDefinitionManager with auto-export preserved, and _pipelines.py imports neither the generic definitions module nor the legacy manager. file: `src/gobby/mcp_proxy/tools/workflows/_pipelines.py`. test: `tests/mcp_proxy/tools/workflows/test_pipeline_crud.py`.
- 4.3.18 - The pipeline copy migration writes one legacy_copy_ledger row per copied source row and reruns keep the copy-time hash. test: `tests/storage/test_pipeline_copy_migration.py`.
- 4.3.19 - The pipeline copy migration holds ACCESS EXCLUSIVE on workflow_definitions: a concurrent second-connection pipeline-row write blocks until the migration commits, and its post-commit landing is the post-copy drift the 7.1 backstop refuses. test: `tests/storage/test_pipeline_copy_migration.py::test_copy_lock_fences_concurrent_writes`.

## P5: HTTP and MCP Surfaces
`kind: framing`

**Goal**: the generic `/api/workflows` and generic MCP definition CRUD are
gone; domain surfaces exist for everything the UI and agents need.

### 5.1 HTTP surface rebuild [category: code] (depends: P3, P4)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/workflows.py::*` — scope-reason: generic router deleted in this task
- `src/gobby/servers/routes/pipeline_definitions.py` (new)
- `src/gobby/servers/routes/variable_definitions.py` (new)
- `src/gobby/servers/_app_routes.py::*` — scope-reason: router registration changes
- `src/gobby/servers/routes/__init__.py::*` — scope-reason: router exports
- `src/gobby/workflows/template_hashes.py::*` — scope-reason: kind-keyed cache
- `src/gobby/servers/routes/sessions/variables.py` (new)
- `src/gobby/servers/routes/sessions/__init__.py::*` — scope-reason: sessions package registration
- `src/gobby/mcp_proxy/stdio_proxy.py::*` — scope-reason: relocated variable-endpoint client
- `src/gobby/servers/auth_service.py::*` — scope-reason: agent-token capability grants for the relocated session-variable routes (adversary round 3 APR3-016)
- `src/gobby/servers/middleware/project_context.py::*` — scope-reason: route comment update
- `tests/servers/routes/test_pipeline_definitions.py` (new)
- `tests/servers/routes/test_variable_definitions.py` (new)
- `tests/servers/routes/test_session_variables.py` (new)
- `tests/mcp_proxy/test_stdio_proxy.py::*` — scope-reason: client-seam literal retarget
- `tests/mcp_proxy/test_mcp_proxy_stdio.py::*` — scope-reason: DaemonProxy integration seam retarget (adversary round 3 APR3-016)
- `tests/servers/test_auth_service.py::*` — scope-reason: capability-matrix cases for the relocated routes
- `tests/servers/test_auth_middleware.py::*` — scope-reason: agent-token route authorization cases
- `tests/servers/routes/test_workflows.py::*` — scope-reason: suite deleted in this task
- `tests/servers/test_workflow_routes.py::*` — scope-reason: second generic-routes suite deleted in this task
- `src/gobby/workflows/workflow_templates.py::*` — scope-reason: module orphaned by the router deletion, deleted here (adversary round 4 APR4-004)
- `tests/workflows/test_workflow_templates.py::*` — scope-reason: deleted module's suite, deleted in the same commit (adversary round 4 APR4-004)

- DELETE the 16-route generic router and its registration. By this commit the
  router is exhausted: every kind is already rejected by the staged E3 shrinks
  (agent in 2.3, rule in 4.1, variable in 4.2, pipeline in 4.3), so deletion
  removes only rejection stubs and the omitted-kind list/get shell.
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
  (the `/api/workflows/variables/set|get` literals at `:500` and `:514`), not
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
- **Agent-token capability matrix moves with the routes (adversary round 3
  APR3-016, same commit)**: `_AGENT_CAPABILITY_MATRIX`
  (`auth_service.py:62-99`) grants agent tokens exactly
  `POST /api/workflows/variables/get|set` (`:74-75`) and nothing under
  `/api/sessions/`, so relocating the endpoints without retargeting the
  grants leaves the authenticated `DaemonProxy` client unauthorized even
  while the route suite and the client suite both pass in isolation. Replace
  the two grants with the parameterized
  `POST /api/sessions/*/variables/get|set` entries — the matcher's
  one-segment `*` wildcard (`auth_service.py:56-57,112-122`) covers the path
  parameter — preserving the session-identity binding flag so an agent token
  still reaches only its own session. The matrix cases in
  `tests/servers/test_auth_service.py`/`test_auth_middleware.py` and the
  end-to-end DaemonProxy seam in `tests/mcp_proxy/test_mcp_proxy_stdio.py`
  retarget in the same commit.
- **Deleted-surface test closure**: deleting the router in this commit breaks
  `tests/servers/routes/test_workflows.py`, which exercises all sixteen routes
  (`/api/workflows` literals at `:77` through `:400`). It is deleted here, not
  in P7. Deferring it would leave the phase's own prescribed focused test run
  red for two phases, which contradicts the working-daemon-per-commit rule.
  Behavior worth keeping — the variables get/set cases at `:379-400` — moves
  into `test_session_variables.py` rather than being dropped. The second
  generic suite, `tests/servers/test_workflow_routes.py` at the servers root,
  exercises the same deleted routes through the legacy manager and is deleted
  in the same commit; its CRUD coverage already exists per-domain in the
  domain route suites.
- **Orphaned template module deleted with its router (adversary round 4
  APR4-004)**: the generic router's `GET /templates` handler
  (`workflows.py:102-108`, function-local import) is the sole production
  consumer of `src/gobby/workflows/workflow_templates.py` — four hardcoded
  `workflow_type`-keyed template dicts for the old "New" button. The domain
  routers serve their own template surfaces (pipeline `GET /templates` and
  restore-from-template through the re-keyed `template_hashes.py` loaders),
  so the module and its ten-test suite
  `tests/workflows/test_workflow_templates.py` are deleted in this commit;
  leaving them orphans an unimportable-only module whose five
  `workflow_type` occurrences fail 7.2's production-token audit with no
  owner.
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
- 5.1.8 - Both generic workflows route suites are deleted in this commit and the variables get/set coverage survives under the sessions API. test: `tests/servers/routes/test_workflows.py`. test: `tests/servers/test_workflow_routes.py`. test: `tests/servers/routes/test_session_variables.py`.
- 5.1.9 - An agent token authorizes the relocated session-variable routes for its own session only, the old workflow-variable grants are gone, and the authenticated DaemonProxy round trip passes at the integration seam. file: `src/gobby/servers/auth_service.py`. test: `tests/servers/test_auth_service.py`. test: `tests/mcp_proxy/test_mcp_proxy_stdio.py`.
- 5.1.10 - workflow_templates.py and its suite are deleted with the generic router and nothing in the tree imports either. file: `src/gobby/workflows/workflow_templates.py`. test: `tests/workflows/test_workflow_templates.py`.

### 5.2 MCP surface prune and re-scope [category: code] (depends: P3, P4)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/workflows/__init__.py::*` — scope-reason: registry disposition rewrite
- `src/gobby/mcp_proxy/tools/workflows/_definitions.py::*` — scope-reason: module deleted in this task
- `src/gobby/mcp_proxy/tools/workflows/_query.py::*` — scope-reason: get_step_status re-scope
- `src/gobby/mcp_proxy/tools/workflows/_import.py::*` — scope-reason: reload_cache over the sync registry
- `src/gobby/mcp_proxy/tools/workflows/_auto_export.py::*` — scope-reason: explicit-kind dispatch
- `src/gobby/mcp_proxy/tools/workflows/_agents.py::*` — scope-reason: explicit-kind auto-export caller
- `src/gobby/mcp_proxy/tools/workflows/_rules.py::*` — scope-reason: explicit-kind auto-export caller
- `src/gobby/mcp_proxy/tools/workflows/_variables.py::*` — scope-reason: explicit-kind auto-export caller
- `src/gobby/mcp_proxy/tools/workflows/_pipelines.py::*` — scope-reason: fourth explicit-kind auto-export caller after 4.3's CRUD cutover (adversary round 3 APR3-005)
- `src/gobby/dispatch/prompts.py::*` — scope-reason: dispatch prompt names the renamed status tool (adversary round 3 APR3-017)
- `src/gobby/install/shared/workflows/agents/doc-reviewer.yaml::*` — scope-reason: agent instructions name the renamed status tool (adversary round 3 APR3-017)
- `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml::*` — scope-reason: agent instructions name the renamed status tool (adversary round 3 APR3-017)
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated hashes for the rewritten agent YAMLs
- `src/gobby/sync_registry.py` (new)
- `src/gobby/cli/installers/shared.py::*` — scope-reason: fan-out extraction and delegation
- `src/gobby/cli/install_setup.py::*` — scope-reason: sync-registry consumer
- `src/gobby/cli/sync.py::*` — scope-reason: sync-registry consumer
- `src/gobby/runner_init/storage.py::*` — scope-reason: sync-registry consumer
- `tests/mcp_proxy/tools/test_workflow_crud.py::*` — scope-reason: suite deleted in this task
- `tests/mcp_proxy/tools/workflows/test_import.py::*` — scope-reason: per-kind import retarget
- `tests/mcp_proxy/tools/workflows/test_project_scope.py::*` — scope-reason: domain-surface retarget
- `tests/mcp_proxy/tools/workflows/test_query.py::*` — scope-reason: get_step_status retarget
- `tests/mcp_proxy/tools/workflows/test_get_workflow_not_found.py::*` — scope-reason: suite deleted with the generic get_workflow tool
- `tests/dispatch/test_prompts.py::*` — scope-reason: prompt regression names the renamed tool (adversary round 3 APR3-017)
- `tests/agents/test_qa_reviewer_definition.py::*` — scope-reason: instruction assertions name the renamed tool (adversary round 3 APR3-017)
- `tests/events/test_mcp_tool_changes.py::*` — scope-reason: registry inventory drops list_workflows and tracks the rename (adversary round 3 APR3-017)
- `tests/e2e/test_parallel_clones.py::*` — scope-reason: E2E tool inventory and call retarget (adversary round 3 APR3-017)
- `tests/e2e/test_sequential_review_loop.py::*` — scope-reason: E2E status-tool call retarget (adversary round 3 APR3-017)
- `tests/mcp_proxy/tools/workflows/test_registry_surface.py` (new)

Server name stays `gobby-workflows` (bundled pipelines/skills reference it).
Disposition: DELETE `create_workflow`, `update_workflow`, `delete_workflow`,
`export_workflow`, `restore_workflow`, `get_workflow`, `list_workflows`,
`import_workflow` (domain CRUD already exists for all four kinds; by this
commit every kind is already rejected by the staged E3 shrinks, so the
deleted tools are rejection stubs). RE-SCOPE
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
  explicit `kind: Literal["rule","variable","agent","pipeline"]` from each of
  the four domain callers — `_agents.py`, `_rules.py`, `_variables.py`, and
  `_pipelines.py` (whose direct auto-export calls arrived with 4.3's CRUD
  cutover); `has_gobby_name_collision` becomes a per-domain manager query.
- **Renamed/removed tool consumer closure (adversary round 3 APR3-017), same
  commit as the disposition**: the rename `get_workflow_status` →
  `get_step_status` breaks every consumer that names the old tool, and the
  `list_workflows` deletion breaks every inventory that asserts it. The
  executable consumers move here atomically: the dispatch prompt instruction
  at `dispatch/prompts.py:244` (pinned by `tests/dispatch/test_prompts.py:99`),
  the bundled agent instructions in `doc-reviewer.yaml` (`:73,:121`) and
  `qa-reviewer.yaml` (`:83,:102,:151,:186`; pinned by
  `tests/agents/test_qa_reviewer_definition.py:148,:158`) with the bundled
  content manifest regenerated in the same commit, the registry inventory
  assertion `tests/events/test_mcp_tool_changes.py:109`, and the E2E
  inventory and calls in `tests/e2e/test_parallel_clones.py:124,:574` and
  `tests/e2e/test_sequential_review_loop.py:689`. Documentation references
  (`docs/guides/mcp-tools.md`, `docs/guides/variables.md`) follow in 7.3.
  After this commit no production, bundled, or test reference to
  `list_workflows` or `get_workflow_status` remains outside 7.3's doc sweep.
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
  to pipeline discovery; `workflows/test_get_workflow_not_found.py` exists
  solely to pin `get_workflow` not-found responses, so it is deleted with its
  tool. Retarget preserves domain behavior; only assertions
  that a deleted generic tool exists are dropped.

**Acceptance:**

- 5.2.1 - Generic definition CRUD tools are gone from the registry; domain tools remain. file: `src/gobby/mcp_proxy/tools/workflows/__init__.py`.
- 5.2.2 - evaluate_pipeline and evaluate_agent expose the complete dry-run story. test: `tests/mcp_proxy/tools/workflows/test_registry_surface.py::test_evaluate_tools_cover_pipeline_and_agent`.
- 5.2.3 - get_step_status is registered under its new name and reports the snapshot step list for a session. symbol: `get_step_status`. file: `src/gobby/mcp_proxy/tools/workflows/_query.py`.
- 5.2.4 - One sync registry feeds install, reload_cache, and CLI sync. symbol: `sync_bundled_content_to_db`. file: `src/gobby/sync_registry.py`.
- 5.2.5 - Auto-export dispatches on explicit kind with per-domain collision checks. file: `src/gobby/mcp_proxy/tools/workflows/_auto_export.py`.
- 5.2.5a - Every auto-export caller passes its kind explicitly. file: `src/gobby/mcp_proxy/tools/workflows/_agents.py`. file: `src/gobby/mcp_proxy/tools/workflows/_rules.py`. file: `src/gobby/mcp_proxy/tools/workflows/_variables.py`. file: `src/gobby/mcp_proxy/tools/workflows/_pipelines.py`.
- 5.2.6 - Registry tool inventory and schemas match the disposition table. test: `tests/mcp_proxy/tools/workflows/test_registry_surface.py`.
- 5.2.7 - The generic-CRUD and get_workflow not-found suites are deleted and the import, project-scope, and query suites are retargeted at surviving tools with their domain assertions intact. test: `tests/mcp_proxy/tools/test_workflow_crud.py`. test: `tests/mcp_proxy/tools/workflows/test_get_workflow_not_found.py`. test: `tests/mcp_proxy/tools/workflows/test_import.py`. test: `tests/mcp_proxy/tools/workflows/test_project_scope.py`. test: `tests/mcp_proxy/tools/workflows/test_query.py`.
- 5.2.8 - The dispatch prompt and bundled agent instructions name get_step_status, the regenerated bundled content manifest passes its freshness test, and the prompt and definition regressions pin the rename. file: `src/gobby/dispatch/prompts.py`. file: `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml`. test: `tests/dispatch/test_prompts.py`. test: `tests/agents/test_qa_reviewer_definition.py`.
- 5.2.9 - The registry-inventory and E2E suites assert the final tool set: no list_workflows, get_step_status present and callable. test: `tests/events/test_mcp_tool_changes.py`. test: `tests/e2e/test_parallel_clones.py`. test: `tests/e2e/test_sequential_review_loop.py`.

## P6: CLI and Web UI
`kind: framing`

**Goal**: user-facing surfaces speak domains; no `workflow_type` in public
vocabulary.

### 6.1 CLI restructure [category: code] (depends: P5)
`kind: deliverable`

Targets:
- `src/gobby/cli/workflows/__init__.py::*` — scope-reason: package deleted in this task
- `src/gobby/cli/workflows/manage.py::*` — scope-reason: package deleted in this task
- `src/gobby/cli/workflows/inspect.py::*` — scope-reason: package deleted in this task
- `src/gobby/cli/workflows/check.py::*` — scope-reason: package deleted in this task
- `src/gobby/cli/workflows/variables.py::*` — scope-reason: package deleted in this task
- `src/gobby/cli/workflows/common.py::*` — scope-reason: package deleted in this task
- `src/gobby/cli/__init__.py::*` — scope-reason: command-group re-registration
- `src/gobby/cli/pipelines.py::*` — scope-reason: cli.workflows.common import rewire before the package deletion (adversary round 3 APR3-006)
- `src/gobby/cli/agents.py::*` — scope-reason: registration surface for the extracted subcommand module
- `src/gobby/cli/agents_steps.py` (new)
- `src/gobby/cli/pipelines_catalog.py::*` — scope-reason: show/check subcommands
- `src/gobby/cli/sync.py::*` — scope-reason: per-domain reinstall
- `src/gobby/cli/variables.py` (new)
- `src/gobby/workflows/imports.py::*` — scope-reason: per-kind directory globs
- `src/gobby/mcp_proxy/tools/workflows/_import.py::*` — scope-reason: import-path alignment
- `tests/cli/test_agents_steps.py` (new)
- `tests/cli/test_cli_workflows.py::*` — scope-reason: suite deleted with the package (adversary round 3 APR3-006)
- `tests/cli/test_workflows.py::*` — scope-reason: suite deleted with the package (adversary round 3 APR3-006)
- `tests/cli/test_workflows_coverage.py::*` — scope-reason: suite deleted with the package (adversary round 3 APR3-006)
- `tests/cli/test_pipelines_coverage.py::*` — scope-reason: cli.workflows.common patch paths re-pointed at the rewired home (adversary round 3 APR3-006)

- DELETE the `gobby workflows` group (list/show/status/check/audit/import/
  reload/reinstall — including the raw `DELETE FROM workflow_definitions`
  statements and the legacy `"workflow"→pipelines` alias map in
  `manage.py:93-99`). Replacements: `gobby agents steps [--session REF]`
  (reads `agent_step_instances`), `gobby agents check <name>` (wraps
  `evaluate_agent_definition`), `gobby pipelines show <name>` and
  `gobby pipelines check <name>` (catalog + `evaluate_pipeline_definition`),
  `gobby sync --reinstall [rules|agents|pipelines|variables|all]` (typed
  managers via the sync registry). The new agent subcommands land
  **unconditionally** in a new `src/gobby/cli/agents_steps.py` registered from
  `cli/agents.py` (adversary round 2 APR2-011): `cli/agents.py` is already at
  861 lines, so adding command surface in place projects across the cap, and
  Constraints bans conditional extraction — the destination module is part of
  this deliverable's contract, not an implementation-time judgment call.
- No export/import work: the root `gobby export`/`gobby import` CLI no longer
  exists — its module path sits in `DEAD_PYTHON_PATHS` within
  `tests/meta/test_import_hygiene.py`, and
  `tests/cli/test_export_import.py::test_export_import_commands_are_absent_from_root_help`
  pins the commands' absence from root help; both contracts stay untouched.
  This epic does not resurrect that surface; the earlier draft's vocabulary
  migration for it is moot (adversary BR-008).
- `cli/workflows/variables.py` (set-var/get-var) → `gobby variables get|set
  --session` in new `cli/variables.py` with the scope model.
- **Package-deletion closure (adversary round 3 APR3-006)**: the deletion is
  not confined to the group's own modules. `src/gobby/cli/pipelines.py`
  imports `get_project_path` and `get_workflow_loader` from
  `cli.workflows.common` at module scope (`pipelines.py:30-31`), and the
  package `__init__` eagerly imports its submodules, so deleting the package
  breaks `gobby pipelines` at import time. Re-home `get_project_path` into
  `cli/pipelines.py` (its only surviving consumer) and replace
  `get_workflow_loader` with the `PipelineLoader` factory 4.3 retyped, in the
  same commit as the deletion. Test dispositions, same commit:
  `tests/cli/test_cli_workflows.py`, `tests/cli/test_workflows.py`, and
  `tests/cli/test_workflows_coverage.py` import the package at module scope
  and exist to test the deleted group — they are deleted with it (surviving
  behavior is covered by `tests/cli/test_agents_steps.py` and the pipelines
  suites); `tests/cli/test_pipelines_coverage.py` re-points its two
  `@patch("gobby.cli.workflows.common.Path")` sites (`:52,:62`) at the
  re-homed helper. `tests/cli/test_pipelines.py` holds no `cli.workflows`
  reference (verified) and needs no edit — recorded so the closure is
  deliberate, not an omission.
- `imports.py::sync_imported_workflows`: glob per-kind subdirectories
  `.gobby/workflows/{rules,agents,pipelines,variables}/*.yaml` (symmetry with
  auto-export); drop the root-only glob.

**Acceptance:**

- 6.1.1 - The gobby workflows group is gone and per-domain replacements exist. file: `src/gobby/cli/__init__.py`.
- 6.1.2 - Reinstall runs per-domain through the sync registry with no raw legacy SQL. file: `src/gobby/cli/sync.py`.
- 6.1.4 - Filesystem imports cover the per-kind directories. symbol: `sync_imported_workflows`. file: `src/gobby/workflows/imports.py`.
- 6.1.5 - New CLI subcommands are covered by focused tests. test: `tests/cli/test_agents_steps.py`.
- 6.1.6 - `gobby variables get|set --session` reads and writes both scopes and replaces the deleted set-var/get-var commands. file: `src/gobby/cli/variables.py`.
- 6.1.7 - The new agent subcommands live in the extracted module and both it and the registration surface stay below 1,000 lines. file: `src/gobby/cli/agents_steps.py`. file: `src/gobby/cli/agents.py`.
- 6.1.8 - `gobby pipelines` imports cleanly after the package deletion: no production module imports gobby.cli.workflows, and the re-homed helpers serve the pipelines CLI. file: `src/gobby/cli/pipelines.py`.
- 6.1.9 - The three workflows-group suites are deleted with the package and the pipelines-coverage patch paths point at the re-homed helper. test: `tests/cli/test_cli_workflows.py`. test: `tests/cli/test_workflows.py`. test: `tests/cli/test_workflows_coverage.py`. test: `tests/cli/test_pipelines_coverage.py`.

### 6.2 Web UI migration [category: code] (depends: P5)
`kind: deliverable`

Targets:
- `web/src/hooks/useWorkflows.ts::*` — scope-reason: hook deleted in this task
- `web/src/hooks/usePipelineDefs.ts` (new)
- `web/src/hooks/useVariableDefs.ts` (new)
- `web/src/components/activity/pipelines/PipelinesDefsActions.ts::*` — scope-reason: fetch layer onto domain endpoints
- `web/src/components/activity/pipelines/PipelineEditor.types.ts::*` — scope-reason: PipelineDefDetail types
- `web/src/components/settings/WorkflowVariablesEditor.tsx::*` — scope-reason: renamed onto useVariableDefs
- `web/src/components/settings/workflowVariables.ts::*` — scope-reason: display helper retyped off the definition_json payload (adversary round 3 APR3-018)
- `web/src/components/settings/VariableDefaultsEditor.tsx` (new name)
- `web/src/components/settings/sections/AutomationWorkflowsSection.tsx::*` — scope-reason: section reference updates
- `web/src/components/activity/agents/AgentsTabData.ts::*` — scope-reason: pipeline picker endpoint and data.definitions fix; AgentDefInfo/AgentDraft/agentToDraft onto nested step_workflow (adversary round 4 APR4-002)
- `web/src/components/agents/AgentEditForm.types.ts::*` — scope-reason: AgentItemForPanel definition types onto nested step_workflow (adversary round 4 APR4-002)
- `web/src/components/agents/AgentReadOnlyDetails.tsx::*` — scope-reason: read-only step rendering onto nested step_workflow (adversary round 4 APR4-002)
- `web/src/components/activity/agents/AgentsTabActions.ts::*` — scope-reason: create/update/duplicate request bodies write nested step_workflow (adversary round 4 APR4-002)
- `web/src/components/activity/pipelines/__tests__/PipelinesDefs.test.tsx::*` — scope-reason: domain-endpoint retarget of the pipeline-defs suite
- `web/src/components/activity/pipelines/__tests__/PipelineEditor.test.tsx::*` — scope-reason: PipelineDefDetail type retarget
- `web/src/components/settings/__tests__/WorkflowVariablesEditor.test.tsx::*` — scope-reason: renamed with its component onto useVariableDefs
- `web/src/components/settings/sections/__tests__/AutomationWorkflowsSection.test.tsx::*` — scope-reason: section-reference retarget
- `web/src/components/activity/__tests__/AgentsTab.test.tsx::*` — scope-reason: pipeline-picker endpoint and data.definitions assertions
- `web/src/components/agents/__tests__/AgentEditors.test.tsx::*` — scope-reason: read-only panel fixture gains the nested step fields (adversary round 4 APR4-002)
- `web/src/components/activity/__tests__/AgentsTabActions.test.ts::*` — scope-reason: adapter fixtures and body assertions onto nested step_workflow (adversary round 4 APR4-002)
- `web/src/hooks/__tests__/useFilteredRefetches.test.ts::*` — scope-reason: refetch-filter suite onto the new hooks
- `web/src/hooks/__tests__/useSelectionFetchRaces.test.ts::*` — scope-reason: selection-race suite onto the new hooks
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: network-capture fake taught the domain routes (adversary round 3 APR3-018)

- Replace `useWorkflows.ts` with `usePipelineDefs.ts`
  (`/api/pipelines/definitions`, `WorkflowDetail` → `PipelineDefDetail`, no
  `workflow_type`) and `useVariableDefs.ts` (`/api/variables`).
- `PipelinesDefsActions.ts` + editors → new endpoints/types.
- `WorkflowVariablesEditor.tsx` → `VariableDefaultsEditor.tsx` on
  `useVariableDefs`; update section references. Its extracted display helper
  `workflowVariables.ts::variableDisplayValue` parses the legacy
  `definition_json` string payload and reads `.value` (`:19-22`) — the new
  `/api/variables` shape exposes `default_value` directly, so the helper is
  retyped around it (adversary round 3 APR3-018).
- **Visual-coverage fake follows the endpoints (adversary round 3
  APR3-018)**: the Playwright style-surface capture fake serves definitions
  only through `case "/api/workflows"` branching on `workflow_type`
  (`style-surfaces.spec.ts:1233-1241`, fixtures `:394,:407`), so after this
  migration the pipeline and variable editors would render empty in visual
  coverage. Teach the fake `/api/pipelines/definitions` and `/api/variables`
  response shapes and delete its generic-workflow branch in the same commit.
- `AgentsTabData.ts::loadPipelineList` → `/api/pipelines/definitions` and fix
  the latent bug: read `data.definitions` (it reads `data.workflows`, which is
  always undefined, so the agent editor's pipeline picker is empty today).
- **Agent step-shape closure (adversary round 4 APR4-002)**: the final
  `/api/agents/definitions` payload nests step data under
  `step_workflow.{steps, variables, exit_condition}` (2.2), but the web
  layer still models all three as top-level definition fields —
  `AgentItemForPanel.definition` (`AgentEditForm.types.ts:53-55`),
  `AgentDefInfo.definition` (`AgentsTabData.ts:46-48`), and the read-only
  step list (`AgentReadOnlyDetails.tsx:303-312`) — and today's draft
  pipeline silently loses data even pre-migration: `AgentDraft` carries only
  flat `steps`, `agentToDraft` (`AgentsTabData.ts:266`) drops
  `step_variables`/`exit_condition`, and `buildAgentDefinitionBody`
  (`AgentsTabActions.ts:89`) writes only top-level `steps`. Retype both
  definition interfaces around the nested `step_workflow` object, carry all
  three fields through `AgentDraft`/`agentToDraft`/`createAgentDraft`, write
  the nested shape in `buildAgentDefinitionBody` (the
  `buildDuplicateAgentBody` spread follows the definition shape unchanged),
  and render the read-only step list from `step_workflow.steps`. The
  internal `AgentStepsEditor` `WorkflowStep[]` prop shape is UI-local and
  keeps its flat form. The Playwright `AGENT_DEFINITION` fixture
  (`style-surfaces.spec.ts:280-282`) moves to the nested shape with the
  other capture-fake edits. A hydrate→draft→save-body regression asserts
  steps, step variables, and exit condition survive the round trip.
- Retarget tests: `PipelinesDefs.test.tsx`, `PipelineEditor.test.tsx`,
  `WorkflowVariablesEditor.test.tsx`, `AgentsTab.test.tsx`,
  `useFilteredRefetches.test.ts`, `useSelectionFetchRaces.test.ts`,
  `AutomationWorkflowsSection.test.tsx`, `AgentEditors.test.tsx`,
  `AgentsTabActions.test.ts`.

**Acceptance:**

- 6.2.1 - No web code references /api/workflows or workflow_type. file: `web/src/hooks/usePipelineDefs.ts`.
- 6.2.2 - Pipeline definitions UI performs full CRUD against the domain routes. file: `web/src/components/activity/pipelines/PipelinesDefsActions.ts`.
- 6.2.3 - Variable defaults editor works against /api/variables under its new name. file: `web/src/components/settings/VariableDefaultsEditor.tsx`.
- 6.2.4 - The agent editor's pipeline picker is populated (data.definitions bug fixed). file: `web/src/components/activity/agents/AgentsTabData.ts`.
- 6.2.5 - The retargeted pipeline-definition and editor suites pass. test: `web/src/components/activity/pipelines/__tests__/PipelinesDefs.test.tsx`. test: `web/src/components/activity/pipelines/__tests__/PipelineEditor.test.tsx`.
- 6.2.6 - The retargeted settings and agents-tab suites pass. test: `web/src/components/settings/__tests__/WorkflowVariablesEditor.test.tsx`. test: `web/src/components/settings/sections/__tests__/AutomationWorkflowsSection.test.tsx`. test: `web/src/components/activity/__tests__/AgentsTab.test.tsx`.
- 6.2.7 - The refetch and selection-race hook suites pass against the new hooks. test: `web/src/hooks/__tests__/useFilteredRefetches.test.ts`. test: `web/src/hooks/__tests__/useSelectionFetchRaces.test.ts`.
- 6.2.8 - The variable display helper reads default_value, and the style-surface capture fake serves the domain routes with no generic /api/workflows branch, so the migrated editors render populated in visual coverage. file: `web/src/components/settings/workflowVariables.ts`. test: `web/tests/style-surfaces.spec.ts`.
- 6.2.9 - The web definition types and adapters model step data only under the nested step_workflow object, and the read-only panel renders steps from it. file: `web/src/components/agents/AgentEditForm.types.ts`. file: `web/src/components/activity/agents/AgentsTabData.ts`. file: `web/src/components/agents/AgentReadOnlyDetails.tsx`. file: `web/src/components/activity/agents/AgentsTabActions.ts`.
- 6.2.10 - A hydrated agent definition round-trips to a draft and back to a save body with steps, step variables, and exit condition intact, asserted in the retargeted adapter and editor suites. test: `web/src/components/activity/__tests__/AgentsTabActions.test.ts`. test: `web/src/components/agents/__tests__/AgentEditors.test.tsx`.

## P7: Legacy Removal, Audit, Documentation
`kind: framing`

**Goal**: legacy storage is physically gone, a standing audit prevents
regression, docs describe the final state.

### 7.1 Drop migration and legacy module deletion [category: code] (depends: P6)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/NNN_drop_legacy_workflow_tables.sql` (new)
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: MIGRATIONS entry and baseline checksum re-arm
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: refresh contract gains an enumerated removed-statement allowlist
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: removed-statement assertions and destructive-apply coverage
- `crates/gcore/tests/fixtures/schema/predecessor_baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: legacy catalog rows removed
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pinned checksum and root-hash constants
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated release-pinned identity
- `src/gobby/storage/workflow_definitions.py::*` — scope-reason: module deleted in this task
- `src/gobby/storage/definitions/_shared.py`
- `src/gobby/sessions/lifecycle.py::*` — scope-reason: purge fan-out over typed managers
- `src/gobby/workflows/template_hashes.py::*` — scope-reason: legacy import removal
- `src/gobby/storage/skills/_metadata.py::*` — scope-reason: docstring reword
- `tests/storage/test_workflow_definitions.py::*` — scope-reason: comprehensive legacy-manager suite deleted; behavior superseded by the typed-manager suites
- `tests/storage/test_workflow_definitions_rules.py::*` — scope-reason: legacy rule-listing suite deleted; behavior superseded by test_rules_manager.py
- `tests/agents/test_merge_orchestrator_contract.py::*` — scope-reason: legacy fixture-seeding rewrite
- `tests/storage/tasks/test_stage_registry_default_agent_fk.py::*` — scope-reason: legacy fixture-seeding rewrite
- `tests/storage/test_drop_legacy_migration.py` (new)
- `crates/gdaemon/src/main.rs::apply_schema`
- `crates/gdaemon/tests/schema_cli.rs::*` — scope-reason: destructive epoch-fence contract coverage

- The former `workflow_states` slice is gone: the table is already absent from
  the baseline and `get_claimed_task_owners` already reads `tasks JOIN
  sessions` (Epic Review Notes correction 10). Only the two legacy runtime
  tables remain to drop.
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
- **Refresh-contract removal handling**: this is the epic's only baseline edit
  that removes statements, and the set-difference tripwire
  (`baseline_refresh_accepts_exactly_the_predecessor_statement_difference`)
  fails any removal today. Extend `verify_baseline_refresh_contract` with an
  explicit enumerated removed-statement allowlist naming exactly the legacy
  DDL this hop deletes (the three CREATE TABLEs — both legacy tables and
  `legacy_copy_ledger` — their constraints, indexes, FK, and grants) so
  removal is a deliberate, checked property rather than a relaxed assertion. Statement removal never executes anything on a refresh
  hop — existing hubs lose the tables only through the destructive drop
  migration below.
- **Residual audited tokens with no other owner**: `workflows/template_hashes.py:20`
  imports from `storage/workflow_definitions.py` and must lose that import in the
  same commit the module is deleted (5.1 re-keys the cache by `kind`; it does not
  touch the import). `src/gobby/storage/skills/_metadata.py:250` names the
  `workflow_definitions` pattern in a docstring describing installed-copy
  precedence — reword it. Both are matched by 7.2's audit.
- Drop migration (a `-- gobby:destructive` `EmbeddedMigration` per 1.5:
  receipt-stamped without executing on fresh lineages, refused on a plain
  restart of an existing hub, and applied once per hub via `gobby
  hub-maintenance run schema-apply` against a verified backup manifest): the
  destructive path runs only inside an open **maintenance epoch** (adversary
  round 4 APR4-008, narrowed — the write fence already exists in code):
  `gobby schema apply --destructive` refuses without one
  (`cli/schema.py:114-146`), and `gobby hub-maintenance run schema-apply`
  stops the daemon first (`hub_maintenance.py:185-196`), terminates every
  `gobby%` backend and polls to proven quiescence
  (`maintenance_epoch.py:174-271`), and blocks reconnects via the baseline's
  login event trigger (`baseline.sql:4240-4317`). That fence has one
  unfenced entry point today (adversary round 5 APR5-002): the orchestrated
  path itself ends in a shell-out to the raw binary —
  `_apply_verified_batch` binds the epoch GUC into the DSN
  (`bind_maintenance_epoch`, `cli/schema.py:168`) and
  `schema_contract.apply_schema(..., destructive=True)`
  (`schema_contract.py:112-125`) runs `gdaemon schema apply --destructive` —
  but `crates/gdaemon/src/main.rs::apply_schema` authorizes destructive work
  from the backup manifest alone, so a direct operator invocation of that
  same command bypasses daemon stop, backend quiescence, and the reconnect
  fence entirely. This task closes that hole in the gdaemon binary: when
  `--destructive` is set, `apply_schema` additionally requires its own
  connection to carry the maintenance-epoch GUC
  (`current_setting('gobby.maintenance_epoch', true)`) naming the currently
  open maintenance-epoch record, and refuses otherwise with a message
  directing the operator to `gobby hub-maintenance run schema-apply`. The
  orchestrated path already satisfies the check — the GUC rides the bound
  DSN — so no new plumbing is added anywhere else. The backstop preflight
  and the DROPs therefore execute over a hub with no live gobby writer — no
  additional table locking is needed for the check-to-drop gap. **Backstop
  first**, and the backstop is **directional** (adversary round 3 APR3-011,
  replacing the earlier symmetric payload-equivalence design): between each
  domain's copy migration and this drop, the typed tables are the sole
  writable authority, so typed rows legitimately evolve — user edits, sync
  refreshes, restores, hard-deletes — while the legacy rows are supposed to
  stay frozen. Comparing legacy payloads against current typed payloads
  cannot tell safe typed-side evolution from dangerous legacy-side drift and
  would permanently block the drop on any hub that used the system after
  cutover. The backstop therefore proves **the legacy side is unchanged
  since its copy**, using the `legacy_copy_ledger` checkpoint (1.1) the four
  copy migrations wrote: for **every non-generated legacy row, live and
  soft-deleted**, require a ledger entry with the row's preserved `id` AND
  the MD5 of the row's current normalized payload equal to the recorded
  `source_hash`; RAISE with the offending ids and names on a missing entry
  (a row created after its domain's copy ran — including one soft-deleted
  before P5, which is why soft-deleted rows are covered) or a hash mismatch
  (a legacy write after the copy — data the typed tables never saw). Typed
  state is not consulted: typed evolution and deliberate typed deletion
  never block the drop, and a timestamp comparison is not a substitute
  because legacy `updated_at` values are client-stamped and skew across the
  hub's daemons. Exclusion of `workflow_type='workflow'` rows is
  **by proven provenance, never by discriminator alone** (adversary round 2
  APR2-005): a row is generated — and only then excluded — when it matches
  the exact signature `name ~ '-steps$' AND source = 'agent'`, the shape
  `register_agent_step_workflow` writes. The preflight RAISEs with ids and
  names on every `workflow_type='workflow'` row that fails the signature and
  on every row whose `workflow_type` is outside the five known values: a
  standalone user-authored workflow row is unsupported by the split, and the
  epic's fail-loud contract requires the migration to refuse rather than
  silently drop it. Then
  `DROP TABLE workflow_instances`, `DROP TABLE workflow_definitions`,
  `DROP TABLE legacy_copy_ledger` — the ledger exists only to prove this
  drop safe and leaves with the tables it guards. The 3.2 instance
  write-fence trigger goes with its table; its trigger function is dropped
  explicitly here (`DROP FUNCTION IF EXISTS`).
- Remove all three tables' DDL from the baseline (CREATE TABLE, constraints,
  indexes, FK, grants — both legacy tables and `legacy_copy_ledger`) and
  their catalog-manifest rows in the same commit,
  with the full Constraints lockstep re-arm.
- Delete `src/gobby/storage/workflow_definitions.py` (row, manager, global
  revision helpers — `compute_definition_hash` already lives in
  `src/gobby/storage/definitions/_shared.py`). **Legacy-suite disposition
  (adversary BR-002)**: the module's own comprehensive suites are deleted with
  it — `tests/storage/test_workflow_definitions.py`, whose CRUD,
  scope-fallback, conflict, soft-delete/restore, and purge coverage is
  superseded by the per-domain typed-manager suites from 1.2/1.3, and
  `tests/storage/test_workflow_definitions_rules.py`, whose event/group
  listing coverage is superseded by `test_rules_manager.py`. Then sweep
  remaining test imports of the legacy module (~45 files; most already
  rewritten by their domain cutovers in P1–P6 — this task deletes stragglers,
  rewriting tests that still seed `workflow_definitions` fixtures such as
  `tests/agents/test_merge_orchestrator_contract.py:119` and
  `tests/storage/tasks/test_stage_registry_default_agent_fk.py:30`). The
  grep-derived import inventory at this commit must come back empty; 7.2's
  audit pins that property.

**Acceptance:**

- 7.1.1 - The drop is a destructive EmbeddedMigration whose backstop verifies every non-generated legacy row, live and soft-deleted, against its legacy_copy_ledger checkpoint hash and RAISEs with offending ids and names. test: `tests/storage/test_drop_legacy_migration.py`.
- 7.1.2 - The backstop refuses to drop when a legacy row was written after its copy (hash mismatch) and when a legacy row has no ledger entry (post-copy insertion). test: `tests/storage/test_drop_legacy_migration.py`.
- 7.1.2a - The backstop also covers soft-deleted legacy rows by preserved id, refusing to drop a definition created after its copy migration and soft-deleted before P5. test: `tests/storage/test_drop_legacy_migration.py::test_backstop_covers_soft_deleted_rows`.
- 7.1.3 - Both legacy tables and legacy_copy_ledger are gone from the baseline, the catalog manifest, and the live schema after the destructive apply, and the refresh contract enumerates exactly the removed statements. file: `crates/gcore/assets/schema/baseline.sql`. file: `crates/gcore/src/schema/runner_tests.rs`.
- 7.1.4 - storage/workflow_definitions.py is deleted and no source or test imports it. file: `src/gobby/storage/definitions/_shared.py`.
- 7.1.5 - The scheduled soft-deleted-definition purge drops the legacy manager import and fans out over the four typed parent managers, with agent step-workflow children removed by cascade and no step-instance branch. symbol: `_purge_soft_deleted_definitions`. file: `src/gobby/sessions/lifecycle.py`.
- 7.1.6 - A fresh lineage receipt-stamps the drop without executing it; an existing hub refuses it on plain restart and applies it under --destructive with a verified backup manifest inside an open maintenance epoch. file: `crates/gcore/src/schema/runner_tests.rs`. test: `tests/storage/test_drop_legacy_migration.py`.
- 7.1.7 - Template hashing and the skills metadata docstring carry no legacy storage reference. file: `src/gobby/workflows/template_hashes.py`. file: `src/gobby/storage/skills/_metadata.py`.
- 7.1.8 - A signature-matching generated row is excluded from the backstop, while a workflow_type='workflow' row failing the generated signature and a row with an unknown workflow_type each fail the preflight loudly with ids and names. test: `tests/storage/test_drop_legacy_migration.py::test_unsupported_row_classification`.
- 7.1.9 - Typed-side evolution after copy — an edit, a sync refresh, a restore, and a hard-delete of typed rows — does not block the drop, while a legacy-only write and a post-copy legacy insertion each block it loudly. test: `tests/storage/test_drop_legacy_migration.py::test_directional_backstop`.
- 7.1.10 - Replaying every embedded copy migration against a fresh final-baseline lineage, where neither legacy table exists, records receipted no-ops with no error and no typed-row writes. file: `crates/gcore/src/schema/runner_tests.rs`.
- 7.1.11 - gdaemon schema apply --destructive refuses when its connection lacks the open maintenance-epoch GUC, naming gobby hub-maintenance run schema-apply in the refusal, and succeeds over an epoch-bound DSN with a verified backup manifest. symbol: `apply_schema`. file: `crates/gdaemon/src/main.rs`. test: `crates/gdaemon/tests/schema_cli.rs`.

### 7.2 Legacy-reference audit test [category: test] (depends: 7.1, 7.3)
`kind: deliverable`

Target: `tests/audit/test_legacy_workflow_storage_removed.py` (new)

Grep-style pytest failing on word-boundary occurrences of
`workflow_definitions`, `workflow_instances`, `workflow_states`,
`LocalWorkflowDefinitionManager`, `WorkflowDefinitionRow`, `workflow_type`,
`register_agent_step_workflow`, `_step_workflow_name`, `/api/workflows`, and
`-steps` name-derivation patterns. Scan scope (read-only; the audit changes no
scanned file): `src/gobby/**/*.py`, `web/src/**/*.{ts,tsx}`, the live baseline
schema `crates/gcore/assets/schema/baseline.sql` — excluding this epic's
historical migration assets under `crates/gcore/assets/schema/migrations/`,
which legitimately name the dropped tables — and the current bundled
YAML/skill/prompt sources under `src/gobby/install/shared/`. The baseline SQL
and the bundled templates are precisely what 7.1 and 7.3 rewrite, so leaving
them unscanned lets the removal regress exactly where regression is easiest.
`ALLOWLIST` is a list of exact `(path, token, reason)` triples; the audit fails
both when a non-allowlisted occurrence appears **and** when an allowlisted
occurrence no longer exists, so exceptions stay narrow and self-prune instead of
outliving their reason. Additionally assert no bundled agent YAML contains
top-level `steps:`/`step_variables:`/`exit_condition:` keys (guards the
`extra="ignore"` silent-drop trap that 2.2's validator closes at runtime).

The dependency on 7.3 is load-bearing, not bookkeeping: 7.2 scans the bundled
YAML/skill/prompt sources that 7.3 rewrites, so as siblings the audit lands red
after 7.1 completes and phase-by-phase expansion has no valid ordering.

**Owner inventory.** Every token the audit matches must have an upstream
deliverable that removes it or a justified allowlist entry; an audit written
against an unowned occurrence lands red at the end of the epic with no
deliverable left to fix it. The occurrences that are not obviously owned by the
domain cutover that renames them are assigned as follows: the stale
`workflow_states` docstring in `src/gobby/config/tasks.py:611` → 7.3; the
`workflow_definitions` import in
`src/gobby/workflows/template_hashes.py:20` and the `workflow_definitions`
docstring in `src/gobby/storage/skills/_metadata.py:250` → 7.1; the
`workflow_instances` log key in `src/gobby/agents/terminal_cleanup.py:180` →
3.3; the `/api/workflows` comment in
`src/gobby/servers/middleware/project_context.py:10` → 5.1;
`WorkflowEvaluation.workflow_type` in `src/gobby/workflows/dry_run.py:101` → 4.3;
the `WorkflowDefinitionRow` import and annotations in
`src/gobby/workflows/engine/evaluation.py:12,96,207,229` → 4.1; the same import
and the `is_internal_rule` annotation in
`src/gobby/workflows/reserved_variables.py:6,39` → 4.1; the
`workflow_instances_deleted` result key in
`src/gobby/mcp_proxy/tools/agents_termination.py:34` → 3.3 (adversary round 4
APR4-003); the five `workflow_type` occurrences in
`src/gobby/workflows/workflow_templates.py:19,55,94,113,152` → 5.1, which
deletes the module (adversary round 4 APR4-004); and the `workflow_type`
column and `idx_wf_defs_type` index in
`crates/gcore/assets/schema/baseline.sql:2140-2158,3182` → 7.1.

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

Targets:
- `docs/guides/agents.md`
- `docs/guides/rules.md`
- `docs/guides/variables.md`
- `docs/guides/workflows-overview.md`
- `docs/guides/http-endpoints.md`
- `docs/guides/pipelines.md`
- `docs/guides/cli-commands.md`
- `docs/guides/mcp-tools.md`
- `docs/architecture/architecture.md`
- `docs/reviews/cli-build-ops.md`
- `docs/audits/configuration-audit.md`
- `docs/plans/workflow-refactor.md` (deleted)
- `src/gobby/install/shared/workflows/rules/CLAUDE.md`
- `src/gobby/dispatch/CLAUDE.md`
- `src/gobby/config/tasks.py::*` — scope-reason: stale workflow_states docstring removal
- `src/gobby/install/shared/skills/persona/SKILL.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated hashes for the rewritten bundled skills and prompts

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
— including every guide under `docs/guides/` — for every removed route,
command, tool name, table, and discriminator rather than by the
guide list this plan started with. That search adds five artifacts the initial
list missed: `docs/guides/http-endpoints.md` and `docs/guides/pipelines.md` both
document `/api/workflows` or `workflow_type` as current behavior;
`docs/guides/cli-commands.md` presents the entire `gobby workflows` command
group 6.1 deletes (the top-level table row at `:77` and the `### Workflows`
subcommand block at `:589-601`) and is rewritten around the `gobby agents`,
`gobby pipelines`, `gobby variables`, and `gobby sync` replacements
(adversary round 3 APR3-019); `docs/guides/mcp-tools.md` documents the
removed `list_workflows` (`:614`) and renamed `get_workflow_status` (`:624`),
and `docs/guides/variables.md:434` names the old status tool — both retarget
at the 5.2 disposition (adversary round 3 APR3-017); and
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
- 7.3.9 - The CLI guide documents the replacement command surface with no gobby workflows group, and the MCP tools and variables guides name only surviving tools. file: `docs/guides/cli-commands.md`. file: `docs/guides/mcp-tools.md`. file: `docs/guides/variables.md`.
- 7.3.6 - The configuration audit carries an explicit active-or-historical disposition. file: `docs/audits/configuration-audit.md`.
- 7.3.7 - Module guidance describes the domain tables. file: `src/gobby/dispatch/CLAUDE.md`. file: `src/gobby/install/shared/workflows/rules/CLAUDE.md`.
- 7.3.8 - The bundled content manifest is regenerated for every rewritten bundled skill and prompt and its freshness test passes without an update flag. file: `src/gobby/install/bundled_content_manifest.json`. test: `tests/install/test_bundled_content_manifest.py`.

## E1 End-to-End Verification
`kind: verification`

Per-phase: rebuild and reinstall gdaemon when crate schema assets changed,
then `uv run gobby restart` — startup applies the baseline refresh and that
phase's pending non-destructive migrations (the P7 drop instead needs the
`gobby hub-maintenance run schema-apply` orchestrator step from
Constraints); then focused tests only
(`GOBBY_TEST_PROTECT=1 uv run pytest <paths> -v`) — storage
(`tests/storage/definitions/`, per-migration tests), runtime
(`tests/workflows/test_step_instances.py`,
`tests/workflows/test_step_snapshot_semantics.py`), surfaces
(`tests/servers/routes/`, `tests/mcp_proxy/tools/workflows/`), UI (vitest for
the retargeted suites). Epic-final: restore a copy of the live hub into an
isolated database, replay the baseline refresh and every pending
non-destructive migration through `gdaemon schema apply`; for the drop, open
a maintenance epoch on the isolated hub and run the epoch-bound destructive
apply with a verified backup manifest through the `gobby hub-maintenance run
schema-apply` machinery — a raw `--destructive` invocation now refuses
without the epoch GUC (adversary round 5 APR5-002). Confirm **249 definition
rows** land in the typed
tables — 167
rules (162 live + 5 soft-deleted), 29 agents (28 live + 1 soft-deleted),
42 variables, 11 pipelines — plus **25** `agent_step_workflows` children (every
agent row carrying a non-empty `steps` array), for 274 rows total; the 29
generated `workflow_type='workflow'` rows are not copied. Re-derive these five
counts from the restored hub before asserting them: the legacy tables are still
live and writable until P7, so a sync or a retired bundled agent moves them.
Confirm every copied definition row left a `legacy_copy_ledger` checkpoint,
that the directional backstop passes over typed edits made after the copies,
and that the destructive apply drops the ledger with the legacy tables.
Confirm only live-session instances survive; spawn a stepful agent (e.g. planner) and observe step
enforcement, transitions, and the completion gate from the snapshot; edit the
agent definition mid-run and confirm the run is unaffected while a second
spawn picks up the edit; restart the daemon against a session whose instance
was deleted and confirm the structured fresh-snapshot recovery warning appears
in `~/.gobby/logs/` with the session, agent name, and resolved ids; exercise
the pipelines and variables editors in the web UI; run the isolated
two-daemon definition-revision test
(`tests/integration/definitions/test_definition_revisions_multi_daemon.py`)
proving a mutation through daemon A invalidates daemon B without a restart;
run `gobby sync`, `gobby agents steps`, `gobby pipelines check`;
`uv run ruff check src/`, `uv run mypy src/`, and the test-types ratchet must
pass; the P7 audit test is the standing regression gate.

## V1 Plan Changelog

`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: afd50f90-e9e9-4802-8bb8-67a0a7ab6e28
- enhancer_session: 2b57d1d0-211f-49d3-8992-7e90b947856c
- converged: false
- suggestions_presented: 4
- accepted:
  - E1 / better / commit-visible revision contract with dual-domain child bumps
  - E2 / better / parameterized enabled reconciliation contract across all four domains
  - E3 / better / staged generic-surface kind rejection at each domain cutover
  - E4 / bigger / persistent revisions with cross-daemon LISTEN/NOTIFY invalidation
- declined:
  - (none)
- resolution_notes: All four suggestions folded in. The operator additionally
  renamed the reconciliation bit `enabled_user_modified` → `enabled_pinned`
  on the typed tables (the legacy column keeps its name until the P7 drop).
  E4 adds the `definition_revisions` table — 1.1 now ships seven tables — and
  a notifications module modeled on `config_notifications.py` with a
  two-daemon integration test. E3 serializes 4.3 after 4.2 because the
  staged shrink shares `routes/workflows.py`, `_definitions.py`, and
  `workflows/imports.py` across the P4 cutovers.

**Round 2** `kind: verification`

- reviewer_run: 038eb7bb-00f8-40c9-85cb-5b007c2c5ed6
- reviewer_session: b40679e5-fd7c-4581-8ccc-4509141525fe
- verdict: needs_review
- findings:
- APR2-001 / blocking / stale #18974 resume exclusion in 3.2-3.4
- APR2-002 / blocking / model-before-YAML cutover not atomic (2.1/2.2)
- APR2-003 / blocking / bundled content manifest unowned by 2.2
- APR2-004 / blocking / bundled agent inventory conflated files with hub rows
- APR2-005 / blocking / workflow_type=workflow exclusion lacked provenance predicate
- APR2-006 / blocking / no .coverage-ledger.yaml companion
- APR2-007 / blocking / 3.2/3.3 acceptance cited a suite 3.4 creates
- APR2-008 / blocking / copy migrations missed schema-identity lockstep targets
- APR2-009 / blocking / WorkflowInstanceManager test seams undispositioned
- APR2-010 / blocking / hooks-factory WorkflowLoader patch sites unowned
- APR2-011 / blocking / 6.1 conditional extraction of 861-line agents.py
- APR2-012 / blocking / agent_name writable through generic save
- APR2-013 / blocking / pre-launch compensation boundaries incomplete
- APR2-014 / blocking / instance copy candidate resolution nondeterministic
- APR2-015 / blocking / revision bump earlier than outermost ambient commit
- resolution_notes: All 15 findings accepted (unattended, operator-delegated)
  and repaired in this revision with whole-plan sweeps per class. 2.1 became
  additive-only and 2.2 the atomic model+YAML cutover owning every reader and
  shape suite; the 3.2-3.4 resume block was rewritten around the completed
  #18974 same-session continuity contract with the daemon_stop retention gate
  carried onto the typed table; schema-identity lockstep targets were added to
  all five migration-registering deliverables; the generated-row signature,
  deterministic instance-copy resolution, agent_name immutability, the
  pre-launch cleanup owner, after-commit revision binding, unconditional
  agents_steps.py extraction, and the full WorkflowInstanceManager /
  WorkflowLoader test-seam dispositions were folded into their owning
  sections; the coverage-ledger companion ships alongside the plan. Two
  narrowed repairs are recorded in the Decision Record (APR2-015 existing
  after_commit seam; APR2-008 not applied to 1.5).

```json plan-review-round
{"evidence_id":"cd52d419-de72-4e44-8517-7840a45f6633","plan_hash":"e46380804ad1a347e413be2c3899fb16562f7c7e47bfaa5fae4669b2a117f4ce","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"0f518d735f7e678f7d900ad08e759e02daad8a51b9cf6e6252e494cc1f7e3238","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":15,"total":19},"evidence_id":"cd52d419-de72-4e44-8517-7840a45f6633","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":23,"manifest_digest":"7db866952dce5da506749eeec432697e001576c9fa803801b9f7521b3b6f1c94","status":"valid"},"source_digest":"0a268a954ae860cbe0b783031f02e4a32dc5b540d4fe450f7c9a42c7e5430b70","version":1},"findings":[{"category":"bad-sequencing","check_key":"daemon-resume-state-continuity","description":"The current runtime resumes prepared same-session agents through resume_executor, spawn, and runtime_cleanup, with tests in tests/agents/test_spawn_prepare_resume.py. The plan's stale exclusion would route the storage split around that live contract and leave resume behavior unowned.","finding_id":"APR2-001","fix":"Rewrite §§ 3.2-3.4 around the completed #18974 seam. Target resume_executor.py, spawn.py, runtime_cleanup.py, and test_spawn_prepare_resume.py; require same-session daemon resume, snapshot persistence, and cleanup behavior to remain intact.","location":"Phase 3 / §§ 3.2-3.4","prevention":"Before finalizing a refreshed plan, re-read every completed prerequisite task and map its current regression tests to the affected deliverables.","principle":"A refreshed plan must build on completed prerequisite contracts and preserve their tested runtime invariants.","root_cause":"Sections 3.2-3.4 still treat same-session daemon resume as excluded even though #18974 completed and established the prepare/resume cleanup seam.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"atomic-schema-shape-cutover","description":"Applying § 2.1 first makes the existing bundled agent YAML incompatible with the new model. The split also leaves raw-definition consumers and tests such as test_agent_definitions.py, test_agent_definitions_v2.py, and test_skill_composition.py outside exact ownership.","finding_id":"APR2-002","fix":"Combine §§ 2.1 and 2.2 into one atomic model-and-YAML cutover. Add every raw reader and named regression file to exact Targets and require the bundled definitions to load successfully in that deliverable's validation.","location":"Phase 2 / §§ 2.1-2.2","prevention":"For every serialized shape change, inventory all producers/readers/tests and prove each ordered commit can load the checked-in artifacts.","principle":"A serialized schema cutover must leave every implementation boundary executable and own every producer, reader, and regression target.","root_cause":"Section 2.1 changes the Definition model before § 2.2 converts bundled YAML, creating an invalid intermediate shape while omitting raw readers and named regression files from exact Targets.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"generated-bundled-manifest-lockstep","description":"The bundled content manifest hashes the agent YAML payload. The planned YAML conversion would leave that generated index stale and fail its repository freshness contract.","finding_id":"APR2-003","fix":"Add src/gobby/install/bundled_content_manifest.json and tests/install/test_bundled_content_manifest.py to § 2.2 Targets. Require manifest regeneration plus a passing freshness test without an update flag.","location":"Phase 2 / § 2.2","prevention":"Whenever bundled install content changes, include its generated manifest and the no-update freshness command in Targets and Acceptance.","principle":"Checked-in bundled-content indexes must be regenerated and freshness-tested in the same deliverable as their source assets.","root_cause":"Section 2.2 changes bundled YAML files without owning src/gobby/install/bundled_content_manifest.json or its freshness test.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"bundled-agent-inventory-parity","description":"The current bundled manifest contains 25 total agent YAML files: 21 stepful and 4 step-less. nightly-linter, nightly-test-fixer, plan-review-researcher-taskless, and wiki-researcher are absent, so the stated conversion set and its acceptance count cannot be satisfied.","finding_id":"APR2-004","fix":"Replace the stale inventory with the verified 25-file total set, remove the four nonexistent names, and state the separate legacy database-row count and reconciliation rule used by § 2.3.","location":"Phase 2 / §§ 2.2-2.3","prevention":"Generate and record the current asset inventory before specifying bulk conversion counts; separately label any source-database counts.","principle":"Migration and conversion inventories must be derived from the current filesystem and kept distinct from legacy database-row counts.","root_cause":"The plan claims 25 stepful plus 4 step-less bundled agents and names four absent files, conflating the 25-file current inventory with a legacy count.","section_id":"2.2","severity":"blocking"},{"category":"missing-requirement","check_key":"unsupported-definition-row-rejection","description":"The epic requires unsupported standalone workflow rows to fail migration. workflow_type alone does not establish generated provenance, so the current exclusion can silently leave unsupported user definitions behind.","finding_id":"APR2-005","fix":"Define the exact generated-workflow signature and project/global lineage rules. Migrate only rows satisfying that signature, make preflight fail on every remaining standalone workflow row, and test generated and unsupported examples.","location":"Phase 2 / § 2.3 and Phase 7 / § 7.1","prevention":"For each excluded migration row class, specify the provenance predicate, preflight failure rule, and positive and negative fixtures.","principle":"A destructive migration must classify supported rows by a proven discriminator and fail on every unsupported row.","root_cause":"The plan excludes all workflow_type=workflow rows without defining evidence that those rows are generated rather than unsupported standalone definitions.","section_id":"7.1","severity":"blocking"},{"category":"missing-requirement","check_key":"coverage-ledger-companion","description":"docs/contracts/plan-coverage.md requires every new epic plan to ship an adversary-reviewed .coverage-ledger.yaml companion. The existing coverage artifacts do not provide that required companion for this plan.","finding_id":"APR2-006","fix":"Add .gobby/plans/split-workflow-definition-storage.coverage-ledger.yaml and populate it with every acceptance item and expected implementation leaf; include ledger validation in § 7.2.","location":"Whole plan / § 7.2","prevention":"Before adversary approval, verify the canonical .gobby/plans/<plan-id>.coverage-ledger.yaml exists and covers every deliverable acceptance item.","principle":"Every new epic plan must include the governing bootstrap coverage ledger before expansion.","root_cause":"The plan has no .coverage-ledger.yaml companion mapping acceptance items to expected implementation leaves.","section_id":"7.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"acceptance-test-producer-order","description":"The Phase 3 ordering makes § 3.2 and § 3.3 validation impossible in isolation because their referenced regression module is a downstream § 3.4 target.","finding_id":"APR2-007","fix":"Move creation and ownership of tests/workflows/test_step_snapshot_semantics.py into § 3.2, make §§ 3.3-3.4 depend on that producer, and update Targets and acceptance commands accordingly.","location":"Phase 3 / §§ 3.2-3.4","prevention":"For each acceptance test path, map its producing deliverable and ensure that producer precedes every consumer.","principle":"A deliverable's acceptance command may depend only on test artifacts that exist when that deliverable executes.","root_cause":"Sections 3.2 and 3.3 invoke tests/workflows/test_step_snapshot_semantics.py, which § 3.4 creates later.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"migration-identity-lockstep","description":"Changing an embedded migration changes the schema root/latest asset identity. Leaving crates/gcore/tests/schema_contract.rs or src/gobby/storage/schema_expected_identity.json outside any one copy step produces an internally inconsistent commit.","finding_id":"APR2-008","fix":"Add crates/gcore/tests/schema_contract.rs and src/gobby/storage/schema_expected_identity.json to every listed copy-migration deliverable, and require the schema-contract and rebuilt-daemon identity checks after each asset replacement.","location":"Phases 1-4 / §§ 1.5, 2.3, 3.2, 4.1, 4.2, 4.3","prevention":"For every EmbeddedMigration Target, require the Rust contract fixture, expected daemon identity, and their freshness/rebuild checks in the same deliverable.","principle":"Every embedded schema migration update must advance all checked-in schema identity locks in the same commit.","root_cause":"The copy-migration deliverables replace EmbeddedMigration assets without consistently targeting schema_contract.rs and schema_expected_identity.json.","section_id":"2.3","severity":"blocking"},{"category":"weak-testability","check_key":"deleted-instance-test-seam-closure","description":"WorkflowInstance and WorkflowInstanceManager remain embedded in tests/workflows/test_instance_manager.py and tests/workflows/test_step_enforcement.py, yet the sections deleting them do not target or disposition those seams.","finding_id":"APR2-009","fix":"Add the complete instance-manager and enforcement test inventory to §§ 3.2-3.3 Targets. State which fixtures are deleted or rewritten for the new storage API and require focused regressions for both suites.","location":"Phase 3 / §§ 3.2-3.3","prevention":"Before deleting a class, sweep constructors, imports, monkeypatches, fakes, and fixtures across production and tests; list a rewrite or deletion for each hit.","principle":"Deleting a runtime abstraction requires an exhaustive disposition of its constructors, fakes, fixtures, and enforcement tests.","root_cause":"The repaired target inventory still omits WorkflowInstanceManager consumers in test_instance_manager.py and adjacent step-enforcement fixtures.","section_id":"3.3","severity":"blocking"},{"category":"weak-testability","check_key":"deleted-loader-test-seam-closure","description":"Section 4.3 now owns the main loader deletion surface, yet tests/workflows/test_workflow_hooks.py still patches the removed factory symbol twice. Those tests will break or continue asserting the obsolete seam.","finding_id":"APR2-010","fix":"Add tests/workflows/test_workflow_hooks.py to § 4.3 Targets, replace both WorkflowLoader patches with the domain-loader seam, and include the focused workflow-hook test command.","location":"Phase 4 / § 4.3","prevention":"After repairing a deletion inventory, repeat the class-wide symbol and string-patch sweep across tests and verify every match has an exact Target.","principle":"A deletion repair is complete only when every adjacent monkeypatch and fake is migrated to the replacement seam.","root_cause":"The WorkflowLoader repair misses two gobby.hooks.factory.WorkflowLoader patch sites in tests/workflows/test_workflow_hooks.py.","section_id":"4.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"source-size-preemptive-decomposition","description":"The proposed conditional extraction can leave agents.py at or beyond the enforced ceiling during implementation, and its Targets do not identify the required destination module.","finding_id":"APR2-011","fix":"Make decomposition unconditional in § 6.1. Move the new definition-related commands into a focused submodule first, target both that module and the remaining registration surface, and require both production files to stay below 1,000 lines.","location":"Phase 6 / § 6.1","prevention":"Record current and projected line counts for every touched production source and make decomposition an explicit predecessor whenever the projection approaches the ceiling.","principle":"A production module already near the 1,000-line ceiling must be decomposed before adding another command surface.","root_cause":"Section 6.1 makes extraction conditional even though src/gobby/cli/agents.py is already 861 lines before the planned CLI additions.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"instance-lineage-identity-immutability","description":"A caller can save an instance under a different agent_name while retaining the prior snapshot and lineage. Resume resolution can then combine the wrong definition identity with historical execution state.","finding_id":"APR2-012","fix":"Make agent_name immutable in ordinary save/update operations. Permit identity changes only through the explicit replacement operation, define its snapshot/lineage reset behavior, and add rejection and replacement tests.","location":"Phase 3 / §§ 3.1-3.2","prevention":"Classify every persisted identity field as immutable or replace-only and test generic updates against that classification.","principle":"A persisted execution lineage record must not change its agent identity through a generic save path.","root_cause":"The proposed save semantics preserve snapshot and lineage fields while leaving agent_name writable, allowing mixed identity and snapshot state.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"prelaunch-compensation-completeness","description":"Preparation can mutate state and issue credentials before it returns. A failure during preparation or after credential/bootstrap acquisition can escape the plan's cleanup branch, leaving active credentials or partial runtime state.","finding_id":"APR2-013","fix":"Specify one idempotent prelaunch cleanup owner covering failures inside preparation, snapshot persistence, credential activation, bootstrap, and launch. Add fault-injection tests at every boundary and prove repeated cleanup is safe.","location":"Phase 3 / §§ 3.2 and 3.4","prevention":"Enumerate acquisition boundaries in prepare-to-launch order and inject a failure after each one to prove one cleanup owner restores the prelaunch state.","principle":"Every acquired prelaunch resource and side effect needs an idempotent compensation owner for failures at each subsequent boundary.","root_cause":"The plan compensates snapshot-save failure after successful preparation but does not cover partial failures inside preparation, active credential issuance, bootstrap, or launch.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"instance-copy-deterministic-resolution","description":"Multiple definition candidates can share names or timestamps across project and global scope. 'Latest' alone can copy an arbitrary snapshot or attach the wrong lineage, and the zero-candidate path is unspecified.","finding_id":"APR2-014","fix":"Define the qualifying agent-step predicate, project-first then global fallback, lineage resolution, and a stable total order including a final ID tie-break. Add fixtures for duplicates, equal timestamps, fallback, and no candidate.","location":"Phase 3 / § 3.2","prevention":"For every migration lookup, specify qualification, scope precedence, total ordering, missing-row behavior, and duplicate fixtures.","principle":"Data-copy migrations must define deterministic, scope-aware resolution when zero, one, or many candidates exist.","root_cause":"The latest-candidate rule lacks an agent-step predicate, project-first/global fallback lineage, and a deterministic tie-break.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"ambient-transaction-after-commit","description":"The planned post-manager-transaction bump is earlier than the true commit boundary under ambient nesting. An outer rollback can therefore leave local revisions and listeners advanced while the definition write and persistent revision disappear.","finding_id":"APR2-015","fix":"Add an outermost after-commit callback mechanism to the ambient transaction layer and route all definition revision/local-listener work through it. Test outer commit, outer rollback, nested mutation failure, and exactly-once delivery.","location":"Phase 1 / §§ 1.2-1.4","prevention":"Test definition mutations inside nested ambient transactions and assert persistent revision, local counter, and listener state before commit and after rollback.","principle":"Revision counters and listener delivery must occur only after the outermost ambient transaction commits.","root_cause":"A manager's inner transaction can finish and bump local state while an ambient outer transaction remains able to roll back.","section_id":"1.4","severity":"blocking"}],"reviewer_session":"b40679e5-fd7c-4581-8ccc-4509141525fe","round":2,"verdict":"needs_review"},"session_id":"24384cfe-8cb0-4aaa-bfaa-5b86560252aa"}
```


**Round 3** `kind: verification`

- reviewer_run: 4866bfa1-37c5-4e46-9c16-ab3a58051aca
- reviewer_session: 32bf2f1b-08ca-410d-baa2-e0eb8b9fd979
- verdict: needs_review
- findings:
- APR3-001 / blocking / 1.3 removed the child on ordinary soft delete
- APR3-002 / blocking / 2.3.5 kept the stale 25-stepful bundled count (fixer-induced, APR2-004)
- APR3-003 / blocking / staged import rejections lacked an owned test_imports.py seam
- APR3-004 / blocking / rule-engine and session-defaults suites unowned by the 4.1/4.2 cutovers
- APR3-005 / blocking / pipeline MCP CRUD still imported the generic module 5.2 deletes
- APR3-006 / blocking / cli/pipelines.py and four CLI suites undispositioned at the package deletion
- APR3-007 / blocking / 2.3 copy migration assumed only the flat pre-2.2 shape (fixer-induced, APR2-002)
- APR3-008 / blocking / claimed non-reentrant same-target lock reacquisition
- APR3-009 / blocking / timestamp order could migrate the wrong persona's snapshot (fixer-induced, APR2-014)
- APR3-010 / blocking / variable-defaults cache and paths lacked project scope
- APR3-011 / blocking / symmetric drop backstop blocked on legitimate typed evolution
- APR3-012 / blocking / prepare_terminal_spawn raise left partial acquisitions unreachable (fixer-induced, APR2-013)
- APR3-013 / blocking / baseline_refresh.rs implementation owner untargeted in 1.1
- APR3-014 / blocking / listener started from sync init with no rollback branch
- APR3-015 / blocking / sixteen instance-manager test seams undispositioned (fixer-induced, APR2-009)
- APR3-016 / blocking / agent-token capability matrix missed the relocated variable routes
- APR3-017 / blocking / renamed/removed MCP tool consumers outside Targets
- APR3-018 / blocking / web variable helper and style-surface fake untargeted
- APR3-019 / blocking / cli-commands.md still documents the deleted workflows group
- resolution_notes: Eighteen findings repaired in this revision (unattended,
  operator-delegated), with class-wide sweeps per finding class. APR3-008's
  core claim was refuted against the repository — nested same-target
  acquisition short-circuits on frozen-dataclass value equality in
  _PostgresTransaction.acquire_additional_lock (postgres_pool.py:313-314) via
  the ambient path, so the mutation lock is re-entrant and the proposed
  optional-Transaction plumbing was not applied; 3.1 instead now pins the
  mechanism and requires the re-entrancy test to run through the real adapter
  on one shared adapter instance. Narrowed repairs recorded against verified
  evidence: APR3-006 (tests/cli/test_pipelines.py holds no cli.workflows
  reference, so its retarget was dropped and the closure records it
  deliberately), APR3-013 (the refresh acceptance is a single-const equality,
  not an allowlist; 1.1 extends it to an enumerated added-statement
  acceptance), APR3-017 (list_workflows appears in no prompt or bundled YAML;
  the closure covers its registry/E2E inventories plus every
  get_workflow_status consumer), APR3-018 (workflowVariables.ts is coupled
  through its definitionJson parameter rather than reading the field
  directly). Major repairs: soft delete now preserves the step-workflow
  child with a delete-restore acceptance (1.3); the agent copy migration
  normalizes flat and nested source shapes (2.3); the instance copy resolves
  the active persona identity before ordering (3.2); prepare_terminal_spawn
  owns its internal compensation with per-acquisition fault injection (3.2);
  the P7 drop backstop became directional via a new legacy_copy_ledger
  checkpoint table written by all four definition copy migrations and
  dropped with the legacy tables (1.1, 2.3, 4.1-4.3, 7.1, E1); variable
  defaults are project-scoped with a keyed cache (4.2); the listener splits
  construction from async start with a rollback branch (1.4); the capability
  matrix, MCP tool consumers, CLI/web/docs closures gained exact Targets and
  acceptance (5.1, 5.2, 6.1, 6.2, 7.3).

```json plan-review-round
{"evidence_id":"b11eb9aa-e9ed-42d9-ab8e-1f97330cca7d","plan_hash":"ccfda402f8b2bf479bc92c484f459c9fc4a1a59fdd650d1ad26fa5fb9a26f1e1","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"19308048690d00eb4ac857bcb281598b02c31c9a7b8bacbfe107e9ef62065af6","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":19,"total":25},"evidence_id":"b11eb9aa-e9ed-42d9-ab8e-1f97330cca7d","lanes":[{"candidate_count":10,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":8,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":23,"manifest_digest":"f70d5c2f556f44e0ec0f99677662079afccefa3792a2be635920e4e349f7640a","status":"valid"},"source_digest":"7c5ae3db9b911dfb59a71629a0767876ba1959b7f7576b2471606ae9c05ff870","version":1},"findings":[{"category":"unhandled-edge","check_key":"soft-delete-child-payload-preservation","description":"Section 1.3 says parent delete/hard-delete removes the child, while §§ 2.3 and 7.1 rely on agent_step_workflows surviving soft deletion. Removing the child on ordinary delete destroys the only restorable step-workflow payload.","finding_id":"APR3-001","fix":"State that AgentDefinitionManager.delete preserves the child and only hides the parent. Delete the child only through set_step_workflow(None), upsert-without-steps, hard_delete, or purge; add delete→restore tests for payload and revision behavior.","location":"Phase 1 / § 1.3, with §§ 2.3 and 7.1","prevention":"For every parent-child soft-delete model, test delete→restore and classify child behavior separately for soft-delete, explicit child removal, hard-delete, and purge.","principle":"Soft deletion must preserve every payload needed for a lossless restore.","root_cause":"The child-lifecycle text conflates ordinary soft-delete with hard-delete even though the parent JSON no longer carries step-workflow data.","section_id":"1.3","severity":"blocking"},{"category":"traceability","causal_finding_id":"APR2-004","causal_section_ids":["Epic Review Notes","2.2","2.3","E1"],"check_key":"bundled-agent-count-parity-after-repair","description":"The repaired inventory is 25 bundled files total—21 stepful and four step-less—yet acceptance 2.3.5 still requires 25 stepful bundled agents plus four step-less agents, an unsatisfiable 29-file set.","finding_id":"APR3-002","fix":"Change 2.3.5 to 21 stepful and four step-less bundled agents. Keep 29 agent rows and 25 child rows only in the hub-migration acceptance and E1, explicitly labeled hub-derived.","introduced_in_round":2,"location":"Phase 2 / §§ 2.2-2.3","prevention":"After repairing an inventory, sweep every numeric acceptance and end-to-end count and label each as filesystem-derived or database-derived.","principle":"Filesystem conversion counts and persisted-hub migration counts must remain distinct in every acceptance criterion.","root_cause":"The APR2-004 inventory repair updated the plan narrative and YAML set but left acceptance 2.3.5 on the old 25-stepful bundled count.","section_id":"2.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"staged-import-test-target-ownership","description":"Agent, rule, and variable cutovers each change sync_imported_definition and cite tests/workflows/test_imports.py, but none targets that test before § 4.3. Their phase-local rejection acceptances therefore lack an owned implementation seam.","finding_id":"APR3-003","fix":"Add tests/workflows/test_imports.py to Targets in §§ 2.3, 4.1, and 4.2, with exact agent/rule/variable rejection fixtures in each phase before § 4.3 retargets the suite to final typed dispatch.","location":"Phases 2 and 4 / §§ 2.3, 4.1, 4.2","prevention":"For every staged mutation of one shared module, assign the shared tests to each stage that changes their expected behavior.","principle":"A serialized behavior change must own its changed regression seam in the same deliverable.","root_cause":"The plan stages import rejection across three cutovers but leaves the shared import suite targeted only by the later final-dispatch rewrite.","section_id":"2.3","severity":"blocking"},{"category":"weak-testability","check_key":"typed-cutover-behavior-suite-closure","description":"tests/workflows/test_rule_engine.py and test_session_defaults.py seed LocalWorkflowDefinitionManager. Once rules/defaults read typed tables, these suites stop exercising their own fixtures or fail, yet neither has exact 4.1/4.2 ownership.","finding_id":"APR3-004","fix":"Target and retarget test_rule_engine.py in § 4.1. Split test_session_defaults.py ownership across §§ 4.1 and 4.2, naming the manager fixture replacements and focused validation commands.","location":"Phase 4 / §§ 4.1-4.2","prevention":"Before deleting a storage seam, enumerate test constructors and fixture seeders and assign each file to the first cutover that stops reading its fixtures.","principle":"A storage cutover must retarget every behavior suite that seeds the replaced store before the reader changes.","root_cause":"The plan defers a grep-derived test inventory to implementation while omitting known rule-engine and default-loading suites from exact Targets.","section_id":"4.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"pipeline-mcp-deletion-closure","description":"The current pipeline MCP module imports create/update/delete/export helpers and LocalWorkflowDefinitionManager from modules § 5.2 deletes. The plan neither replaces pipeline CRUD in § 4.3 nor targets the pipeline callers in § 5.2.","finding_id":"APR3-005","fix":"In § 4.3, replace pipeline MCP CRUD with PipelineDefinitionManager operations and add focused CRUD acceptance. In § 5.2, target _pipelines.py, pass kind='pipeline' to auto-export/delete, and assert no generic-definition or legacy-manager import remains.","location":"Phases 4-5 / §§ 4.3 and 5.2","prevention":"For every deleted module, sweep imports, helper calls, and auto-export/delete callers and assign each surviving consumer to a preceding deliverable.","principle":"A surviving module must shed every import from a module before that dependency is deleted.","root_cause":"The pipeline MCP task owns only loader rewiring, while its CRUD and auto-export paths still depend on the generic definitions module and legacy manager removed in § 5.2.","section_id":"4.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"cli-package-deletion-closure","description":"src/gobby/cli/pipelines.py still imports get_project_path/get_workflow_loader from cli.workflows.common, and multiple CLI suites import or patch cli.workflows at collection time. The planned deletion leaves a broken production import and red tests.","finding_id":"APR3-006","fix":"Retarget tests/cli/test_pipelines.py with PipelineLoader in § 4.3. In § 6.1 target pipelines.py and explicitly delete, split, or retarget test_cli_workflows.py, test_workflows.py, test_workflows_coverage.py, and test_pipelines_coverage.py into surviving domain suites.","location":"Phases 4 and 6 / §§ 4.3 and 6.1","prevention":"Before deleting a command package, sweep production imports plus test imports, patches, runners, and coverage suites and give each an explicit delete or retarget disposition.","principle":"Deleting a CLI package requires rewiring surviving production imports and disposing every module-scope test import and patch path in the same commit.","root_cause":"The CLI restructure inventories the package files but omits src/gobby/cli/pipelines.py and the existing workflow/pipeline test suites that import or patch the deleted package.","section_id":"6.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"APR2-002","causal_section_ids":["2.1","2.2","2.3"],"check_key":"nested-legacy-agent-copy-normalization","description":"After § 2.2, bundled sync and MCP writes serialize nested step_workflow into legacy agent rows. The § 2.3 copy migration strips and extracts only top-level steps, step_variables, and exit_condition, so newly written nested rows lose children or retain child data in the parent.","finding_id":"APR3-007","fix":"Normalize both flat and nested source shapes in the copy migration: strip either representation from the parent, extract the child from nested step_workflow when present and otherwise from flat fields, add type guards and per-shape equivalence checks, and test a § 2.2 sync followed by migration plus mixed-shape rows.","introduced_in_round":2,"location":"Phase 2 / §§ 2.1-2.3","prevention":"For each staged shape change, serialize data through every live writer in the preceding commit and use those bytes as migration fixtures.","principle":"A migration following a serialized shape cutover must accept every source shape the preceding working commit can persist.","root_cause":"APR2-002 made the model/YAML cutover atomic but § 2.3 still assumes only the pre-repair flat legacy JSON shape.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"instance-lock-real-adapter-reentry","description":"Persona and enforcement are planned to hold AgentStepInstanceMutation around reads and call manager mutators inside it. Current ambient nesting reacquires that equal-priority target through acquire_additional_lock, so the real adapter can raise LockAcquisitionOrderError.","finding_id":"APR3-008","fix":"Give AgentStepInstanceManager mutators an optional Transaction parameter and require outer persona/enforcement sections to pass their held immediate transaction, avoiding reacquisition. Add real-adapter tests for nested save/replace/merge/delete and keep the instance-before-session-variable lock order.","location":"Phase 3 / §§ 3.1-3.2","prevention":"Exercise nested manager calls through the real PostgresHubDatabase adapter and inspect equal-target/equal-priority lock behavior before declaring re-entry.","principle":"A caller-held critical section must not reacquire the same non-reentrant lock through an inner manager call.","root_cause":"The plan declares the renamed descriptor re-entrant, but current nested transaction_immediate routes through equal-priority additional-lock acquisition, which raises.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR2-014","causal_section_ids":["3.2"],"check_key":"instance-copy-active-identity-resolution","description":"Legacy storage permits multiple per-session rows. A reachable A→B→A persona sequence can leave stale B newer than active A, so latest updated_at deterministically migrates the wrong snapshot and agent_name.","finding_id":"APR3-009","fix":"Resolve active agent identity from the active run/session persona state, reconcile it with _agent_type and _step_workflow_name, fail on contradictions, and apply timestamp/id ordering only to rows for that identity. Add A→B→A, stale-newer-row, equal-timestamp, and suffix-matching non-generated fixtures.","introduced_in_round":2,"location":"Phase 3 / §§ 3.2-3.4","prevention":"For migration lookups with historical duplicates, resolve authoritative identity first, reconcile redundant signals, and order only within the matching identity set.","principle":"Deterministic ordering is valid only after candidates are restricted to the authoritative runtime identity.","root_cause":"APR2-014 added a total order across all suffix-matching rows but did not resolve which agent is active for the session.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"project-scoped-defaults-cache-isolation","description":"SessionVariableManager currently has only session_id and one unkeyed defaults cache. Routing it through load_variable_defaults(db, project_id=None) can omit project overrides or leak project A defaults into project B while the other three application paths disagree.","finding_id":"APR3-010","fix":"Require each defaults path to resolve session.project_id, define project-first/global fallback deduplication in get_defaults_map, key the TTL cache by project_id plus variables revision, and add alternating project-A/project-B/global-fallback tests across all four application paths.","location":"Phases 1 and 4 / §§ 1.2 and 4.2","prevention":"For every scoped cache, enumerate key dimensions and alternate two scopes plus global fallback in one regression.","principle":"A cache for project-scoped fallback data must include project scope in both lookup and key.","root_cause":"The plan retains one SessionVariableManager TTL cache and adds revision invalidation without specifying project resolution or per-project cache entries.","section_id":"4.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"drop-backstop-directional-drift-proof","description":"After each cutover, valid edits, sync refreshes, restores, or hard-deletes change only typed rows. P7's current payload-equivalence backstop treats those safe changes exactly like a dangerous legacy-only write and can permanently block the destructive migration.","finding_id":"APR3-011","fix":"Add a copy-time per-row ledger containing legacy id, domain, and normalized source hash plus an explicit typed-deletion outcome. At P7 compare legacy rows to that checkpoint, allow recorded typed evolution, and test typed-only update/sync/delete separately from legacy-only update and post-copy legacy insertion.","location":"Phases 2, 4, and 7 / §§ 2.3, 4.1-4.3, 7.1","prevention":"For staged copies, record a copy-time source checkpoint and test source-only and target-only mutations independently before defining the final drop guard.","principle":"A destructive backstop must distinguish source-side drift from legitimate target-side evolution after cutover.","root_cause":"The plan compares current legacy and typed payloads symmetrically even though typed tables become the sole writable authority months or phases before the drop.","section_id":"7.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR2-013","causal_section_ids":["3.2","3.4"],"check_key":"preparation-partial-result-compensation","description":"prepare_terminal_spawn creates the child session, initial variables, run metadata, and managed credential before returning PreparedSpawn. On an exception inside that sequence the caller has no partial result, so the claimed single cleanup owner cannot identify what to remove or revoke.","finding_id":"APR3-012","fix":"Move the cleanup owner inside prepare_terminal_spawn or raise a structured partial PreparedSpawn carrying every acquired row and credential handle. Specify idempotent compensation in a finally/exception path and fault-inject after child creation, variable merge, run persistence, and credential issuance.","introduced_in_round":2,"location":"Phase 3 / §§ 3.2 and 3.4","prevention":"Fault-inject after each internal acquisition and require the exception/return contract to expose a complete compensation handle.","principle":"A cleanup owner can compensate an internal failure only if the failing operation exposes every acquisition made before raising.","root_cause":"APR2-013 added prose for cleanup inside prepare_terminal_spawn without defining how the caller obtains child, run, and credential identities when preparation raises before returning.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"baseline-refresh-owner-target","description":"crates/gcore/src/schema/runner.rs only delegates to baseline_refresh_statement. The exact allowlist that must accept seven new-table statements lives in crates/gcore/src/baseline_refresh.rs, which § 1.1 does not target.","finding_id":"APR3-013","fix":"Add crates/gcore/src/baseline_refresh.rs::* and its focused module tests to § 1.1 Targets, and specify the exact seven-table added-statement inventory there.","location":"Phase 1 / § 1.1","prevention":"Resolve each planned symbol to its current qualified file immediately before review and target the implementation owner plus its tests.","principle":"Every changed production owner must appear as an exact Target; naming a delegating caller does not cover the implementation module.","root_cause":"The refreshed plan still locates baseline_refresh_statement in runner.rs, while the current implementation moved the allowlist to baseline_refresh.rs.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"listener-async-start-and-init-rollback","description":"init_storage_and_config is used by direct synchronous GobbyRunner construction, so start() cannot assume an event loop. If later initialization fails after listener creation/start, runner_rollback has no branch to cancel its tasks or close its pool-exempt connection.","finding_id":"APR3-014","fix":"Construct the listener during storage setup, start it from the existing async runtime startup phase, add it to runner state, construction rollback, and graceful shutdown, and target runner.py, runner_rollback.py, and the runner init/shutdown tests.","location":"Phase 1 / § 1.4","prevention":"For every runner-owned service, trace construction, async start, later-init failure rollback, normal shutdown, and direct-constructor tests.","principle":"A background service must start only under a running async runtime and must be owned by both construction rollback and graceful shutdown.","root_cause":"The plan starts the new listener from synchronous storage construction and targets only the graceful-shutdown tail.","section_id":"1.4","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"APR2-009","causal_section_ids":["3.2","3.3"],"check_key":"instance-deletion-adjacent-test-seam-closure","description":"tests/workflows/test_step_runtime_transitions.py, test_step_enforcement_audit.py, tests/agents/test_runtime_cleanup.py, and adjacent suites still import or instantiate WorkflowInstance/WorkflowInstanceManager. They fail collection or retain obsolete fixtures when § 3.3 deletes the classes.","finding_id":"APR3-015","fix":"Extend §§ 3.2-3.3 with exact Targets and rewrite/delete dispositions for every remaining WorkflowInstance and WorkflowInstanceManager hit across transition, enforcement-audit/error-code, lifecycle-monitor, merge, cleanup, coordinator, observability, server cleanup, and E2E fixtures.","introduced_in_round":2,"location":"Phase 3 / §§ 3.2-3.3","prevention":"Repeat the full class-wide symbol and string-patch sweep after applying a deletion repair and list every remaining test hit in Targets.","principle":"A class deletion repair is complete only after every constructor, annotation, fixture, patch, and adjacent behavior suite has an exact disposition.","root_cause":"APR2-009 repaired named primary suites but missed additional transition, audit, cleanup, lifecycle, observability, and E2E consumers.","section_id":"3.3","severity":"blocking"},{"category":"missing-requirement","check_key":"relocated-route-agent-auth-capability","description":"DaemonProxy moves session-variable calls to /api/sessions/{id}/variables/*, but the agent-token capability matrix still grants only the deleted /api/workflows/variables paths. The exact client can become unauthorized despite both route and client tests passing.","finding_id":"APR3-016","fix":"Target auth_service.py, auth middleware/capability tests, and tests/mcp_proxy/test_mcp_proxy_stdio.py. Replace old grants with parameterized session-variable routes while retaining session identity binding, and retarget both DaemonProxy suites.","location":"Phase 5 / § 5.1","prevention":"For every route move, trace router registration, client literal, auth/capability matching, middleware identity binding, and all client test suites.","principle":"A relocated endpoint used by an authenticated internal client must be added to that client's capability matrix in the same change.","root_cause":"The HTTP relocation inventories router and stdio client paths but omits authorization policy and its separate regression seams.","section_id":"5.1","severity":"blocking"},{"category":"traceability","check_key":"removed-mcp-tool-consumer-closure","description":"list_workflows and get_workflow_status remain required by MCP inventory tests, E2E calls, production dispatch prompts, and agent instructions outside current Targets. The final tree can therefore retain calls to tools § 5.2 removes or renames.","finding_id":"APR3-017","fix":"Add registry/E2E consumers to § 5.2 and retarget them to the final tool set. Add dispatch/prompts.py, bundled agent instruction sources, and their tests to § 5.2 or § 7.3 and update them atomically.","location":"Phases 5 and 7 / §§ 5.2 and 7.3","prevention":"For every removed tool name, sweep production strings, prompt assets, bundled YAML, MCP inventories, E2E calls, and assertion fixtures.","principle":"Tool deletion and rename must update registries, executable consumers, prompts, bundled agent instructions, and their tests atomically.","root_cause":"The disposition table focuses on the workflows registry suites and the documentation sweep omits live prompt/E2E consumers.","section_id":"5.2","severity":"blocking"},{"category":"traceability","check_key":"typed-variable-ui-helper-and-capture-fixture","description":"web/src/components/settings/workflowVariables.ts still reads definition_json while the new variable API exposes default_value directly. web/tests/style-surfaces.spec.ts only fakes /api/workflows, so the migrated editors can receive no data in visual coverage.","finding_id":"APR3-018","fix":"Add both files to § 6.2 Targets, retype the helper around default_value, teach the capture fake /api/pipelines/definitions and /api/variables response shapes, and remove its generic-workflow branch.","location":"Phase 6 / § 6.2","prevention":"Trace response data from network fake through hooks, adapters, and components for every endpoint migration.","principle":"A frontend API-shape migration must include extracted data adapters and integration fakes, not only hooks and components.","root_cause":"The UI target inventory misses the helper that decodes the old definition_json shape and the capture harness that serves only the old generic route.","section_id":"6.2","severity":"blocking"},{"category":"traceability","check_key":"active-cli-guide-command-parity","description":"docs/guides/cli-commands.md still presents the entire gobby workflows group that § 6.1 deletes. The scoped audit can pass while users are still directed to nonexistent commands.","finding_id":"APR3-019","fix":"Add docs/guides/cli-commands.md to § 7.3 Targets, replace the workflows command table with agents, pipelines, variables, and sync replacements, and include active guides in the documentation search assertion.","location":"Phase 7 / § 7.3","prevention":"Search the full active docs tree for every removed command, route, table, and discriminator and target every current-behavior hit.","principle":"Active user documentation must be included in the same inventory as the command surface it describes.","root_cause":"The documentation sweep's explicit guide list omits cli-commands.md, and the legacy audit does not scan active docs.","section_id":"7.3","severity":"blocking"}],"reviewer_session":"32bf2f1b-08ca-410d-baa2-e0eb8b9fd979","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"24384cfe-8cb0-4aaa-bfaa-5b86560252aa"}
```


**Round 4** `kind: verification`

- reviewer_run: fea2887d-e7ff-4a43-a64e-49e9b91687ad
- reviewer_session: 6e9db1b7-a15c-4621-b571-cc5c171df1d7
- verdict: needs_review
- findings:
- APR4-001 / blocking / Constraints still bound target.id = source.id into the P7 backstop (fixer-induced, APR3-011)
- APR4-002 / blocking / web agent types, adapters, and read-only view still model top-level step fields
- APR4-003 / blocking / agents_termination.py result key workflow_instances_deleted unowned
- APR4-004 / blocking / workflow_templates.py orphaned by the 5.1 router deletion, undispositioned
- APR4-005 / blocking / dispatcher spawn test asserts the deleted _step_workflow_name seed
- APR4-006 / blocking / persona-switch disagreement state misclassified as corruption (fixer-induced, APR3-009)
- APR4-007 / blocking / caller variables overwrite the persona delta with no reserved-key validation
- APR4-008 / blocking / copy/drop sequence left legacy writers unfenced
- resolution_notes: All eight findings accepted after repository verification
  (unattended, operator-delegated) and repaired in this revision with
  class-wide sweeps. Two were narrowed against verified code: APR4-006's
  no-matching-row case resolves through the existing 3.3 recovery path (the
  plan's canonical missing-instance handler) instead of a migration-time
  snapshot rebuild, with _agent_type made the sole authoritative identity
  and the disagreement RAISE dropped; APR4-008's P7 half is contradicted by
  the coded maintenance-epoch fence (cli/schema.py:114-146 refuses
  destructive apply without an open epoch; hub_maintenance.py:185-196 stops
  the daemon; maintenance_epoch.py:174-271 terminates gobby% backends to
  proven quiescence; baseline.sql:4240-4317 blocks reconnects), which 7.1
  now pins instead of adding table locks — the copy-side gap is real
  (runner.rs:105-113 serializes runners only; migrations apply before the
  predecessor gate severs old backends) and every copy migration now opens
  with LOCK TABLE ... ACCESS EXCLUSIVE, with a write-rejection trigger on
  workflow_instances because instance rows have no ledger backstop.
  Class-widened repairs: APR4-002 additionally targets the production
  adapter AgentsTabActions.ts, whose buildAgentDefinitionBody writes
  top-level steps, and the Playwright AGENT_DEFINITION fixture, and records
  that the current draft pipeline already drops step_variables and
  exit_condition; APR4-003's rename gains its first test seam, since no
  existing test asserts the old key; APR4-007 rejects collisions against
  the just-built persona delta plus is_reserved_workflow_variable — the
  guard set_variable already uses — rather than a hand-maintained list.
  The superseded (name, project_id) backstop sentence in Constraints,
  predating even the round-3 design, was corrected in the same APR4-001
  sweep.

```json plan-review-round
{"evidence_id":"239757c3-df39-4a00-a56e-caaa4e2f3b22","plan_hash":"ad3553dd6f61f7f5cb94c6113d3c5862eff1eca9097cdcdb535e437ea8b1f190","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"5ec8ee4dac76f30646f0a755b7f782c5c0d24b08843f4c54fb1fa46cd7dbc788","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":8,"total":10},"evidence_id":"239757c3-df39-4a00-a56e-caaa4e2f3b22","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":23,"manifest_digest":"3fcea5628c8ed7d26f7ded567f47ba475b6019a129daba156c54b2ede44a7392","status":"valid"},"source_digest":"25206f5963a12794520364fd7adbcaed165640c2eb5bd9beb38e9aab53029c61","version":1},"findings":[{"category":"traceability","causal_finding_id":"APR3-011","causal_section_ids":["1.1","2.3","4.1","4.2","4.3","7.1","E1"],"check_key":"p7-directional-guard-contract-parity","description":"Constraints still state that `target.id = source.id` is part of the P7 drop backstop, while §7.1 says typed state is never consulted and validates only each legacy row against `legacy_copy_ledger`. Implementers can follow either mutually exclusive contract, reintroducing the typed-evolution blockage APR3-011 repaired.","finding_id":"APR4-001","fix":"Rewrite the Constraints equivalence paragraph so target identity and payload checks apply only inside the domain copy migrations. Define P7 separately as a legacy-only `(legacy_id, domain, normalized source_hash)` checkpoint check, with no typed-row lookup, and keep §7.1's directional tests.","introduced_in_round":3,"location":"Constraints and Phase 7 / § 7.1","prevention":"After replacing a cross-phase invariant, sweep governing framing, every producer, every consumer, acceptance cases, and changelog text for the superseded predicate.","principle":"Governing constraints and a destructive deliverable must prescribe one unambiguous safety predicate.","root_cause":"APR3-011 replaced the symmetric P7 guard with a directional ledger check across §§1.1, 2.3, 4.1-4.3, 7.1, and E1, while the governing Constraints retained the superseded requirement that P7 compare typed target identity, including `target.id = source.id`.","section_id":"7.1","severity":"blocking"},{"category":"traceability","check_key":"agent-nested-step-workflow-web-consumer-closure","description":"`AgentsTabData`, `AgentEditForm.types.ts`, and `AgentReadOnlyDetails.tsx` still model or read top-level step fields. The final API moves them under `step_workflow`, and none of these files is targeted for that cutover, so hydrated agent details and edit drafts can silently lose the step workflow.","finding_id":"APR4-002","fix":"Add `AgentEditForm.types.ts`, `AgentReadOnlyDetails.tsx`, `AgentsTabData.ts`, `AgentEditors.test.tsx`, and `AgentsTabActions.test.ts` to §6.2. Type and decode `step_workflow.{steps,variables,exit_condition}` end to end, and add a hydrated-details-to-draft regression that preserves all three fields.","location":"Phases 2 and 6 / §§ 2.2, 2.3, and 6.2","prevention":"For every response-shape move, trace the payload from fetch through shared types, normalization, drafts, read-only views, editors, and their fixtures.","principle":"An API shape cutover must migrate every typed consumer, adapter, display, draft path, and fixture in the same change.","root_cause":"The plan retargets the main agent editor but omits shared web types and read-only/detail adapters that still model `steps` and `step_variables` as top-level fields.","section_id":"6.2","severity":"blocking"},{"category":"traceability","check_key":"runtime-cleanup-result-key-owner-closure","description":"`src/gobby/mcp_proxy/tools/agents_termination.py` still emits `workflow_instances_deleted`. The file is absent from all Targets, and that production token is absent from §7.2's owner inventory even though the final audit rejects every `workflow_instances` occurrence.","finding_id":"APR4-003","fix":"Target `agents_termination.py` in §3.3, rename the result key to `agent_step_instances_deleted`, retarget result-shape tests and fakes, and add this occurrence to §7.2's checked owner inventory.","location":"Phases 3 and 7 / §§ 3.3 and 7.2","prevention":"For every storage/model rename, sweep result dictionaries, serialized payloads, logs, metrics, fakes, and assertions in addition to class and table references.","principle":"A renamed runtime model must update externally returned field names and their tests alongside internal storage owners.","root_cause":"The cleanup cutover targets the manager and terminal log field but misses the MCP termination wrapper that exposes the legacy model name in its result.","section_id":"3.3","severity":"blocking"},{"category":"traceability","check_key":"generic-workflow-template-module-deletion-closure","description":"`workflow_templates.py` is used by the generic `/api/workflows/templates` handler that §5.1 deletes. The orphan module and `test_workflow_templates.py` remain untargeted, and the module's public `workflow_type` payloads make §7.2's production-token audit fail.","finding_id":"APR4-004","fix":"Add `src/gobby/workflows/workflow_templates.py` and `tests/workflows/test_workflow_templates.py` to §5.1 and delete them with the generic template route. Record their `workflow_type` occurrences in §7.2's preexisting owner inventory.","location":"Phases 5 and 7 / §§ 5.1 and 7.2","prevention":"For every deleted router, walk each handler import and call edge, then assign delete, retarget, or retained-consumer evidence to every reached module and test.","principle":"Deleting a route surface must disposition its private implementation modules and dedicated tests.","root_cause":"The generic route deletion removes the sole production consumer of `workflow_templates.py` without assigning that module or its test suite to any deliverable.","section_id":"5.1","severity":"blocking"},{"category":"weak-testability","check_key":"removed-step-workflow-variable-test-closure","description":"`tests/dispatch/test_dispatcher.py::test_spawn_action_uses_services_and_records_agent_run` asserts that initial variables contain `_step_workflow_name == 'backend-developer-steps'`. Section 3.2 removes that seed, yet no deliverable targets this test, leaving a deterministic focused-suite failure.","finding_id":"APR4-005","fix":"Add `tests/dispatch/test_dispatcher.py` to §3.2. Retarget the assertion to require `_step_workflow_name` absence while preserving the selected agent identity; keep durable typed-instance creation covered at the real spawn boundary.","location":"Phase 3 / § 3.2","prevention":"Before deleting a variable writer, run a class-wide production-and-test token sweep and target every positive assertion, absence assertion, fixture seed, and patch path.","principle":"Removing a runtime variable requires an exact disposition for every positive assertion and fixture that encodes its former writer contract.","root_cause":"The `_step_workflow_name` writer sweep covers spawn/persona tests but misses a dispatcher service test that still requires the deleted initial-variable seed.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR3-009","causal_section_ids":["3.2"],"check_key":"instance-copy-persona-signal-authority","description":"`apply_persona_impl` uses `build_session_persona_changes`, which writes `_agent_type` and deliberately leaves `_step_workflow_name` untouched. A valid pre-cutover A→B switch can therefore hold `_agent_type=B` beside stale `A-steps`; §3.2 now classifies that reachable state as corruption and aborts the instance migration.","finding_id":"APR4-006","fix":"Make `_agent_type` authoritative when present on this legacy path. Treat conflicting `_step_workflow_name` as stale metadata, resolve B's typed child and rebuild B's snapshot when no matching legacy row exists, migrate no row for a step-less B, and add A→B stepful and step-less fixtures while retaining loud failure for genuinely unresolved identity.","introduced_in_round":3,"location":"Phase 3 / § 3.2","prevention":"For every migration that reconciles redundant identity signals, inventory every live writer and enumerate precedence for each reachable partial-update state.","principle":"Migration identity resolution must distinguish an authoritative current signal from stale redundant metadata for every reachable writer state.","root_cause":"APR3-009 made `_agent_type` and `_step_workflow_name` disagreement fatal without accounting for the public persona path, which intentionally updates `_agent_type` alone.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"persona-custom-variable-reserved-key-integrity","description":"The public persona tool accepts an arbitrary variables dictionary, and `apply_persona_impl` applies it after setting `_agent_type`. A caller can submit another `_agent_type`, committing agent B's session identity beside agent A's requested snapshot even after §3.2 adds cross-row atomicity.","finding_id":"APR4-007","fix":"Define and reject collisions with the reserved persona/runtime field set before opening the transition transaction, including `_agent_type`, `_step_workflow_name`, and completion/step-state fields. Add adversarial tool and implementation tests proving rejection leaves both session variables and the typed instance unchanged.","location":"Phase 3 / § 3.2","prevention":"At every custom-variable merge boundary, define a reserved field set and test adversarial collisions against identity, lifecycle, completion, and snapshot metadata.","principle":"Caller-supplied extension data must never overwrite authoritative fields of the state transition that consumes it.","root_cause":"`apply_persona_impl` overlays public `variables` after building the authoritative persona delta, with no reserved-key validation.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"staged-legacy-runtime-write-fence","description":"A pre-cutover daemon can update `workflow_instances` during or after the P3 copy because its writes do not participate in the schema advisory lock. At P7, another write can also commit after the preflight SELECT and before DROP obtains its table lock, so valid runtime state can be omitted from `agent_step_instances` and then destroyed.","finding_id":"APR4-008","fix":"At P3, acquire an `ACCESS EXCLUSIVE` lock before copying `workflow_instances`, wait out existing writers, and install a persistent database rejection barrier for later legacy runtime writes before releasing the lock. At P7, lock all legacy tables before running any backstop query and hold those locks through DROP. Add a two-connection stale-writer fault test for during-copy, post-copy, and check-to-drop attempts.","location":"Phases 3 and 7 / §§ 3.2 and 7.1","prevention":"For every online copy/drop sequence, test a stale writer before copy, during copy, after copy, during preflight, and immediately before drop under a second connection.","principle":"A staged copy followed by destructive drop must fence source writers before the copy snapshot and hold the fence through final verification and drop.","root_cause":"The schema advisory lock serializes migration runners only; legacy runtime writes use independent per-session transactions, and §7.1 does not acquire table locks until each DROP executes.","section_id":"3.2","severity":"blocking"}],"reviewer_session":"6e9db1b7-a15c-4621-b571-cc5c171df1d7","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"24384cfe-8cb0-4aaa-bfaa-5b86560252aa"}
```


**Round 5** `kind: verification`

- reviewer_run: 06a44bb9-a57d-49c3-9959-569c86e8a61f
- reviewer_session: c21c4a4b-6c55-413c-98f0-525408988521
- verdict: needs_review
- findings:
- APR5-001 / blocking / copy-fence LOCK TABLE ordered ahead of the existence guard breaks fresh final-baseline lineages (fixer-induced, APR4-008)
- APR5-002 / blocking / raw gdaemon schema apply --destructive bypasses the maintenance-epoch fence the plan's own recipes invoked (fixer-induced, APR4-008)
- resolution_notes: Both findings accepted after repository verification
  (unattended, operator-delegated) and repaired in this revision. APR5-001:
  apply_pending_migrations (runner.rs:676-742) executes every pending
  non-destructive migration's SQL before stamping its receipt, and 7.1
  removes the legacy DDL from the final baseline, so the round-4 wording
  "the transaction opens with LOCK TABLE" would abort every post-P7 fresh
  install on undefined_table. The Constraints copy-fence bullet and all
  five per-domain copy sections now order the fence guard-first: existence
  check, then the conditional ACCESS EXCLUSIVE lock as the first
  source-table action (a lock taken inside the guarded DO block holds to
  transaction end), then copy, equivalence guard, and ledger checkpoint
  under it; the 3.2 write-rejection trigger installs inside the same
  guarded branch; new acceptance 7.1.10 replays every embedded copy
  migration against a fresh final-baseline lineage and requires receipted
  no-ops. The same sweep repaired a round-4 editing splice that had
  beheaded the Constraints equivalence-guard sentence, restoring it as its
  own bullet. APR5-002: the orchestrated path is fenced end-to-end (epoch
  discovery and orchestrator-epoch checks in cli/schema.py:114-146, the
  epoch GUC bound into the DSN at cli/schema.py:168, shell-out via
  schema_contract.apply_schema), but
  crates/gdaemon/src/main.rs::apply_schema authorizes destructive work
  from the backup manifest alone, so the raw command the plan's
  Constraints and E1 recipes named bypassed daemon stop, backend
  quiescence, and the reconnect fence. Both recipes now route through
  gobby hub-maintenance run schema-apply; 7.1 gains
  crates/gdaemon/src/main.rs::apply_schema and
  crates/gdaemon/tests/schema_cli.rs as targets and specifies the
  least-mechanism fence — destructive apply requires the connection to
  carry the open maintenance-epoch GUC the orchestrator already binds,
  refusing otherwise with the orchestrator command named (acceptance
  7.1.11; the epoch condition is added to 7.1.6); E1's epic-final replay
  opens an epoch on the isolated hub for the drop.

```json plan-review-round
{"evidence_id":"aef46355-a6cb-4dc4-9b79-4730dacef070","plan_hash":"00b2839f45b87d2ee3a3ebbb8e7bd505e22ac9d0fdb5aae38bf3da78ab7bcda0","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c592b1967bf06a53a8819296edc2a815ba106a32fa8c63b4fea026e2c3153a1c","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":2,"total":2},"evidence_id":"aef46355-a6cb-4dc4-9b79-4730dacef070","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":23,"manifest_digest":"80884a918647794becbd61e64df0d775263717903cee83262cb189165b4104fc","status":"valid"},"source_digest":"a99432887900b7bf4dff3bbabc78f24893dd12eed946618d4d15557e60743184","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"APR4-008","causal_section_ids":["Constraints","2.3","3.2","4.1","4.2","4.3"],"check_key":"fresh-lineage-conditional-copy-lock","description":"All five copy migrations are specified to open with an unconditional LOCK TABLE on a legacy source. On a fresh lineage built from the final P7 baseline those relations do not exist, and the runner executes each pending migration's SQL before stamping its receipt, so the lock fails before the existing absence guard can no-op.","finding_id":"APR5-001","fix":"Rewrite Constraints and §§ 2.3, 3.2, 4.1–4.3 so each migration checks source existence first, conditionally acquires ACCESS EXCLUSIVE as its first source-table action, then copies and validates under that lock; conditionally install the workflow_instances rejection trigger. Add an end-state-baseline test that replays the actual embedded copy migrations with both legacy tables absent.","introduced_in_round":4,"location":"Constraints; §§ 2.3, 3.2, 4.1–4.3; § 7.1","prevention":"For every legacy copy migration, replay the final baseline with the source absent and verify the order: existence guard, conditional ACCESS EXCLUSIVE lock, copy, equivalence check, and ledger checkpoint.","principle":"A guarded migration must establish that a legacy relation exists before referencing it, while taking its concurrency fence before the first source read.","root_cause":"APR4-008 placed each legacy LOCK TABLE at transaction opening, ahead of the information_schema guard that makes the copy fresh-redundant.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"APR4-008","causal_section_ids":["Constraints","7.1"],"check_key":"destructive-apply-maintenance-epoch-entrypoint","description":"The repaired P7 argument requires the drop to run only inside the stopped, backend-quiesced maintenance epoch, yet Constraints and E1 still instruct the operator to run gdaemon schema apply --destructive. crates/gdaemon/src/main.rs::apply_schema verifies the backup and applies destructive migrations without discovering or requiring an epoch, so the documented path bypasses the fence and reopens the backstop-to-DROP race.","finding_id":"APR5-002","fix":"Replace both direct gdaemon instructions with gobby hub-maintenance run schema-apply. Add crates/gdaemon/src/main.rs::apply_schema and its contract tests to § 7.1, then remove the raw destructive path or require an epoch-bound invocation token/connection so direct execution cannot bypass daemon stop, backend quiescence, and the reconnect fence.","introduced_in_round":4,"location":"Constraints; § 7.1; E1 End-to-End Verification","prevention":"Sweep every destructive operator recipe and executable entry point together; prove direct invocation outside an orchestrator-owned maintenance epoch is rejected.","principle":"Every reachable destructive entry point must enforce hub quiescence before the preflight-and-drop sequence, and operator recipes must route through that enforcing entry point.","root_cause":"APR4-008 repaired the P7 safety argument around the Python maintenance orchestrator while leaving direct gdaemon destructive commands and a raw Rust entry point that authorizes destructive work from backup verification alone.","section_id":"7.1","severity":"blocking"}],"reviewer_session":"c21c4a4b-6c55-413c-98f0-525408988521","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"24384cfe-8cb0-4aaa-bfaa-5b86560252aa"}
```


## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Domain-table DDL in the revisioned baseline
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Baseline contains the eight new tables (including legacy_copy_ledger)
    with partial unique live-name indexes and `enabled_pinned` on the four definition
    domains, and retains the legacy tables. file: `crates/gcore/assets/schema/baseline.sql`.

    1.1.2: The schema-artifact lockstep is re-armed: the refresh-statement acceptance
    in its implementation module enumerates exactly the added statements and the predecessor
    fixture matches its pinned checksum. file: `crates/gcore/src/baseline_refresh.rs`.
    file: `crates/gcore/src/schema/runner.rs`. file: `crates/gcore/src/schema/runner_tests.rs`.

    1.1.3: The regenerated catalog manifest carries the eight tables and the freshness
    test passes without the update flag; the regenerated expected identity matches
    the rebuilt gdaemon. file: `crates/gcore/assets/schema/catalog.manifest.json`.
    file: `src/gobby/storage/schema_expected_identity.json`.

    1.1.4: The applied-schema catalog pins the eight tables, the reconciliation columns,
    and the partial unique predicates. test: `tests/storage/test_domain_tables_schema.py`.'
  labels:
  - covers:split-workflow-definition-storage:1.1:1.1.1
  - covers:split-workflow-definition-storage:1.1:1.1.2
  - covers:split-workflow-definition-storage:1.1:1.1.3
  - covers:split-workflow-definition-storage:1.1:1.1.4
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Typed definition managers package
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.4'
  validation_criteria: "1.2.1: Shared scope/soft-delete/conflict utilities exist with\
    \ typed errors. file: `src/gobby/storage/definitions/_shared.py`.\n1.2.2: Rule\
    \ manager supports event/group listing with priority ordering. symbol: `RuleDefinitionManager`.\
    \ file: `src/gobby/storage/definitions/rules.py`.\n1.2.3: Variable-defaults manager\
    \ returns a typed defaults map. symbol: `SessionVariableDefaultManager`. file:\
    \ `src/gobby/storage/definitions/variables.py`.\n1.2.4: Pipeline manager covers\
    \ CRUD, duplicate, scope moves, canvas/version updates. symbol: `PipelineDefinitionManager`.\
    \ file: `src/gobby/storage/definitions/pipelines.py`.\n1.2.5: CRUD, scope fallback,\
    \ cross-domain same-name, same-domain live conflict, restore collision, and purge\
    \ behaviors are covered for rules. test: `tests/storage/definitions/test_rules_manager.py`.\n\
    1.2.6: The same behavior set is covered for variable defaults. test: `tests/storage/definitions/test_variables_manager.py`.\n\
    1.2.7: The same behavior set is covered for pipelines, including duplicate and\
    \ canvas/version updates. test: `tests/storage/definitions/test_pipelines_manager.py`.\n\
    1.2.8: The parameterized reconciliation contract holds for rules, variables, and\
    \ pipelines: user update/toggle stamps enabled_pinned, sync adopts a changed template\
    \ enabled default while unpinned, and sync preserves the user's value while pinned.\
    \ test: `tests/storage/definitions/test_enabled_reconciliation.py`.\n1.2.9: Manager\
    \ mutators bump post-commit only: a mutator that raises mid-transaction leaves\
    \ the persistent revision, the local counter, and the listeners untouched \u2014\
    \ including when the mutator runs nested inside a caller's ambient transaction\
    \ that later rolls back. test: `tests/storage/definitions/test_rules_manager.py`."
  labels:
  - covers:split-workflow-definition-storage:1.2:1.2.1
  - covers:split-workflow-definition-storage:1.2:1.2.2
  - covers:split-workflow-definition-storage:1.2:1.2.3
  - covers:split-workflow-definition-storage:1.2:1.2.4
  - covers:split-workflow-definition-storage:1.2:1.2.5
  - covers:split-workflow-definition-storage:1.2:1.2.6
  - covers:split-workflow-definition-storage:1.2:1.2.7
  - covers:split-workflow-definition-storage:1.2:1.2.8
  - covers:split-workflow-definition-storage:1.2:1.2.9
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Agent definition manager with step-workflow child
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: "1.3.1: Reads hydrate the nested step_workflow from the child\
    \ table in one query. symbol: `AgentDefinitionManager`. file: `src/gobby/storage/definitions/agents.py`.\n\
    1.3.2: `upsert_with_steps` writes parent and child atomically, deleting the child\
    \ when steps are removed. test: `tests/storage/definitions/test_agents_manager.py::test_upsert_with_steps_atomic`.\n\
    1.3.3: Child cascade on parent hard-delete and orphan-free child lifecycle are\
    \ covered. test: `tests/storage/definitions/test_agents_manager.py`.\n1.3.6: Soft\
    \ delete leaves the child row in place and a delete\u2192restore round trip returns\
    \ the parent with its step workflow payload intact. test: `tests/storage/definitions/test_agents_manager.py::test_soft_delete_restore_preserves_child`.\n\
    1.3.4: A child-only create, update, or delete bumps both the agent_step_workflows\
    \ and agents revisions after commit; a rolled-back child write bumps neither.\
    \ test: `tests/storage/definitions/test_agents_manager.py`.\n1.3.5: AgentDefinitionManager\
    \ satisfies the parameterized enabled_pinned reconciliation contract. test: `tests/storage/definitions/test_enabled_reconciliation.py`."
  labels:
  - covers:split-workflow-definition-storage:1.3:1.3.1
  - covers:split-workflow-definition-storage:1.3:1.3.2
  - covers:split-workflow-definition-storage:1.3:1.3.3
  - covers:split-workflow-definition-storage:1.3:1.3.6
  - covers:split-workflow-definition-storage:1.3:1.3.4
  - covers:split-workflow-definition-storage:1.3:1.3.5
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Domain cache revisions with cross-daemon invalidation
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: "1.4.1: Per-domain counters, the persistent advance/notify\
    \ half, and the listener registry exist with thread-safety. file: `src/gobby/storage/definitions/revisions.py`.\n\
    1.4.2: Bumping one domain fires only that domain's listeners and leaves other\
    \ domains' revisions unchanged. test: `tests/storage/definitions/test_revisions.py`.\n\
    1.4.3: A committed advance_persistent_revision advances definition_revisions and\
    \ delivers exactly one notification; a rolled-back transaction leaves the table\
    \ unchanged and delivers none. test: `tests/storage/definitions/test_revisions.py`.\n\
    1.4.4: The listener service maps observed persistent revisions into local bumps,\
    \ and poll-healing recovers a missed notification. file: `src/gobby/storage/definitions/notifications.py`.\
    \ test: `tests/storage/definitions/test_revisions.py`.\n1.4.5: A definition mutation\
    \ through daemon A is observed by daemon B without a restart, in an isolated two-daemon\
    \ cluster. test: `tests/integration/definitions/test_definition_revisions_multi_daemon.py`.\n\
    1.4.6: The listener service has a complete lifecycle: synchronous storage init\
    \ only constructs it, the async stateful-services phase starts it, the graceful-shutdown\
    \ tail closes it cancelling both tasks and closing the LISTEN connection, and\
    \ a listen-task crash reconnects with poll-healing covering the gap \u2014 proven\
    \ through the injectable connection-factory fake. file: `src/gobby/runner_init/services.py`.\
    \ file: `src/gobby/runner_lifecycle_shutdown.py`. test: `tests/storage/definitions/test_revisions.py`.\n\
    1.4.8: Direct synchronous GobbyRunner construction succeeds with no event loop,\
    \ and a construction failure after listener creation rolls the listener back with\
    \ the other runner resources. file: `src/gobby/runner_rollback.py`. test: `tests/test_runner_init.py`.\n\
    1.4.7: A mutation nested inside an outer ambient transaction bumps and notifies\
    \ exactly once when the outer transaction commits, and neither bumps, notifies,\
    \ nor fires listeners when the outer transaction rolls back. test: `tests/storage/definitions/test_revisions.py::test_ambient_nested_commit_visibility`."
  labels:
  - covers:split-workflow-definition-storage:1.4:1.4.1
  - covers:split-workflow-definition-storage:1.4:1.4.2
  - covers:split-workflow-definition-storage:1.4:1.4.3
  - covers:split-workflow-definition-storage:1.4:1.4.4
  - covers:split-workflow-definition-storage:1.4:1.4.5
  - covers:split-workflow-definition-storage:1.4:1.4.6
  - covers:split-workflow-definition-storage:1.4:1.4.8
  - covers:split-workflow-definition-storage:1.4:1.4.7
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Re-arm the embedded migration machinery
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.5.1: The migrations asset directory and include_str wiring
    exist with versions above the baseline and pinned checksums. file: `crates/gcore/src/schema/assets.rs`.

    1.5.2: A destructive migration on a fresh lineage is receipt-stamped without executing,
    and still refuses without authorization on an existing lineage. file: `crates/gcore/src/schema/runner_tests.rs`.

    1.5.3: A guarded non-destructive migration applies on fresh and predecessor lineages
    and re-applies as a receipted no-op. file: `crates/gcore/src/schema/runner_tests.rs`.'
  labels:
  - covers:split-workflow-definition-storage:1.5:1.5.1
  - covers:split-workflow-definition-storage:1.5:1.5.2
  - covers:split-workflow-definition-storage:1.5:1.5.3
  tdd: true
  source_section: '1.5'
  implementation_domain: backend
- title: Model split and nested step_workflow model
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  - '1.4'
  - '1.5'
  validation_criteria: '2.1.1: AgentStepWorkflowBody exists and AgentDefinitionBody
    carries the optional nested step_workflow field alongside the still-present legacy
    fields. symbol: `AgentStepWorkflowBody`. file: `src/gobby/workflows/agent_models.py`.

    2.1.2: Pipeline models live in their own module with definitions.py re-exports
    intact. file: `src/gobby/workflows/pipeline_models.py`.

    2.1.3: definitions.py is under 1,000 lines after the split. file: `src/gobby/workflows/definitions.py`.

    2.1.5: Model validation round-trips nested YAML (stepful and step-less). test:
    `tests/workflows/test_agent_models.py::test_step_workflow_nesting`.'
  labels:
  - covers:split-workflow-definition-storage:2.1:2.1.1
  - covers:split-workflow-definition-storage:2.1:2.1.2
  - covers:split-workflow-definition-storage:2.1:2.1.3
  - covers:split-workflow-definition-storage:2.1:2.1.5
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Atomic model-and-YAML step_workflow cutover
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: All 21 stepful agent YAMLs use the nested step_workflow
    shape and none of the 25 bundled files carries top-level steps/step_variables/exit_condition.
    file: `src/gobby/install/shared/workflows/agents/planner.yaml`.

    2.2.2: Every rewritten YAML still validates through AgentDefinitionBody with a
    populated step_workflow, and all 25 bundled definitions load under the new model.
    test: `tests/agents/test_sync.py::test_bundled_agents_nested_step_workflow`.

    2.2.3: The bundled-agent contract suite reads steps from the nested key and passes
    against the rewritten YAML in the same commit. test: `tests/dispatch/test_bundled_agent_contract.py`.

    2.2.4: AgentDefinitionBody carries no top-level step fields, and validating a
    body with top-level steps, step_variables, or exit_condition raises with a message
    naming the nested replacement key. symbol: `AgentDefinitionBody`. file: `src/gobby/workflows/agent_models.py`.
    test: `tests/workflows/test_agent_models.py::test_legacy_step_keys_rejected`.

    2.2.5: Every direct-access site reads through step_workflow and handles the step-less
    case, across the CLI, dispatch, persona, and spawn readers. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.
    file: `src/gobby/cli/agents.py`. file: `src/gobby/dispatch/spawn.py`.

    2.2.6: Agent-shape tests and mocks assert the nested shape with no residual top-level
    step fields, including direct AgentDefinitionBody constructors that passed removed
    fields and the field-inventory assertions, which move to the nested body rather
    than being deleted. test: `tests/agents/test_discovery_agents.py`. test: `tests/dispatch/test_skill_composition.py`.
    test: `tests/workflows/test_agent_definitions_v2.py`.

    2.2.7: The bundled-definition contract suites and raw step fixtures read the nested
    shape and none asserts a top-level steps or step_variables key. test: `tests/agents/test_qa_reviewer_definition.py`.
    test: `tests/agents/test_merge_orchestrator_contract.py`. test: `tests/agents/test_sync.py`.
    test: `tests/agents/test_plan_adversary_taskless_definition.py`.

    2.2.8: The bundled content manifest is regenerated in the same commit and its
    freshness test passes without an update flag. file: `src/gobby/install/bundled_content_manifest.json`.
    test: `tests/install/test_bundled_content_manifest.py`.

    2.2.9: Scaffolded register_agent_step_workflow reads the nested shape. symbol:
    `register_agent_step_workflow`. file: `src/gobby/agents/step_workflow.py`.'
  labels:
  - covers:split-workflow-definition-storage:2.2:2.2.1
  - covers:split-workflow-definition-storage:2.2:2.2.2
  - covers:split-workflow-definition-storage:2.2:2.2.3
  - covers:split-workflow-definition-storage:2.2:2.2.4
  - covers:split-workflow-definition-storage:2.2:2.2.5
  - covers:split-workflow-definition-storage:2.2:2.2.6
  - covers:split-workflow-definition-storage:2.2:2.2.7
  - covers:split-workflow-definition-storage:2.2:2.2.8
  - covers:split-workflow-definition-storage:2.2:2.2.9
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Agent sync, write surfaces, and agent copy migration
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.3.1: Agent sync upserts parent and child in one transaction
    and no longer manages step data in the parent body. symbol: `sync_bundled_agents`.
    file: `src/gobby/agents/sync.py`.

    2.3.2: MCP agent CRUD operates on the typed manager with a nested step_workflow
    surface. symbol: `update_agent_step_workflow`. file: `src/gobby/mcp_proxy/tools/workflows/_agents.py`.

    2.3.3: HTTP agent definition routes read and write the typed tables. file: `src/gobby/servers/routes/agents.py`.

    2.3.4: Copy migration migrates every agent row and one child per row carrying
    a non-empty steps array (29 and 25 at planning time), preserves soft-deleted rows,
    skips the four `"steps": null` rows without a scalar-length error, and fails loudly
    on count mismatch. test: `tests/storage/test_agent_copy_migration.py`.

    2.3.5: Sync produces child workflows for all 21 stepful bundled agents, none for
    the 4 step-less, and leaves no stale child rows (filesystem-derived counts; the
    29-row/25-child hub-derived counts belong to the copy migration in 2.3.4 and E1).
    test: `tests/agents/test_sync.py`.

    2.3.6: The equivalence guard succeeds idempotently on an identical pre-existing
    typed row and fails loudly on a divergent one. test: `tests/storage/test_agent_copy_migration.py`.

    2.3.9: Rerunning the copy over an already-migrated soft-deleted agent row completes
    without a primary-key abort, and two soft-deleted rows sharing a natural key each
    match their own target by preserved id. test: `tests/storage/test_agent_copy_migration.py::test_rerun_over_soft_deleted_rows`.

    2.3.7: A public agent write carrying legacy top-level step keys is rejected instead
    of silently dropping the step workflow. test: `tests/servers/routes/test_agents_routes.py`.

    2.3.8: Template hashing reads the nested body shape, so a step_workflow edit registers
    as drift. symbol: `TemplateHashCache._load_agents`. file: `src/gobby/workflows/template_hashes.py`.

    2.3.10: No generic surface can create or mutate a legacy agent row post-cutover:
    the generic HTTP routes, generic MCP tools, and the import path each reject kind
    `agent` naming the surviving domain surface. test: `tests/servers/routes/test_workflows.py`.
    test: `tests/mcp_proxy/tools/test_workflow_crud.py`. test: `tests/workflows/test_imports.py`.

    2.3.11: Bundled agent sync reaches the typed table through update_from_sync: a
    changed template enabled default is adopted on an untouched row and preserved
    on a pinned row. test: `tests/agents/test_sync.py`.

    2.3.12: The pinned schema root hashes and the release-pinned expected identity
    match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`.
    file: `src/gobby/storage/schema_expected_identity.json`.

    2.3.13: The copy migration strips and extracts both the flat pre-2.2 shape and
    the nested 2.2 shape equivalently: a row synced by the 2.2 cutover then migrated
    yields a stripped parent and a correct child, and mixed-shape populations migrate
    without child loss or child data retained in a parent body. test: `tests/storage/test_agent_copy_migration.py::test_nested_and_flat_source_shapes`.

    2.3.14: The copy migration writes one legacy_copy_ledger row per copied agent
    source row with the normalized payload hash, and reruns keep the copy-time hash.
    test: `tests/storage/test_agent_copy_migration.py`.

    2.3.15: The copy migration holds ACCESS EXCLUSIVE on workflow_definitions: a concurrent
    second-connection agent-row write blocks until the migration commits, and its
    post-commit landing is the post-copy drift the 7.1 backstop refuses. test: `tests/storage/test_agent_copy_migration.py::test_copy_lock_fences_concurrent_writes`.'
  labels:
  - covers:split-workflow-definition-storage:2.3:2.3.1
  - covers:split-workflow-definition-storage:2.3:2.3.2
  - covers:split-workflow-definition-storage:2.3:2.3.3
  - covers:split-workflow-definition-storage:2.3:2.3.4
  - covers:split-workflow-definition-storage:2.3:2.3.5
  - covers:split-workflow-definition-storage:2.3:2.3.6
  - covers:split-workflow-definition-storage:2.3:2.3.9
  - covers:split-workflow-definition-storage:2.3:2.3.7
  - covers:split-workflow-definition-storage:2.3:2.3.8
  - covers:split-workflow-definition-storage:2.3:2.3.10
  - covers:split-workflow-definition-storage:2.3:2.3.11
  - covers:split-workflow-definition-storage:2.3:2.3.12
  - covers:split-workflow-definition-storage:2.3:2.3.13
  - covers:split-workflow-definition-storage:2.3:2.3.14
  - covers:split-workflow-definition-storage:2.3:2.3.15
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Agent read-consumer rewiring
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: '2.4.1: resolve_agent resolves via the typed manager with hydrated
    step_workflow; the row-returning variant exists. symbol: `resolve_agent`. file:
    `src/gobby/workflows/agent_resolver.py`.

    2.4.2: RuleEngine agent cache keys on the agents domain revision. symbol: `RuleEngine`.
    file: `src/gobby/workflows/engine/core.py`.

    2.4.3: Dispatch agent loading reads the typed manager. file: `src/gobby/dispatch/context.py`.

    2.4.4: agents/dry_run.py uses resolve_agent; the untyped name lookup is gone.
    file: `src/gobby/agents/dry_run.py`.

    2.4.5: Agent resolution, dry-run, and required-skills composition behave identically
    for stepful and step-less agents. test: `tests/workflows/test_agent_resolver.py`.

    2.4.6: Expansion agent loading reads the typed manager across the common loader,
    the service, and the compiler. file: `src/gobby/tasks/expansion/_common.py`. file:
    `src/gobby/tasks/expansion_service.py`. file: `src/gobby/tasks/expansion/_compile.py`.

    2.4.7: The CLI agent listing and detail dict read the typed manager and emit the
    nested step_workflow key. file: `src/gobby/cli/agents.py`.

    2.4.8: dry_run.py is under 1,000 lines after the trace extraction and the agent
    rewrite. file: `src/gobby/workflows/dry_run.py`. file: `src/gobby/workflows/dry_run_trace.py`.

    2.4.9: A child-only step-workflow edit or delete invalidates the cached hydrated
    agent, and the next resolution returns the updated body. test: `tests/workflows/test_agent_resolver.py`.'
  labels:
  - covers:split-workflow-definition-storage:2.4:2.4.1
  - covers:split-workflow-definition-storage:2.4:2.4.2
  - covers:split-workflow-definition-storage:2.4:2.4.3
  - covers:split-workflow-definition-storage:2.4:2.4.4
  - covers:split-workflow-definition-storage:2.4:2.4.5
  - covers:split-workflow-definition-storage:2.4:2.4.6
  - covers:split-workflow-definition-storage:2.4:2.4.7
  - covers:split-workflow-definition-storage:2.4:2.4.8
  - covers:split-workflow-definition-storage:2.4:2.4.9
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Step instance model and manager
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  validation_criteria: '3.1.1: AgentStepInstance and its manager exist with one-instance-per-session
    upsert semantics. symbol: `AgentStepInstanceManager`. file: `src/gobby/workflows/step_instances.py`.

    3.1.2: Snapshot, lineage id, and created_at are immutable across saves. test:
    `tests/workflows/test_step_instances.py::test_snapshot_immutable_on_upsert`.

    3.1.3: AgentStepInstanceMutation replaces WorkflowInstanceMutation in the hub
    protocol. symbol: `AgentStepInstanceMutation`. file: `src/gobby/storage/hub/protocol.py`.

    3.1.4: `replace_for_session` swaps snapshot, lineage id, agent name, and step
    position together, and is the only mutator that changes snapshot, lineage, or
    agent identity. test: `tests/workflows/test_step_instances.py::test_replace_for_session_swaps_snapshot_and_lineage`.

    3.1.5: A step-scope variable merge concurrent with an enforcement save is not
    lost. test: `tests/workflows/test_step_instances.py::test_merge_variables_serializes_against_save`.

    3.1.6: The mutation lock is re-entrant through the real Postgres adapter with
    ambient nesting on one shared adapter instance: a caller-held section wrapping
    a read and its computed save does not deadlock the mutators, and a merge committed
    outside that section cannot interleave into it. test: `tests/workflows/test_step_instances.py::test_mutation_lock_is_reentrant`.

    3.1.7: A save carrying a compare-and-set precondition from a pre-persona read
    is rejected as stale rather than rewriting the replaced instance''s step position
    and variables. test: `tests/workflows/test_step_instances.py::test_stale_save_after_persona_replacement_rejected`.

    3.1.8: A save whose agent_name differs from the stored row is rejected as a stale
    identity write, with or without the compare-and-set precondition. test: `tests/workflows/test_step_instances.py::test_save_rejects_agent_identity_change`.'
  labels:
  - covers:split-workflow-definition-storage:3.1:3.1.1
  - covers:split-workflow-definition-storage:3.1:3.1.2
  - covers:split-workflow-definition-storage:3.1:3.1.3
  - covers:split-workflow-definition-storage:3.1:3.1.4
  - covers:split-workflow-definition-storage:3.1:3.1.5
  - covers:split-workflow-definition-storage:3.1:3.1.6
  - covers:split-workflow-definition-storage:3.1:3.1.7
  - covers:split-workflow-definition-storage:3.1:3.1.8
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Data-plane cutover and instance copy migration
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: "3.2.1: Spawn creates the per-session snapshot instance and\
    \ no generated-row registration remains in the spawn path. file: `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py`.\
    \ file: `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`. file: `src/gobby/dispatch/spawn.py`.\n\
    3.2.1a: Persona activation creates, preserves, or removes the instance on the\
    \ public apply_persona_impl path. symbol: `apply_persona_impl`. file: `src/gobby/mcp_proxy/tools/apply_persona.py`.\n\
    3.2.2: agents/step_workflow.py is deleted and the sync scaffolding call is gone.\
    \ file: `src/gobby/agents/step_workflow.py`. file: `src/gobby/agents/sync.py`.\n\
    3.2.3: Enforcement reads resolve the step from the instance snapshot in one row\
    \ lookup. symbol: `EnforcementCheckMixin._get_step_for_session`. file: `src/gobby/workflows/engine/enforcement_checks.py`.\n\
    3.2.3a: Transition and completion writers read and write the snapshot instance\
    \ with agent_name in place of workflow_name. file: `src/gobby/workflows/engine/enforcement_handlers.py`.\
    \ file: `src/gobby/workflows/engine/enforcement_completion.py`. file: `src/gobby/workflows/engine/enforcement.py`.\n\
    3.2.3b: Step context and the coordinator completion gate read the snapshot instead\
    \ of the definition manager. file: `src/gobby/workflows/step_context.py`. file:\
    \ `src/gobby/hooks/session_coordinator.py`.\n3.2.4: The instance copy migration\
    \ preserves live-session step state with a valid snapshot and fails loudly when\
    \ none is recoverable. test: `tests/storage/test_instance_copy_migration.py`.\n\
    3.2.5: _step_workflow_name is gone from the spawn, persona, context-injection,\
    \ and idle-reprompt writers. file: `src/gobby/workflows/hooks.py`. file: `src/gobby/agents/idle_check_handler.py`.\
    \ file: `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py`. file: `src/gobby/mcp_proxy/tools/apply_persona.py`.\n\
    3.2.6: spawn_agent/_implementation.py is under 1,000 lines after extraction. file:\
    \ `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.\n3.2.7: The equivalence\
    \ guard fails on the generated-row branch when snapshot or step position diverges,\
    \ and fails on both branches when current_step is absent from the copied snapshot's\
    \ steps, including a generated row whose refresh removed the active step. test:\
    \ `tests/storage/test_instance_copy_migration.py`.\n3.2.8: A failed step-instance\
    \ save aborts before any child process starts or task is claimed, deletes the\
    \ child session and agent-run rows created by pre-launch preparation, and aborts\
    \ the persona switch with the prior instance intact. test: `tests/workflows/test_step_snapshot_semantics.py`.\n\
    3.2.9: A handoff_ready session keeps its step position and variables through the\
    \ migration. test: `tests/storage/test_instance_copy_migration.py::test_handoff_ready_session_continuity`.\n\
    3.2.10: Variables, both action counters, timestamps, enabled, and context_injected\
    \ survive the copy with nullable legacy values normalized; agent_step_workflow_id\
    \ is populated wherever the typed child exists and left NULL for a valid snapshot\
    \ whose child does not, while a row with neither lineage nor a recoverable snapshot\
    \ fails the migration. test: `tests/storage/test_instance_copy_migration.py::test_runtime_field_equivalence`.\
    \ test: `tests/storage/test_instance_copy_migration.py::test_definitionless_snapshot_migrates_with_null_lineage`.\n\
    3.2.11: A persona switch to a different agent replaces snapshot and lineage together;\
    \ a switch to a step-less agent removes the instance. test: `tests/workflows/test_step_snapshot_semantics.py`.\n\
    3.2.12: RuleEngine constructs AgentStepInstanceManager in the same commit as the\
    \ typed readers. symbol: `RuleEngine`. file: `src/gobby/workflows/engine/core.py`.\n\
    3.2.13: The caller calls prepare_terminal_spawn and saves the step instance before\
    \ launch, so the instance row is durable before any provider process exists. symbol:\
    \ `prepare_terminal_spawn`. file: `src/gobby/agents/spawn.py`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.\n\
    3.2.13a: SpawnRequest carries the prepared spawn and every provider path consumes\
    \ it without creating a second session across all five executor call sites. file:\
    \ `src/gobby/agents/spawn_models.py`. file: `src/gobby/agents/spawn_executor.py`.\n\
    3.2.13b: The spawn, executor, droid, SRT, resume, and execution suites are retargeted\
    \ to the moved boundary and still pin agent_run persistence and per-provider spawn\
    \ context. test: `tests/agents/test_spawn.py`. test: `tests/agents/test_spawn_executor.py`.\
    \ test: `tests/agents/test_spawn_executor_droid.py`. test: `tests/agents/test_srt_spawn.py`.\
    \ test: `tests/agents/test_resume_executor.py`. test: `tests/mcp_proxy/tools/spawn_agent/test_execution.py`.\n\
    3.2.13c: The resume path keeps its inline prepare_terminal_resume call, and a\
    \ daemon-stop resume returns on the same child session with its retained typed\
    \ instance at the same step and variables \u2014 the #18974 continuity contract\
    \ holds across the storage cutover. symbol: `resume_agent_run`. file: `src/gobby/agents/resume_executor.py`.\
    \ test: `tests/agents/test_spawn_prepare_resume.py`.\n3.2.21: Persona tests are\
    \ retargeted off _step_workflow_name and WorkflowInstanceManager onto the typed\
    \ instance. test: `tests/mcp_proxy/tools/test_apply_persona.py`.\n3.2.22: The\
    \ spawn factory suite's self-healing registration tests are retargeted at prepared\
    \ snapshot creation, since the symbol they import is deleted in this task. test:\
    \ `tests/mcp_proxy/tools/spawn_agent/test_factory.py`.\n3.2.14: cleanup_failed_spawn\
    \ terminates the recorded PID and tmux session before deleting the child session\
    \ row, and tolerates an already-dead process. symbol: `cleanup_failed_spawn`.\
    \ file: `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py`.\n3.2.14a:\
    \ Provider process identity is available to cleanup at every post-launch failure\
    \ point, and the tmux live-pane verification failure routes through cleanup instead\
    \ of falling through. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.\n\
    3.2.14b: Fault injection at lease attach, live-pane verification, run start, and\
    \ the post-claim update each leave no surviving PID, no tmux session, and no attached\
    \ lease. test: `tests/workflows/test_step_snapshot_semantics.py::test_post_launch_faults_leave_no_live_process`.\n\
    3.2.15: An auto-claimed spawn reaches the same effective initial step and variables\
    \ as before the reordering, via the atomic post-claim update. test: `tests/workflows/test_step_snapshot_semantics.py::test_auto_claimed_spawn_initial_step_preserved`.\n\
    3.2.16: A persona switch commits instance replacement and the _agent_type variable\
    \ merge in one immediate transaction with the instance lock outermost; a fault\
    \ in either leaves neither applied, for stepful and step-less targets. test: `tests/workflows/test_step_snapshot_semantics.py::test_persona_switch_is_atomic_across_rows`.\n\
    3.2.17: Persona activation targeting the same agent with no existing instance\
    \ creates a fresh snapshot rather than reporting a successful no-op, and a persistence\
    \ fault on that branch fails the activation. test: `tests/workflows/test_step_snapshot_semantics.py::test_persona_same_agent_missing_instance_creates_snapshot`.\n\
    3.2.18: A persona failure on the web-chat path propagates: the runtime is stopped,\
    \ the session is not registered, and the caller does not report success. file:\
    \ `src/gobby/servers/websocket/chat/_session.py`.\n3.2.19: Enforcement read-compute-write\
    \ pairs hold one mutation section, so a concurrent step-scope merge cannot be\
    \ lost across a transition or completion write. test: `tests/workflows/test_step_snapshot_semantics.py::test_enforcement_write_paths_hold_one_critical_section`.\n\
    3.2.20: A live session whose only legacy instance is disabled migrates with enabled\
    \ preserved, and activation neither re-enables it nor rewinds its step. test:\
    \ `tests/storage/test_instance_copy_migration.py::test_disabled_instance_continuity`.\n\
    3.2.23: The pinned schema root hashes and the release-pinned expected identity\
    \ match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`.\
    \ file: `src/gobby/storage/schema_expected_identity.json`.\n3.2.24: Fault injection\
    \ at every pre-launch boundary \u2014 after each acquisition inside preparation\
    \ (child session, initial variables, run row and prompt file, credential), after\
    \ the instance save, and between save and launch \u2014 leaves no session, variable,\
    \ run, or instance rows, no prompt file, and no live credentials, and running\
    \ the cleanup owner twice is safe. test: `tests/workflows/test_step_snapshot_semantics.py::test_prelaunch_faults_leave_no_rows`.\
    \ test: `tests/agents/test_spawn.py`.\n3.2.25: Candidate selection is deterministic\
    \ within the resolved active identity: equal-timestamp duplicates resolve by the\
    \ id tie-break, child lineage resolves project-first with global fallback, a non-qualifying\
    \ instance row on a live session fails loudly, and a live session with no qualifying\
    \ row and no persona state migrates nothing. test: `tests/storage/test_instance_copy_migration.py::test_candidate_resolution_determinism`.\n\
    3.2.26: The enforcement, transition, audit, error-code, coordinator, step-context,\
    \ and completion-gate suites construct the typed instance manager with no WorkflowInstanceManager\
    \ import or patch remaining. test: `tests/workflows/test_step_enforcement.py`.\
    \ test: `tests/workflows/test_step_runtime_transitions.py`. test: `tests/workflows/test_step_enforcement_audit.py`.\
    \ test: `tests/workflows/test_step_error_codes.py`. test: `tests/hooks/test_session_coordinator.py`.\
    \ test: `tests/workflows/test_step_context.py`. test: `tests/workflows/test_agent_workflow_completion.py`.\n\
    3.2.27: The instance copy resolves the active identity from _agent_type alone:\
    \ an A\u2192B\u2192A persona history with a stale-newer B row migrates A's snapshot\
    \ and agent_name; an A\u2192B switch with a stale A-steps _step_workflow_name\
    \ and no B row migrates nothing for stepful and step-less B alike, without RAISEing;\
    \ qualifying rows with no _agent_type fail loudly; and ordering applies only within\
    \ the resolved identity's rows. test: `tests/storage/test_instance_copy_migration.py::test_active_identity_resolution`.\n\
    3.2.28: The dispatcher spawn test asserts _step_workflow_name is absent from initial\
    \ variables while still pinning the selected agent and task identity. test: `tests/dispatch/test_dispatcher.py::test_spawn_action_uses_services_and_records_agent_run`.\n\
    3.2.29: apply_persona rejects caller variables colliding with the persona delta,\
    \ the task overlay, or the runtime-reserved set before the transition transaction\
    \ opens, leaving session variables and the typed instance unchanged, at both the\
    \ tool wrapper and impl seams, while non-reserved variables still merge. test:\
    \ `tests/mcp_proxy/tools/test_apply_persona.py`. test: `tests/workflows/test_step_snapshot_semantics.py`.\n\
    3.2.30: The instance copy holds ACCESS EXCLUSIVE on workflow_instances and installs\
    \ the legacy write-rejection trigger: a concurrent second-connection write blocks\
    \ at the lock and then fails on the trigger, a post-commit write fails on the\
    \ trigger, and typed rows are untouched in both cases. test: `tests/storage/test_instance_copy_migration.py::test_legacy_write_fence`."
  labels:
  - covers:split-workflow-definition-storage:3.2:3.2.1
  - covers:split-workflow-definition-storage:3.2:3.2.1a
  - covers:split-workflow-definition-storage:3.2:3.2.2
  - covers:split-workflow-definition-storage:3.2:3.2.3
  - covers:split-workflow-definition-storage:3.2:3.2.3a
  - covers:split-workflow-definition-storage:3.2:3.2.3b
  - covers:split-workflow-definition-storage:3.2:3.2.4
  - covers:split-workflow-definition-storage:3.2:3.2.5
  - covers:split-workflow-definition-storage:3.2:3.2.6
  - covers:split-workflow-definition-storage:3.2:3.2.7
  - covers:split-workflow-definition-storage:3.2:3.2.8
  - covers:split-workflow-definition-storage:3.2:3.2.9
  - covers:split-workflow-definition-storage:3.2:3.2.10
  - covers:split-workflow-definition-storage:3.2:3.2.11
  - covers:split-workflow-definition-storage:3.2:3.2.12
  - covers:split-workflow-definition-storage:3.2:3.2.13
  - covers:split-workflow-definition-storage:3.2:3.2.13a
  - covers:split-workflow-definition-storage:3.2:3.2.13b
  - covers:split-workflow-definition-storage:3.2:3.2.13c
  - covers:split-workflow-definition-storage:3.2:3.2.21
  - covers:split-workflow-definition-storage:3.2:3.2.22
  - covers:split-workflow-definition-storage:3.2:3.2.14
  - covers:split-workflow-definition-storage:3.2:3.2.14a
  - covers:split-workflow-definition-storage:3.2:3.2.14b
  - covers:split-workflow-definition-storage:3.2:3.2.15
  - covers:split-workflow-definition-storage:3.2:3.2.16
  - covers:split-workflow-definition-storage:3.2:3.2.17
  - covers:split-workflow-definition-storage:3.2:3.2.18
  - covers:split-workflow-definition-storage:3.2:3.2.19
  - covers:split-workflow-definition-storage:3.2:3.2.20
  - covers:split-workflow-definition-storage:3.2:3.2.23
  - covers:split-workflow-definition-storage:3.2:3.2.24
  - covers:split-workflow-definition-storage:3.2:3.2.25
  - covers:split-workflow-definition-storage:3.2:3.2.26
  - covers:split-workflow-definition-storage:3.2:3.2.27
  - covers:split-workflow-definition-storage:3.2:3.2.28
  - covers:split-workflow-definition-storage:3.2:3.2.29
  - covers:split-workflow-definition-storage:3.2:3.2.30
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Recovery, cleanup, and auxiliary surfaces
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '3.3.1: Restart recovery rebuilds step state from the agent
    definition without any -steps name parsing. symbol: `_ensure_step_instance`. file:
    `src/gobby/hooks/session_activation.py`.

    3.3.2: Session-end and agent-terminal cleanup delete the per-session instance.
    file: `src/gobby/hooks/event_handlers/_session_end.py`. file: `src/gobby/agents/runtime_cleanup.py`.

    3.3.3: Workflow-scoped variable tools use the scope parameter against the single
    instance. file: `src/gobby/mcp_proxy/tools/workflows/_variables.py`.

    3.3.4: WorkflowInstance and WorkflowInstanceManager no longer exist. file: `src/gobby/workflows/state_manager.py`.

    3.3.5: _step_workflow_name is absent from reserved variables and all rule/variable
    plumbing. file: `src/gobby/workflows/reserved_variables.py`.

    3.3.6: Fresh-snapshot recovery emits one structured warning carrying the session,
    agent name, resolved definition ids, and a stable recovery marker. test: `tests/workflows/test_step_snapshot_semantics.py`.

    3.3.7: step_workflow_complete is seeded from the typed instance after recovery
    creates it, with no reference to the removed variable. test: `tests/hooks/test_session_activation_reconciliation.py::test_completion_seed_after_step_instance_recovery`.

    3.3.8: The MCP tool registrations and generic runtime-variable routes use the
    typed manager before WorkflowInstanceManager is deleted, so the tree imports at
    that commit. file: `src/gobby/servers/routes/workflows.py`. file: `src/gobby/mcp_proxy/tools/workflows/__init__.py`.

    3.3.9: The agent terminal-cleanup log no longer names workflow_instances. file:
    `src/gobby/agents/terminal_cleanup.py`.

    3.3.10: Session-end cleanup keeps the terminal-outcome gate, so a COMPACT or IDLE
    web-chat end retains the typed instance and only an expired end deletes it. file:
    `src/gobby/hooks/event_handlers/_session_end.py`. test: `tests/hooks/test_session_end_handlers.py`.

    3.3.12: In-place compact reactivation (#18994) leaves the typed instance keyed
    to the same session across a compact restart, with no ownership move and no legacy
    table named. file: `src/gobby/storage/session_lifecycle.py`. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.

    3.3.13: Orphan handoff expiry only flips status; the marker-gated retention sweep
    deletes typed instances for sessions expired past the revival horizon, with no
    legacy table named. symbol: `expire_orphaned_handoff_sessions`. symbol: `prune_stale_compact_workflow_instances`.
    file: `src/gobby/storage/session_lifecycle.py`.

    3.3.14: A compacted mid-workflow agent resumes on its same session at the same
    nonzero step with the same variables after the port. test: `tests/storage/sessions/test_lifecycle.py`.
    test: `tests/workflows/test_step_snapshot_semantics.py`.

    3.3.11: The spawn initial-variables suite queries the typed instance instead of
    importing the deleted WorkflowInstanceManager, dropping its `<agent>-steps` name
    arguments. test: `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py`.

    3.3.15: Daemon-stop terminal cleanup retains the typed instance and a resumed
    run on the same session sees the same step and variables; every other terminal
    reason deletes the instance. file: `src/gobby/agents/runtime_cleanup.py`. test:
    `tests/workflows/test_agent_workflow_runtime_cleanup.py`.

    3.3.16: Every legacy instance-manager test seam is deleted or rewritten onto the
    typed manager and no test imports or patches WorkflowInstanceManager. test: `tests/workflows/test_instance_manager.py`.
    test: `tests/workflows/test_session_end_cleanup.py`. test: `tests/workflows/test_session_variable_manager.py`.

    3.3.17: The termination tool result reports the cleanup count as agent_step_instances_deleted,
    the old key is gone, and the termination-result case asserts the renamed key.
    file: `src/gobby/mcp_proxy/tools/agents_termination.py`. test: `tests/mcp_proxy/tools/test_agents.py`.'
  labels:
  - covers:split-workflow-definition-storage:3.3:3.3.1
  - covers:split-workflow-definition-storage:3.3:3.3.2
  - covers:split-workflow-definition-storage:3.3:3.3.3
  - covers:split-workflow-definition-storage:3.3:3.3.4
  - covers:split-workflow-definition-storage:3.3:3.3.5
  - covers:split-workflow-definition-storage:3.3:3.3.6
  - covers:split-workflow-definition-storage:3.3:3.3.7
  - covers:split-workflow-definition-storage:3.3:3.3.8
  - covers:split-workflow-definition-storage:3.3:3.3.9
  - covers:split-workflow-definition-storage:3.3:3.3.10
  - covers:split-workflow-definition-storage:3.3:3.3.12
  - covers:split-workflow-definition-storage:3.3:3.3.13
  - covers:split-workflow-definition-storage:3.3:3.3.14
  - covers:split-workflow-definition-storage:3.3:3.3.11
  - covers:split-workflow-definition-storage:3.3:3.3.15
  - covers:split-workflow-definition-storage:3.3:3.3.16
  - covers:split-workflow-definition-storage:3.3:3.3.17
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Snapshot behavior regression suite
  category: test
  task_type: feature
  depends_on:
  - '3.3'
  validation_criteria: '3.4.1: All fourteen pinned behaviors pass against the snapshot
    runtime. test: `tests/workflows/test_step_snapshot_semantics.py`.

    3.4.2: Post-launch fault injection runs against the real spawn executor at all
    four failure points and proves no PID, tmux session, or attached lease survives.
    test: `tests/workflows/test_step_snapshot_semantics.py::test_post_launch_failure_terminates_process`.'
  labels:
  - covers:split-workflow-definition-storage:3.4:3.4.1
  - covers:split-workflow-definition-storage:3.4:3.4.2
  tdd: false
  source_section: '3.4'
  assigned_agent: backend-developer
- title: Rules cutover and copy migration
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  validation_criteria: '4.1.1: Rule evaluation loads through the typed rule manager
    with event/group filtering and priority order. symbol: `RuleEngine._load_rules`.
    file: `src/gobby/workflows/engine/core.py`.

    4.1.2: Bundled rule sync writes the typed table and the self-heal UPDATE is gone.
    file: `src/gobby/workflows/sync_rules.py`.

    4.1.3: Rule mutations invalidate the active-rule-names cache via the rules revision
    listener. file: `src/gobby/hooks/session_activation.py`.

    4.1.4: Copy migration migrates 160+ rules including soft-deleted rows with counts
    validated. test: `tests/storage/test_rule_copy_migration.py`.

    4.1.5: Rule HTTP routes behave identically on the typed manager. file: `src/gobby/servers/routes/rules.py`.
    test: `tests/servers/routes/test_rules_routes.py`.

    4.1.5a: Rule MCP tools and the rules CLI behave identically on the typed manager.
    file: `src/gobby/mcp_proxy/tools/workflows/_rules.py`. file: `src/gobby/cli/rules.py`.

    4.1.5b: The rule row type propagates as RuleDefinitionRow through effects, selectors,
    and the hook rule tuples. file: `src/gobby/workflows/engine/effects.py`. file:
    `src/gobby/workflows/selectors.py`. file: `src/gobby/workflows/hooks.py`.

    4.1.6: The equivalence guard fails when a pre-existing typed rule row diverges
    from its legacy source. test: `tests/storage/test_rule_copy_migration.py`.

    4.1.7: Rerunning the rule copy over already-migrated soft-deleted rows completes
    without a primary-key abort. test: `tests/storage/test_rule_copy_migration.py::test_rerun_over_soft_deleted_rows`.

    4.1.8: Rerunning the rule copy over already-migrated live rows is a clean no-op,
    and two soft-deleted rule rows sharing a natural key both migrate. test: `tests/storage/test_rule_copy_migration.py::test_rerun_over_live_rows`.

    4.1.9: A live typed rule row matching a legacy row on natural key and payload
    but carrying a different UUID fails the guard loudly. test: `tests/storage/test_rule_copy_migration.py::test_divergent_identity_fails`.

    4.1.10: EvaluationMixin and is_internal_rule accept RuleDefinitionRow, and no
    rule-path module imports WorkflowDefinitionRow. file: `src/gobby/workflows/engine/evaluation.py`.
    file: `src/gobby/workflows/reserved_variables.py`.

    4.1.11: No generic surface can create or mutate a legacy rule row post-cutover:
    the generic HTTP routes, generic MCP tools, and the import path each reject kind
    `rule` naming the surviving domain surface. test: `tests/servers/routes/test_workflows.py`.
    test: `tests/mcp_proxy/tools/test_workflow_crud.py`. test: `tests/workflows/test_imports.py`.

    4.1.12: Bundled rule sync reaches the typed table through update_from_sync: a
    changed template enabled default is adopted on an untouched row and preserved
    on a pinned row. test: `tests/workflows/test_rule_yaml_sync.py`.

    4.1.13: The pinned schema root hashes and the release-pinned expected identity
    match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`.
    file: `src/gobby/storage/schema_expected_identity.json`.

    4.1.14: The rule copy migration writes one legacy_copy_ledger row per copied source
    row and reruns keep the copy-time hash. test: `tests/storage/test_rule_copy_migration.py`.

    4.1.15: The rule-engine suite seeds rules through the typed manager and observes
    its own fixtures after the cutover. test: `tests/workflows/test_rule_engine.py`.

    4.1.16: The rule copy migration holds ACCESS EXCLUSIVE on workflow_definitions:
    a concurrent second-connection rule-row write blocks until the migration commits,
    and its post-commit landing is the post-copy drift the 7.1 backstop refuses. test:
    `tests/storage/test_rule_copy_migration.py::test_copy_lock_fences_concurrent_writes`.'
  labels:
  - covers:split-workflow-definition-storage:4.1:4.1.1
  - covers:split-workflow-definition-storage:4.1:4.1.2
  - covers:split-workflow-definition-storage:4.1:4.1.3
  - covers:split-workflow-definition-storage:4.1:4.1.4
  - covers:split-workflow-definition-storage:4.1:4.1.5
  - covers:split-workflow-definition-storage:4.1:4.1.5a
  - covers:split-workflow-definition-storage:4.1:4.1.5b
  - covers:split-workflow-definition-storage:4.1:4.1.6
  - covers:split-workflow-definition-storage:4.1:4.1.7
  - covers:split-workflow-definition-storage:4.1:4.1.8
  - covers:split-workflow-definition-storage:4.1:4.1.9
  - covers:split-workflow-definition-storage:4.1:4.1.10
  - covers:split-workflow-definition-storage:4.1:4.1.11
  - covers:split-workflow-definition-storage:4.1:4.1.12
  - covers:split-workflow-definition-storage:4.1:4.1.13
  - covers:split-workflow-definition-storage:4.1:4.1.14
  - covers:split-workflow-definition-storage:4.1:4.1.15
  - covers:split-workflow-definition-storage:4.1:4.1.16
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Variables cutover and copy migration
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: One helper feeds all four default-application paths
    with identical visibility, each path resolving its session''s project_id, with
    project-first global-fallback deduplication. symbol: `load_variable_defaults`.
    file: `src/gobby/workflows/variable_defaults.py`.

    4.2.2: The session-variables TTL cache is keyed by project_id and the variables
    revision, and invalidates on the variables domain revision. file: `src/gobby/workflows/state_manager.py`.

    4.2.3: Variable sync writes typed columns. file: `src/gobby/workflows/sync_variables.py`.

    4.2.3a: Variable-definition MCP CRUD reads and writes typed columns. file: `src/gobby/mcp_proxy/tools/workflows/_variables.py`.

    4.2.4: Copy migration lands 42 variable rows including the normalized source anomaly.
    test: `tests/storage/test_variable_copy_migration.py`.

    4.2.5: The equivalence guard fails when a pre-existing typed variable row diverges
    from its legacy source. test: `tests/storage/test_variable_copy_migration.py`.

    4.2.6: Rerunning the variable copy over already-migrated soft-deleted rows completes
    without a primary-key abort. test: `tests/storage/test_variable_copy_migration.py::test_rerun_over_soft_deleted_rows`.

    4.2.7: Rerunning the variable copy over already-migrated live rows is a clean
    no-op, and two soft-deleted variable rows sharing a natural key both migrate.
    test: `tests/storage/test_variable_copy_migration.py::test_rerun_over_live_rows`.

    4.2.8: A live typed variable row matching a legacy row on natural key and payload
    but carrying a different UUID fails the guard loudly. test: `tests/storage/test_variable_copy_migration.py::test_divergent_identity_fails`.

    4.2.9: No generic surface can create or mutate a legacy variable row post-cutover:
    the generic HTTP routes, generic MCP tools, and the import path each reject kind
    `variable` naming the surviving domain surface. test: `tests/servers/routes/test_workflows.py`.
    test: `tests/mcp_proxy/tools/test_workflow_crud.py`. test: `tests/workflows/test_imports.py`.

    4.2.10: Bundled variable sync reaches the typed table through update_from_sync:
    a changed template enabled default is adopted on an untouched row and preserved
    on a pinned row. test: `tests/workflows/test_sync.py`.

    4.2.11: The pinned schema root hashes and the release-pinned expected identity
    match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`.
    file: `src/gobby/storage/schema_expected_identity.json`.

    4.2.12: Alternating sessions across project A, project B, and no-project see exactly
    their own overrides plus globals on all four application paths, with no cross-project
    cache leakage. test: `tests/workflows/test_session_defaults.py::test_project_scoped_defaults_isolation`.

    4.2.13: The variable copy migration writes one legacy_copy_ledger row per copied
    source row and reruns keep the copy-time hash. test: `tests/storage/test_variable_copy_migration.py`.

    4.2.14: The variable copy migration holds ACCESS EXCLUSIVE on workflow_definitions:
    a concurrent second-connection variable-row write blocks until the migration commits,
    and its post-commit landing is the post-copy drift the 7.1 backstop refuses. test:
    `tests/storage/test_variable_copy_migration.py::test_copy_lock_fences_concurrent_writes`.'
  labels:
  - covers:split-workflow-definition-storage:4.2:4.2.1
  - covers:split-workflow-definition-storage:4.2:4.2.2
  - covers:split-workflow-definition-storage:4.2:4.2.3
  - covers:split-workflow-definition-storage:4.2:4.2.3a
  - covers:split-workflow-definition-storage:4.2:4.2.4
  - covers:split-workflow-definition-storage:4.2:4.2.5
  - covers:split-workflow-definition-storage:4.2:4.2.6
  - covers:split-workflow-definition-storage:4.2:4.2.7
  - covers:split-workflow-definition-storage:4.2:4.2.8
  - covers:split-workflow-definition-storage:4.2:4.2.9
  - covers:split-workflow-definition-storage:4.2:4.2.10
  - covers:split-workflow-definition-storage:4.2:4.2.11
  - covers:split-workflow-definition-storage:4.2:4.2.12
  - covers:split-workflow-definition-storage:4.2:4.2.13
  - covers:split-workflow-definition-storage:4.2:4.2.14
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Pipelines cutover and copy migration
  category: code
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: '4.3.1: PipelineLoader serves load/discover/validate with extends
    resolution and a revision-aware cache. symbol: `PipelineLoader`. file: `src/gobby/workflows/pipeline_loader.py`.

    4.3.2: loader.py and loader_discovery.py are deleted and no source or test module
    imports WorkflowLoader. file: `src/gobby/workflows/loader.py`. file: `src/gobby/workflows/loader_discovery.py`.

    4.3.3: Pipeline dry-run is pipeline-only and agent dry-run is unchanged. symbol:
    `evaluate_pipeline_definition`. file: `src/gobby/workflows/dry_run.py`.

    4.3.9: The construction and injection layer types the loader as PipelineLoader,
    so the tree imports at this commit. file: `src/gobby/hooks/factory.py`. file:
    `src/gobby/mcp_proxy/registries.py`. file: `src/gobby/runner_init/orchestration.py`.

    4.3.10: WorkflowEvaluation no longer carries workflow_type. symbol: `WorkflowEvaluation`.
    file: `src/gobby/workflows/dry_run.py`.

    4.3.4: Copy migration lands 11 pipelines with counts validated. test: `tests/storage/test_pipeline_copy_migration.py`.

    4.3.5: Dynamic pipeline MCP tool exposure and stage/scheduler execution load through
    the typed manager. test: `tests/workflows/test_pipeline_loader.py`.

    4.3.6: The equivalence guard fails when a pre-existing typed pipeline row diverges
    from its legacy source. test: `tests/storage/test_pipeline_copy_migration.py`.

    4.3.8: Rerunning the pipeline copy over already-migrated soft-deleted rows completes
    without a primary-key abort. test: `tests/storage/test_pipeline_copy_migration.py::test_rerun_over_soft_deleted_rows`.

    4.3.11: Rerunning the pipeline copy over already-migrated live rows is a clean
    no-op, and two soft-deleted pipeline rows sharing a natural key both migrate.
    test: `tests/storage/test_pipeline_copy_migration.py::test_rerun_over_live_rows`.

    4.3.12: A live typed pipeline row matching a legacy row on natural key and payload
    but carrying a different UUID fails the guard loudly. test: `tests/storage/test_pipeline_copy_migration.py::test_divergent_identity_fails`.

    4.3.7: Pipeline sync and per-kind import dispatch write the typed tables and refuse
    a kind change by target table; imports work for all four kinds through typed managers.
    file: `src/gobby/workflows/imports.py`. file: `src/gobby/workflows/sync_pipelines.py`.
    test: `tests/workflows/test_imports.py`.

    4.3.13: No generic surface can create or mutate a legacy pipeline row post-cutover:
    the generic HTTP routes and generic MCP tools reject kind `pipeline` naming the
    surviving domain surface. test: `tests/servers/routes/test_workflows.py`. test:
    `tests/mcp_proxy/tools/test_workflow_crud.py`.

    4.3.14: Bundled pipeline sync reaches the typed table through update_from_sync:
    a changed template enabled default is adopted on an untouched row and preserved
    on a pinned row. test: `tests/workflows/test_sync.py`.

    4.3.15: loader_validation.py survives with no WorkflowLoader reference, and every
    former loader test seam is absorbed, retargeted, or retyped per the closure inventory,
    including the hooks-factory patch sites. file: `src/gobby/workflows/loader_validation.py`.
    test: `tests/workflows/test_pipeline_loader.py`. test: `tests/workflows/test_workflow_hooks.py`.

    4.3.16: The pinned schema root hashes and the release-pinned expected identity
    match the rebuilt gdaemon after the migration entry lands. file: `crates/gcore/tests/schema_contract.rs`.
    file: `src/gobby/storage/schema_expected_identity.json`.

    4.3.17: Pipeline MCP create/update/delete/export operate on PipelineDefinitionManager
    with auto-export preserved, and _pipelines.py imports neither the generic definitions
    module nor the legacy manager. file: `src/gobby/mcp_proxy/tools/workflows/_pipelines.py`.
    test: `tests/mcp_proxy/tools/workflows/test_pipeline_crud.py`.

    4.3.18: The pipeline copy migration writes one legacy_copy_ledger row per copied
    source row and reruns keep the copy-time hash. test: `tests/storage/test_pipeline_copy_migration.py`.

    4.3.19: The pipeline copy migration holds ACCESS EXCLUSIVE on workflow_definitions:
    a concurrent second-connection pipeline-row write blocks until the migration commits,
    and its post-commit landing is the post-copy drift the 7.1 backstop refuses. test:
    `tests/storage/test_pipeline_copy_migration.py::test_copy_lock_fences_concurrent_writes`.'
  labels:
  - covers:split-workflow-definition-storage:4.3:4.3.1
  - covers:split-workflow-definition-storage:4.3:4.3.2
  - covers:split-workflow-definition-storage:4.3:4.3.3
  - covers:split-workflow-definition-storage:4.3:4.3.9
  - covers:split-workflow-definition-storage:4.3:4.3.10
  - covers:split-workflow-definition-storage:4.3:4.3.4
  - covers:split-workflow-definition-storage:4.3:4.3.5
  - covers:split-workflow-definition-storage:4.3:4.3.6
  - covers:split-workflow-definition-storage:4.3:4.3.8
  - covers:split-workflow-definition-storage:4.3:4.3.11
  - covers:split-workflow-definition-storage:4.3:4.3.12
  - covers:split-workflow-definition-storage:4.3:4.3.7
  - covers:split-workflow-definition-storage:4.3:4.3.13
  - covers:split-workflow-definition-storage:4.3:4.3.14
  - covers:split-workflow-definition-storage:4.3:4.3.15
  - covers:split-workflow-definition-storage:4.3:4.3.16
  - covers:split-workflow-definition-storage:4.3:4.3.17
  - covers:split-workflow-definition-storage:4.3:4.3.18
  - covers:split-workflow-definition-storage:4.3:4.3.19
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: HTTP surface rebuild
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.1'
  - '4.2'
  - '4.3'
  validation_criteria: '5.1.1: The generic workflows router no longer exists and nothing
    registers it. file: `src/gobby/servers/_app_routes.py`.

    5.1.2: Pipeline definition routes cover the full UI demand set and mount before
    the execution router. file: `src/gobby/servers/routes/pipeline_definitions.py`.

    5.1.3: Variable definition routes cover the settings-editor demand set. file:
    `src/gobby/servers/routes/variable_definitions.py`.

    5.1.4: Session variable get/set live under the sessions API with scope semantics.
    file: `src/gobby/servers/routes/sessions/variables.py`. test: `tests/servers/routes/test_session_variables.py`.

    5.1.5: Template drift annotation works per domain through the re-keyed cache.
    symbol: `TemplateHashCache`. file: `src/gobby/workflows/template_hashes.py`.

    5.1.6: The daemon-proxy client calls the relocated session-variable endpoints
    with scope, proven at the client seam, and no /api/workflows literal remains in
    it. file: `src/gobby/mcp_proxy/stdio_proxy.py`. test: `tests/mcp_proxy/test_stdio_proxy.py`.

    5.1.7: No /api/workflows reference remains in the project-context middleware.
    file: `src/gobby/servers/middleware/project_context.py`.

    5.1.8: Both generic workflows route suites are deleted in this commit and the
    variables get/set coverage survives under the sessions API. test: `tests/servers/routes/test_workflows.py`.
    test: `tests/servers/test_workflow_routes.py`. test: `tests/servers/routes/test_session_variables.py`.

    5.1.9: An agent token authorizes the relocated session-variable routes for its
    own session only, the old workflow-variable grants are gone, and the authenticated
    DaemonProxy round trip passes at the integration seam. file: `src/gobby/servers/auth_service.py`.
    test: `tests/servers/test_auth_service.py`. test: `tests/mcp_proxy/test_mcp_proxy_stdio.py`.

    5.1.10: workflow_templates.py and its suite are deleted with the generic router
    and nothing in the tree imports either. file: `src/gobby/workflows/workflow_templates.py`.
    test: `tests/workflows/test_workflow_templates.py`.'
  labels:
  - covers:split-workflow-definition-storage:5.1:5.1.1
  - covers:split-workflow-definition-storage:5.1:5.1.2
  - covers:split-workflow-definition-storage:5.1:5.1.3
  - covers:split-workflow-definition-storage:5.1:5.1.4
  - covers:split-workflow-definition-storage:5.1:5.1.5
  - covers:split-workflow-definition-storage:5.1:5.1.6
  - covers:split-workflow-definition-storage:5.1:5.1.7
  - covers:split-workflow-definition-storage:5.1:5.1.8
  - covers:split-workflow-definition-storage:5.1:5.1.9
  - covers:split-workflow-definition-storage:5.1:5.1.10
  tdd: true
  source_section: '5.1'
  implementation_domain: fullstack
- title: MCP surface prune and re-scope
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.1'
  - '4.2'
  - '4.3'
  validation_criteria: '5.2.1: Generic definition CRUD tools are gone from the registry;
    domain tools remain. file: `src/gobby/mcp_proxy/tools/workflows/__init__.py`.

    5.2.2: evaluate_pipeline and evaluate_agent expose the complete dry-run story.
    test: `tests/mcp_proxy/tools/workflows/test_registry_surface.py::test_evaluate_tools_cover_pipeline_and_agent`.

    5.2.3: get_step_status is registered under its new name and reports the snapshot
    step list for a session. symbol: `get_step_status`. file: `src/gobby/mcp_proxy/tools/workflows/_query.py`.

    5.2.4: One sync registry feeds install, reload_cache, and CLI sync. symbol: `sync_bundled_content_to_db`.
    file: `src/gobby/sync_registry.py`.

    5.2.5: Auto-export dispatches on explicit kind with per-domain collision checks.
    file: `src/gobby/mcp_proxy/tools/workflows/_auto_export.py`.

    5.2.5a: Every auto-export caller passes its kind explicitly. file: `src/gobby/mcp_proxy/tools/workflows/_agents.py`.
    file: `src/gobby/mcp_proxy/tools/workflows/_rules.py`. file: `src/gobby/mcp_proxy/tools/workflows/_variables.py`.
    file: `src/gobby/mcp_proxy/tools/workflows/_pipelines.py`.

    5.2.6: Registry tool inventory and schemas match the disposition table. test:
    `tests/mcp_proxy/tools/workflows/test_registry_surface.py`.

    5.2.7: The generic-CRUD and get_workflow not-found suites are deleted and the
    import, project-scope, and query suites are retargeted at surviving tools with
    their domain assertions intact. test: `tests/mcp_proxy/tools/test_workflow_crud.py`.
    test: `tests/mcp_proxy/tools/workflows/test_get_workflow_not_found.py`. test:
    `tests/mcp_proxy/tools/workflows/test_import.py`. test: `tests/mcp_proxy/tools/workflows/test_project_scope.py`.
    test: `tests/mcp_proxy/tools/workflows/test_query.py`.

    5.2.8: The dispatch prompt and bundled agent instructions name get_step_status,
    the regenerated bundled content manifest passes its freshness test, and the prompt
    and definition regressions pin the rename. file: `src/gobby/dispatch/prompts.py`.
    file: `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml`. test: `tests/dispatch/test_prompts.py`.
    test: `tests/agents/test_qa_reviewer_definition.py`.

    5.2.9: The registry-inventory and E2E suites assert the final tool set: no list_workflows,
    get_step_status present and callable. test: `tests/events/test_mcp_tool_changes.py`.
    test: `tests/e2e/test_parallel_clones.py`. test: `tests/e2e/test_sequential_review_loop.py`.'
  labels:
  - covers:split-workflow-definition-storage:5.2:5.2.1
  - covers:split-workflow-definition-storage:5.2:5.2.2
  - covers:split-workflow-definition-storage:5.2:5.2.3
  - covers:split-workflow-definition-storage:5.2:5.2.4
  - covers:split-workflow-definition-storage:5.2:5.2.5
  - covers:split-workflow-definition-storage:5.2:5.2.5a
  - covers:split-workflow-definition-storage:5.2:5.2.6
  - covers:split-workflow-definition-storage:5.2:5.2.7
  - covers:split-workflow-definition-storage:5.2:5.2.8
  - covers:split-workflow-definition-storage:5.2:5.2.9
  tdd: true
  source_section: '5.2'
  implementation_domain: backend
- title: CLI restructure
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  - '5.2'
  validation_criteria: '6.1.1: The gobby workflows group is gone and per-domain replacements
    exist. file: `src/gobby/cli/__init__.py`.

    6.1.2: Reinstall runs per-domain through the sync registry with no raw legacy
    SQL. file: `src/gobby/cli/sync.py`.

    6.1.4: Filesystem imports cover the per-kind directories. symbol: `sync_imported_workflows`.
    file: `src/gobby/workflows/imports.py`.

    6.1.5: New CLI subcommands are covered by focused tests. test: `tests/cli/test_agents_steps.py`.

    6.1.6: `gobby variables get|set --session` reads and writes both scopes and replaces
    the deleted set-var/get-var commands. file: `src/gobby/cli/variables.py`.

    6.1.7: The new agent subcommands live in the extracted module and both it and
    the registration surface stay below 1,000 lines. file: `src/gobby/cli/agents_steps.py`.
    file: `src/gobby/cli/agents.py`.

    6.1.8: `gobby pipelines` imports cleanly after the package deletion: no production
    module imports gobby.cli.workflows, and the re-homed helpers serve the pipelines
    CLI. file: `src/gobby/cli/pipelines.py`.

    6.1.9: The three workflows-group suites are deleted with the package and the pipelines-coverage
    patch paths point at the re-homed helper. test: `tests/cli/test_cli_workflows.py`.
    test: `tests/cli/test_workflows.py`. test: `tests/cli/test_workflows_coverage.py`.
    test: `tests/cli/test_pipelines_coverage.py`.'
  labels:
  - covers:split-workflow-definition-storage:6.1:6.1.1
  - covers:split-workflow-definition-storage:6.1:6.1.2
  - covers:split-workflow-definition-storage:6.1:6.1.4
  - covers:split-workflow-definition-storage:6.1:6.1.5
  - covers:split-workflow-definition-storage:6.1:6.1.6
  - covers:split-workflow-definition-storage:6.1:6.1.7
  - covers:split-workflow-definition-storage:6.1:6.1.8
  - covers:split-workflow-definition-storage:6.1:6.1.9
  tdd: true
  source_section: '6.1'
  implementation_domain: backend
- title: Web UI migration
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  - '5.2'
  validation_criteria: '6.2.1: No web code references /api/workflows or workflow_type.
    file: `web/src/hooks/usePipelineDefs.ts`.

    6.2.2: Pipeline definitions UI performs full CRUD against the domain routes. file:
    `web/src/components/activity/pipelines/PipelinesDefsActions.ts`.

    6.2.3: Variable defaults editor works against /api/variables under its new name.
    file: `web/src/components/settings/VariableDefaultsEditor.tsx`.

    6.2.4: The agent editor''s pipeline picker is populated (data.definitions bug
    fixed). file: `web/src/components/activity/agents/AgentsTabData.ts`.

    6.2.5: The retargeted pipeline-definition and editor suites pass. test: `web/src/components/activity/pipelines/__tests__/PipelinesDefs.test.tsx`.
    test: `web/src/components/activity/pipelines/__tests__/PipelineEditor.test.tsx`.

    6.2.6: The retargeted settings and agents-tab suites pass. test: `web/src/components/settings/__tests__/WorkflowVariablesEditor.test.tsx`.
    test: `web/src/components/settings/sections/__tests__/AutomationWorkflowsSection.test.tsx`.
    test: `web/src/components/activity/__tests__/AgentsTab.test.tsx`.

    6.2.7: The refetch and selection-race hook suites pass against the new hooks.
    test: `web/src/hooks/__tests__/useFilteredRefetches.test.ts`. test: `web/src/hooks/__tests__/useSelectionFetchRaces.test.ts`.

    6.2.8: The variable display helper reads default_value, and the style-surface
    capture fake serves the domain routes with no generic /api/workflows branch, so
    the migrated editors render populated in visual coverage. file: `web/src/components/settings/workflowVariables.ts`.
    test: `web/tests/style-surfaces.spec.ts`.

    6.2.9: The web definition types and adapters model step data only under the nested
    step_workflow object, and the read-only panel renders steps from it. file: `web/src/components/agents/AgentEditForm.types.ts`.
    file: `web/src/components/activity/agents/AgentsTabData.ts`. file: `web/src/components/agents/AgentReadOnlyDetails.tsx`.
    file: `web/src/components/activity/agents/AgentsTabActions.ts`.

    6.2.10: A hydrated agent definition round-trips to a draft and back to a save
    body with steps, step variables, and exit condition intact, asserted in the retargeted
    adapter and editor suites. test: `web/src/components/activity/__tests__/AgentsTabActions.test.ts`.
    test: `web/src/components/agents/__tests__/AgentEditors.test.tsx`.'
  labels:
  - covers:split-workflow-definition-storage:6.2:6.2.1
  - covers:split-workflow-definition-storage:6.2:6.2.2
  - covers:split-workflow-definition-storage:6.2:6.2.3
  - covers:split-workflow-definition-storage:6.2:6.2.4
  - covers:split-workflow-definition-storage:6.2:6.2.5
  - covers:split-workflow-definition-storage:6.2:6.2.6
  - covers:split-workflow-definition-storage:6.2:6.2.7
  - covers:split-workflow-definition-storage:6.2:6.2.8
  - covers:split-workflow-definition-storage:6.2:6.2.9
  - covers:split-workflow-definition-storage:6.2:6.2.10
  tdd: true
  source_section: '6.2'
  implementation_domain: fullstack
- title: Drop migration and legacy module deletion
  category: code
  task_type: feature
  depends_on:
  - '6.1'
  - '6.2'
  validation_criteria: "7.1.1: The drop is a destructive EmbeddedMigration whose backstop\
    \ verifies every non-generated legacy row, live and soft-deleted, against its\
    \ legacy_copy_ledger checkpoint hash and RAISEs with offending ids and names.\
    \ test: `tests/storage/test_drop_legacy_migration.py`.\n7.1.2: The backstop refuses\
    \ to drop when a legacy row was written after its copy (hash mismatch) and when\
    \ a legacy row has no ledger entry (post-copy insertion). test: `tests/storage/test_drop_legacy_migration.py`.\n\
    7.1.2a: The backstop also covers soft-deleted legacy rows by preserved id, refusing\
    \ to drop a definition created after its copy migration and soft-deleted before\
    \ P5. test: `tests/storage/test_drop_legacy_migration.py::test_backstop_covers_soft_deleted_rows`.\n\
    7.1.3: Both legacy tables and legacy_copy_ledger are gone from the baseline, the\
    \ catalog manifest, and the live schema after the destructive apply, and the refresh\
    \ contract enumerates exactly the removed statements. file: `crates/gcore/assets/schema/baseline.sql`.\
    \ file: `crates/gcore/src/schema/runner_tests.rs`.\n7.1.4: storage/workflow_definitions.py\
    \ is deleted and no source or test imports it. file: `src/gobby/storage/definitions/_shared.py`.\n\
    7.1.5: The scheduled soft-deleted-definition purge drops the legacy manager import\
    \ and fans out over the four typed parent managers, with agent step-workflow children\
    \ removed by cascade and no step-instance branch. symbol: `_purge_soft_deleted_definitions`.\
    \ file: `src/gobby/sessions/lifecycle.py`.\n7.1.6: A fresh lineage receipt-stamps\
    \ the drop without executing it; an existing hub refuses it on plain restart and\
    \ applies it under --destructive with a verified backup manifest inside an open\
    \ maintenance epoch. file: `crates/gcore/src/schema/runner_tests.rs`. test: `tests/storage/test_drop_legacy_migration.py`.\n\
    7.1.7: Template hashing and the skills metadata docstring carry no legacy storage\
    \ reference. file: `src/gobby/workflows/template_hashes.py`. file: `src/gobby/storage/skills/_metadata.py`.\n\
    7.1.8: A signature-matching generated row is excluded from the backstop, while\
    \ a workflow_type='workflow' row failing the generated signature and a row with\
    \ an unknown workflow_type each fail the preflight loudly with ids and names.\
    \ test: `tests/storage/test_drop_legacy_migration.py::test_unsupported_row_classification`.\n\
    7.1.9: Typed-side evolution after copy \u2014 an edit, a sync refresh, a restore,\
    \ and a hard-delete of typed rows \u2014 does not block the drop, while a legacy-only\
    \ write and a post-copy legacy insertion each block it loudly. test: `tests/storage/test_drop_legacy_migration.py::test_directional_backstop`.\n\
    7.1.10: Replaying every embedded copy migration against a fresh final-baseline\
    \ lineage, where neither legacy table exists, records receipted no-ops with no\
    \ error and no typed-row writes. file: `crates/gcore/src/schema/runner_tests.rs`.\n\
    7.1.11: gdaemon schema apply --destructive refuses when its connection lacks the\
    \ open maintenance-epoch GUC, naming gobby hub-maintenance run schema-apply in\
    \ the refusal, and succeeds over an epoch-bound DSN with a verified backup manifest.\
    \ symbol: `apply_schema`. file: `crates/gdaemon/src/main.rs`. test: `crates/gdaemon/tests/schema_cli.rs`."
  labels:
  - covers:split-workflow-definition-storage:7.1:7.1.1
  - covers:split-workflow-definition-storage:7.1:7.1.2
  - covers:split-workflow-definition-storage:7.1:7.1.2a
  - covers:split-workflow-definition-storage:7.1:7.1.3
  - covers:split-workflow-definition-storage:7.1:7.1.4
  - covers:split-workflow-definition-storage:7.1:7.1.5
  - covers:split-workflow-definition-storage:7.1:7.1.6
  - covers:split-workflow-definition-storage:7.1:7.1.7
  - covers:split-workflow-definition-storage:7.1:7.1.8
  - covers:split-workflow-definition-storage:7.1:7.1.9
  - covers:split-workflow-definition-storage:7.1:7.1.10
  - covers:split-workflow-definition-storage:7.1:7.1.11
  tdd: true
  source_section: '7.1'
  implementation_domain: backend
- title: Legacy-reference audit test
  category: test
  task_type: feature
  depends_on:
  - '7.1'
  - '7.3'
  validation_criteria: '7.2.1: The audit fails on any production reference to removed
    storage and passes on the final tree. test: `tests/audit/test_legacy_workflow_storage_removed.py`.

    7.2.2: The audit covers the baseline SQL and bundled YAML/skill/prompt sources,
    and fails when an allowlist entry no longer matches anything. test: `tests/audit/test_legacy_workflow_storage_removed.py::test_allowlist_is_self_pruning`.

    7.2.3: Every occurrence in the owner inventory is either absent from the final
    tree or covered by an exact allowlist triple, asserted rather than assumed. test:
    `tests/audit/test_legacy_workflow_storage_removed.py::test_every_preexisting_occurrence_has_an_owner`.'
  labels:
  - covers:split-workflow-definition-storage:7.2:7.2.1
  - covers:split-workflow-definition-storage:7.2:7.2.2
  - covers:split-workflow-definition-storage:7.2:7.2.3
  tdd: false
  source_section: '7.2'
  assigned_agent: backend-developer
- title: Documentation sweep
  category: docs
  task_type: feature
  depends_on:
  - '7.1'
  validation_criteria: '7.3.1: Guides and architecture docs describe the domain-table
    model and snapshot runtime. file: `docs/guides/workflows-overview.md`. file: `docs/architecture/architecture.md`.

    7.3.2: The conflicting prior design doc is deleted and the false review claim
    corrected. file: `docs/reviews/cli-build-ops.md`.

    7.3.3: Bundled skills and prompts reference only surviving MCP tools. file: `src/gobby/install/shared/skills/persona/SKILL.md`.

    7.3.4: No audited legacy token remains in config/tasks.py. file: `src/gobby/config/tasks.py`.

    7.3.5: The HTTP endpoint and pipelines guides describe the domain routes with
    no /api/workflows or workflow_type reference. file: `docs/guides/http-endpoints.md`.
    file: `docs/guides/pipelines.md`.

    7.3.9: The CLI guide documents the replacement command surface with no gobby workflows
    group, and the MCP tools and variables guides name only surviving tools. file:
    `docs/guides/cli-commands.md`. file: `docs/guides/mcp-tools.md`. file: `docs/guides/variables.md`.

    7.3.6: The configuration audit carries an explicit active-or-historical disposition.
    file: `docs/audits/configuration-audit.md`.

    7.3.7: Module guidance describes the domain tables. file: `src/gobby/dispatch/CLAUDE.md`.
    file: `src/gobby/install/shared/workflows/rules/CLAUDE.md`.

    7.3.8: The bundled content manifest is regenerated for every rewritten bundled
    skill and prompt and its freshness test passes without an update flag. file: `src/gobby/install/bundled_content_manifest.json`.
    test: `tests/install/test_bundled_content_manifest.py`.'
  labels:
  - covers:split-workflow-definition-storage:7.3:7.3.1
  - covers:split-workflow-definition-storage:7.3:7.3.2
  - covers:split-workflow-definition-storage:7.3:7.3.3
  - covers:split-workflow-definition-storage:7.3:7.3.4
  - covers:split-workflow-definition-storage:7.3:7.3.5
  - covers:split-workflow-definition-storage:7.3:7.3.9
  - covers:split-workflow-definition-storage:7.3:7.3.6
  - covers:split-workflow-definition-storage:7.3:7.3.7
  - covers:split-workflow-definition-storage:7.3:7.3.8
  tdd: false
  source_section: '7.3'
  assigned_agent: tech-writer
```
