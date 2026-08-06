Plan artifact: `.gobby/plans/machine-scoped-worktrees-clones.md`

> **Plan ID:** machine-scoped-worktrees-clones
> **Execution task:** #19649
> **Completion epic:** #17437

# Machine-Scoped Worktrees and Clones Completion

## Overview
`kind: framing`

Complete the durable machine-ownership contract for worktrees and clones under #19649,
then close #17437. M0 tasks #19594 (`ba8383154`) and #19595 (`8896d4fbe`) already
landed most foundations; this plan implements only the audited gaps.

| Contract area | Current evidence | Required delta |
| --- | --- | --- |
| Durable ownership | Worktrees/clones carried non-null machine FKs while sessions remained nullable | Sessions are now non-null-owned; exact composite workspace/session bindings are enforced |
| Machine-scoped identity | Worktree path/branch and clone path indexes include `machine_id`; two machines may store the same local path or branch | Preserve and verify |
| Create and keyed reuse | Writers stamp local identity; path, branch, task, claim, stale, expired, cleanup, and deletion selectors have partial machine guards | Add workspace/session co-location checks and close remaining direct-ID gaps |
| Direct-ID lifecycle | `get`, generic update/touch, release/status transitions, live-claim checks, counts, and merge-state reads still admit global rows | Make ordinary APIs local and raise typed mismatch for foreign UUIDs |
| Service safety | Path-based worktree removal has a foreign-owner guard; direct-ID MCP and REST paths can load a foreign row before Git/filesystem work | Translate mismatch consistently before every side effect |
| Session bootstrap | Baseline 375 seeded a machine-less system session | The seed is removed and first-lease startup creates it after machine registration without later owner transfer |
| Cooperative handoff | Active local agents blocked handoff | Pending/running local cron rows now block too; pipelines remain advisory |
| Runtime stability | Focused tests did not prove post-cutover stability | A warning/error-free five-minute same-PID soak is required after the final restart |
| Fleet visibility and execution | #17438 owns fleet inventory; #17436 owns worker routing and remote execution | Keep outside this plan |

## Constraints
`kind: framing`

- #19649 remains the sole implementation child. Its accepted completion closes #17437.
- Before 0.5.0 ships, schema changes go directly into the gcore baseline and regenerated
  catalog/checksum/identity artifacts. This plan creates no numbered migration.
- Ordinary worktree and clone APIs operate on the current machine. Unscoped lookup is a
  private diagnostic primitive used only to distinguish a missing UUID from a foreign UUID.
- Path, branch, and task lookup remain machine-relative. A matching row on another machine
  is locally absent, allowing identical local paths and branches across machines.
- Direct foreign UUIDs produce `machine_ownership_mismatch` with resource kind/id, owner
  machine ID, current machine ID, and a readable message. MCP returns the structured failure;
  REST returns HTTP 409 with the same payload; CLI exits non-zero and prints the code and IDs.
- Workspace create and claim require the referenced session to have the same `machine_id` as
  the workspace/current machine. Service validation supplies the typed error; baseline
  constraints provide the durable final guard.
- Ownership validation precedes path probes, Git/process calls, filesystem mutation, database
  mutation, event emission, and task-artifact cleanup.
- Worker routing, remote path interpretation, offline-worker behavior, account/auth policy,
  and fleet-wide workspace browsing remain in #17436, #17769, and #17438 respectively.
- Validation uses `GOBBY_TEST_PROTECT=1` and an isolated PostgreSQL `DATABASE_URL`. The full
  pytest suite is excluded. The current discovery run passed 88 unit cases; 16 PostgreSQL
  cases correctly refused to run without an isolated DSN.
- Preserve unrelated dirty files under `src/gobby/install/shared/skills/impeccable/references/`.
- Every touched hand-maintained production source finishes below 1,000 lines. In particular,
  keep ownership translation centralized so `worktrees/_sync.py` (894 lines) needs no
  repetitive handlers and `source_control.py` (921 lines) remains below the ceiling.
- The fixed system session keeps its original owner across ordinary lease changes.
- Any restart or warning/error resets the required five-minute soak clock.

## P1: Complete the Machine-Owned Workspace Contract
`kind: framing`

**Goal**: Close every audited storage and service gap in one TDD implementation task, then
record bounded evidence sufficient to close #19649 and #17437.

### 1.1 Enforce machine ownership across schema, storage, and service boundaries [category: code]
`kind: deliverable`

Targets:
- `.gobby/plans/machine-scoped-worktrees-clones.md`
- `crates/gcore/assets/schema/baseline.sql::*` — scope-reason: add and verify the complete workspace/session ownership constraint set in the canonical pre-0.5.0 schema
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the canonical schema catalog after baseline constraint changes
- `crates/gcore/assets/schema/seed.manifest.json::*` — scope-reason: remove the machine-less system-session seed while preserving project seeds
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: regenerate baseline and root identity constants after the canonical schema changes
- `crates/gcore/tests/catalog_manifest_freshness.rs::*` — scope-reason: extend fresh-schema coverage for workspace/session machine ownership
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pin the regenerated baseline identity and semantic schema contract
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: synchronize Python's expected gcore identity with regenerated canonical assets
- `src/gobby/storage/workspace_machine_scope.py::*` — scope-reason: define the shared typed ownership invariant and diagnostic lookup helpers
- `src/gobby/storage/worktrees.py::LocalWorktreeManager`
- `src/gobby/storage/clones.py::LocalCloneManager`
- `src/gobby/storage/session_models.py::*` — scope-reason: make the persisted session machine identity non-optional
- `src/gobby/storage/sessions/_constants.py::*` — scope-reason: bootstrap the fixed system session with the required local machine identity
- `src/gobby/storage/sessions/_crud.py::*` — scope-reason: resolve absent ingress identity and reject explicit or existing foreign ownership
- `src/gobby/storage/sessions/_manager.py::*` — scope-reason: validate ownership before registration side effects
- `src/gobby/storage/sessions/_upsert.py::*` — scope-reason: refuse ownership rewrites during session reuse
- `src/gobby/runner_init/storage.py::*` — scope-reason: register the machine before system-session bootstrap and preserve startup failure ordering
- `src/gobby/agents/launcher_session.py::*` — scope-reason: require a daemon UUID for launcher-created sessions
- `src/gobby/servers/routes/agent_spawn.py::*` — scope-reason: require a daemon UUID for web-chat session creation
- `src/gobby/servers/routes/admin/_lease.py::*` — scope-reason: include local pending and running cron executions in cooperative handoff blockers
- `src/gobby/runner_maintenance/isolation.py::*` — scope-reason: inherit storage-owned machine scoping in missing-path sweeps
- `src/gobby/mcp_proxy/tools/internal.py::InternalToolRegistry.call`
- `src/gobby/mcp_proxy/tools/internal.py::InternalToolRegistry.call_sync`
- `src/gobby/servers/routes/source_control.py::delete_worktree`
- `src/gobby/servers/routes/source_control.py::delete_clone`
- `src/gobby/cli/worktrees.py::resolve_worktree_id`
- `src/gobby/cli/clones.py::resolve_clone_id`
- `tests/storage/test_workspace_machine_scope.py::*` — scope-reason: prove every workspace lifecycle surface and session binding under two machines
- `tests/storage/test_worktrees.py::*` — scope-reason: pin every remaining worktree reader, mutation, uniqueness, and session-binding branch to machine ownership
- `tests/storage/test_clones.py::*` — scope-reason: pin every remaining clone reader, mutation, uniqueness, and session-binding branch to machine ownership
- `tests/mcp_proxy/tools/test_internal_workspace_scope.py::*` — scope-reason: pin the canonical async and sync ownership envelope
- `tests/storage/sessions/test_registration.py::*` — scope-reason: cover required local identity and foreign reuse rejection
- `tests/storage/sessions/test_usage_and_bootstrap.py::*` — scope-reason: prove first-lease bootstrap and stable later ownership
- `tests/storage/test_machine_scope_writers.py::*` — scope-reason: prove local writers persist canonical machine UUIDs
- `tests/test_runner_init.py::*` — scope-reason: prove machine registration precedes system-session bootstrap
- `tests/scheduler/test_cron_machine_scope.py::*` — scope-reason: pin pending/running cron counts to the local machine
- `tests/servers/routes/test_daemon_lease_routes.py::*` — scope-reason: prove combined agent and cron handoff blockers
- `tests/servers/routes/test_agent_spawn_routes.py::*` — scope-reason: prove web-chat and launcher writers use required daemon identity
- `tests/mcp_proxy/tools/test_worktrees_lifecycle.py::*` — scope-reason: prove foreign worktree IDs fail before all Git, filesystem, event, and artifact-cleanup effects
- `tests/mcp_proxy/tools/test_clones.py::*` — scope-reason: prove foreign clone IDs fail before all Git, filesystem, claim, sync, merge, and delete effects
- `tests/servers/routes/test_source_control_routes.py::*` — scope-reason: cover REST 409 ownership translation and zero-effect behavior for both workspace types
- `tests/cli/test_worktrees_cli.py::*` — scope-reason: cover stable cross-machine diagnostics through worktree CLI resolution and daemon results
- `tests/cli/test_clones_cli.py::*` — scope-reason: cover stable cross-machine diagnostics through clone CLI resolution and direct storage paths

Implement one shared `MachineOwnershipMismatchError` carrying `resource_kind`,
`resource_id`, `owner_machine_id`, and `current_machine_id`, with a canonical serializer for
the selected error envelope. Keep foreign-row lookup private to the storage layer.

For both storage managers:

- Make `get` local-machine aware. Return `None` for a missing local/global UUID; raise the
  typed mismatch when the UUID exists with another `machine_id`. Clone terminal-cleanup hiding
  occurs after ownership validation.
- Add `machine_id = require_machine_id()` to every direct-ID read and mutation, including
  generic update, touch, live-session claim checks, release, status/merge transitions, and
  deletion. When a guarded write affects no row, use the private lookup to distinguish missing,
  session-claim contention, terminal clone state, and foreign ownership without mutating.
- Make list, count, merge-state, stale, expired, and cleanup queries current-machine scoped by
  construction. Remove optional unscoped defaults and update callers to the local contract.
- Validate `agent_session_id` on create and claim against `sessions.machine_id` before INSERT or
  UPDATE. A missing session keeps existing not-found behavior; a foreign session raises the
  typed mismatch. Conditional claim atomicity and same-session idempotency remain intact.

In the gcore baseline, make `sessions.machine_id` non-null, add the exact
`sessions_id_machine_id_key`, and add the minimum composite foreign keys needed
to enforce `(agent_session_id, machine_id)` against `(sessions.id, sessions.machine_id)` for
both workspace tables. Preserve `ON DELETE SET NULL (agent_session_id)` behavior while retaining
the workspace's immutable machine owner. Remove only the system-session baseline seed. Regenerate the
catalog and identity artifacts through the repository's schema tooling; keep baseline version
375 and leave the numbered migration set empty.

Translate the typed exception once at `InternalToolRegistry` call boundaries so every worktree
and clone MCP tool returns the same structured envelope without inflating individual handlers.
Map direct REST storage access to HTTP 409. Convert CLI resolution failures to normal Click
errors that include the stable code and both machine IDs. Direct service callers may propagate
the typed exception; they must perform no local-resource effect after it is raised.

Use TDD evidence for the new behavior. Storage and fresh-schema tests use two machine rows and
two sessions to prove local success, foreign failure, same-path/same-branch coexistence, and
database rejection of cross-machine session attachment. Service tests inject a foreign record
and assert the exact error payload plus zero calls to `Path` probes, Git managers, storage
mutations, event emitters, and artifact cleanup.

Finish by updating the audit matrix in this artifact with final symbol/test/commit evidence,
running the focused validation below, reviewing every #19649 criterion against that evidence,
closing #19649 with its linked commit, and closing #17437 only after the child is valid and no
contract delta remains.

**Acceptance:**

- 1.1.1 - The audit maps every #17437/#19649 requirement to completed M0 evidence or a delivered delta. behavior: "Contract area" in `.gobby/plans/machine-scoped-worktrees-clones.md`.
- 1.1.2 - Fresh baseline schema rejects workspace/session machine mismatches while preserving nullable ownership and machine-scoped duplicate path/branch behavior. test: `crates/gcore/tests/catalog_manifest_freshness.rs::baseline_enforces_workspace_session_machine_ownership`.
- 1.1.3 - Worktree and clone create, direct-ID read/mutation, list, count, claim, reuse, stale/expired selection, cleanup, merge state, and deletion operate only on the current machine. test: `tests/storage/test_workspace_machine_scope.py::test_every_workspace_lifecycle_surface_is_machine_scoped`.
- 1.1.4 - Foreign workspace UUIDs yield `machine_ownership_mismatch` with both machine IDs across MCP, HTTP 409 REST, and CLI diagnostics. test: `tests/mcp_proxy/tools/test_internal_workspace_scope.py::test_workspace_mismatch_serializes_consistently`.
- 1.1.5 - Foreign worktree operations trigger no path, Git, filesystem, database, event, or artifact-cleanup side effect. test: `tests/mcp_proxy/tools/test_worktrees_lifecycle.py::test_foreign_worktree_id_fails_before_side_effects`.
- 1.1.6 - Foreign clone operations trigger no path, Git, filesystem, database, claim, sync, merge, or delete side effect. test: `tests/mcp_proxy/tools/test_clones.py::test_foreign_clone_id_fails_before_side_effects`.
- 1.1.7 - Worktree/clone create and claim reject a session owned by another machine at the service boundary and at the database constraint. test: `tests/storage/test_workspace_machine_scope.py::test_workspace_session_binding_requires_same_machine`.
- 1.1.8 - Every touched hand-maintained production source remains below 1,000 lines. behavior: "workspace ownership implementation respects the production line ceiling" in `.gobby/plans/machine-scoped-worktrees-clones.md`.
- 1.1.9 - The isolated focused validation is clean: `GOBBY_TEST_PROTECT=1 DATABASE_URL=<isolated-test-dsn> uv run pytest tests/storage/test_workspace_machine_scope.py tests/storage/test_worktrees.py tests/storage/test_clones.py tests/runner_maintenance/test_isolation_machine_scope.py tests/integration/test_cleanup_sweep_selection.py tests/mcp_proxy/tools/test_internal_workspace_scope.py tests/mcp_proxy/tools/test_worktrees_lifecycle.py tests/mcp_proxy/tools/test_clones.py tests/servers/routes/test_source_control_routes.py tests/cli/test_worktrees_cli.py tests/cli/test_clones_cli.py -v`; `cargo test -p gcore --test catalog_manifest_freshness --test schema_contract`; `uv run ruff check <touched-python-paths>`; `uv run mypy <touched-python-paths>`. test: `tests/storage/test_workspace_machine_scope.py::test_every_workspace_lifecycle_surface_is_machine_scoped`.
- 1.1.10 - First-lease startup creates the system session after machine registration and later leases preserve its owner. test: `tests/storage/sessions/test_usage_and_bootstrap.py::test_system_session_bootstrap_preserves_existing_owner`.
- 1.1.11 - Session registration persists required local ownership and rejects explicit or existing foreign identity. test: `tests/storage/sessions/test_registration.py::test_registration_requires_local_machine_ownership`.
- 1.1.12 - Launcher and web-spawn writers never use source-name fallback identities. test: `tests/servers/routes/test_agent_spawn_routes.py::TestSpawnAgent.test_spawn_session_writers_use_required_machine_identity`.
- 1.1.13 - Cooperative handoff reports combined local agent and cron blockers. test: `tests/servers/routes/test_daemon_lease_routes.py::test_handoff_rejects_agent_and_local_cron_blockers`.
- 1.1.14 - Renamed, missing, or altered composite constraints fail verification with the correct receipt. test: `crates/gcore/tests/catalog_manifest_freshness.rs::verify_contract_detects_workspace_constraint_drift`.
- 1.1.15 - The local database passes backup, preflight, exact DDL, receipt, and rebuilt-binary verification. behavior: "Local Docker Cutover Evidence" in `.gobby/plans/machine-scoped-worktrees-clones.md`.
- 1.1.16 - The final daemon PID remains healthy for five minutes with zero new warning/error diagnostics. behavior: "Five-Minute Daemon Soak Evidence" in `.gobby/plans/machine-scoped-worktrees-clones.md`.

## Validation Evidence
`kind: framing`

- Focused Python validation: the final consolidated run passed all 500 selected tests in 28.14 seconds with no pytest warnings.
- Schema: `cargo test -p gobby-core --features postgres --test catalog_manifest_freshness --test schema_contract` passed 18 tests (14 catalog, 4 contract).
- Ruff: all 35 touched Python paths pass formatting and lint checks.
- Mypy: 43 scoped production files pass with no issues.
- Test quality: 477 tests scanned, zero issues and zero new high-severity findings.
- Test types: 17 files scanned, zero new errors against the baseline.

### Local Docker Cutover Evidence
`kind: framing`

- Rebuilt/installed identity: baseline v375 checksum `0086a193a7ed83efce3933fe56d0f0ea64e49f6b47ce5cd000c289eca6a40b56`; root `460067e3303a8a2c8ab80bdc07bce8dd516a8196ec7ea34d074c2d39c8893ab3`.
- Verified backup: `/Users/josh/.gobby/backups/gobby-v375-pre-machine-session-cutover-20260806T181228Z.dump`; 305,785,787 bytes; SHA-256 `3c7e82527c6b9916bb557cbf227397e12c19ce4e84703d778e4e82d06b88c392`; `pg_restore -l` succeeded.
- Preflight: current machine `f7787eaf-968b-4ce8-a896-c1bbf306153a`; old v375 checksum `ee9c523b2f495e3403707f081e93a3c157b543c843951de14e331af7224b7886`; 9,335 null session owners; zero worktree/clone session mismatches or missing references.
- Transaction: locked sessions/worktrees/clones/receipt; backfilled 9,335 sessions; installed non-null column plus all three exact named composite constraints; updated exactly one v375 checksum; committed.
- Postflight: 9,488 sessions owned by the current machine; zero null owners and zero workspace/session mismatches. Rebuilt `gdaemon schema verify` reported `receipts=1, seed_rows=52, catalog_objects=2122`.

### Production Line Ceiling Evidence
`kind: framing`

Every touched hand-maintained production source is below 1,000 lines. The largest is `source_control.py` at 928 lines; worktrees and clones are 725 and 695 lines respectively.

### Five-Minute Daemon Soak Evidence
`kind: framing`

- Final restart boundary: `2026-08-06T18:14:28Z`; daemon log byte offset `4,905,984`; PID `22401`.
- Startup health completed in 4.1 seconds with PostgreSQL, Qdrant, FalkorDB, embeddings, MCP, cron, pipelines, and automation ready.
- Same-PID observations were recorded at 27s, 1m16s, 2m26s, 4m08s, 4m53s, and 5m43s; every observation reported healthy runtime and PostgreSQL services.
- Final observation: `2026-08-06T18:20:20Z`; log end byte `4,910,346`; uptime 5m43s; lease `held=true`, owner application `gobby-lease-v1:f7787eaf-968b-4ce8-a896-c1bbf306153a:1fa35fb5`, heartbeat age 0.953s.
- The exact restart window contained zero `WARNING`, `ERROR`, `CRITICAL`, traceback, unhandled-exception, lease-loss, or database-disconnect entries. No soak remediation task was required.

## V1 Plan Changelog
`kind: verification`

- Expanded the approved artifact to record the delivered session/bootstrap, cron-handoff, exact catalog, cutover, line-ceiling, and runtime-soak contracts.
