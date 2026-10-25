Plan artifact: `.gobby/plans/gdaemon-cron-family.md`

# Gobby 1.0 Stage 2: Cron and scheduler family

**Plan ID:** gdaemon-cron-family

## Overview
`kind: framing`

Implementation root: #21582. Planning assignment: #24952. Rust owns cron job and
run services, scheduled and manual admission, execution, recovery, restart
protection and the ten ledger HTTP routes below, plus a bounded internal
active-run count query for cooperative handoff. A statically linked family crate,
`crates/gcron` (package `gobby-cron`), uses the runtime family's RouteFamily,
configuration snapshot, PostgreSQL and compare seams. The hub owns the scheduler;
standalone runs the same owner, and nodes neither start it nor mutate its rows.

This work needs to exist: the ledger still names Python for all 28 rows. Existing
front-door routing and native code-index handlers solve part of the problem;
reuse them. There is no second scheduler, plugin registry, queue broker or new
database schema. Each deliverable below is one independently verifiable outcome.

## Decision Record
`kind: framing`

The Orchestrator confirmed these decisions on 2026-10-24 through Triage's live
coordination route, including the Stage 2/Stage 3 ordering clarification:

- Port all job/run services and the hub-only scheduler, including all four action
  kinds. Bind native business services from their owning families; do not port
  dream, agent, pipeline, feedback or project-purge business logic a second time.
- Invoke #24706's `gobby_codeindex::jobs::nightly_repair::run` directly. Depend on
  that leaf, never on the #21586 epic: its #24713 cleanup already waits on #21582.
- Delete Cron-owned Python scheduler, HTTP and nightly repair code after their
  own parity gates. #24713 owns the subsequent shared code-index helper and
  configuration-key deletion. This plan deletes neither of its two keys.
- Account for CLI rendering/dispatch in #21571 and MCP front-door registration in
  #21570. Supply their service contracts here. Cron never waits on #21571:
  Stage 3 already waits on Stage 2. S3.2's naming switch removes the Python CLI
  and remaining transitional shims in the same change; no supervised backend
  mode survives that switch.
- Use `proxy`/`compare` only during the parity transition. After native takeover,
  Cron has no Python fallback. Rollback before deletion uses the same binaries
  and routing edit; rollback after deletion requires the prior coherent release.
- Native construction through P3 is passive in production. Only 4.1 activates
  native scheduled/manual admission, reconciliation and system registration,
  after the restart boundary proves Python admission has stopped. Proxy/compare
  retain Python ownership. Isolated tests explicitly exercise native ownership.
- **Mailbox scope ruling (2026-10-24, Orchestrator via Triage).** Port today's
  delivered-mailbox retention against inter_session_messages, preserving its
  cutoff, bounded batch and cadence. Remove only the Python mailbox branch.
  #22340 remains parked by Josh and supplies no dependency or schema here;
  its later approved channel-schema work owns replacement of this function.
  Its unapproved D9 is reference context, not an approved service prerequisite.
- Use the locked chrono, Tokio, tokio-postgres, serde and axum dependencies.
  Schedule parsing additionally uses `croner` 4.0.1 with its chrono backend and
  `chrono-tz` 0.10, confined to gcron. No locked native cron parser or named-zone
  database exists. Its [documented fields and timezone support](https://docs.rs/crate/croner/4.0.1)
  justify reuse over a new calendar engine. A small compatibility layer preserves
  croniter's field order, aliases, validation and DST results against 1.1's
  vectors; library defaults are never the parity oracle.

## Constraints
`kind: framing`

- Every production file stays below 1,000 lines. Split before 850 where growth
  would risk the ceiling. Tests stay in external files. Existing large Python
  initialization files are decomposed inside the retirement leaf, not deferred.
- Each production Rust leaf carries its release: bump every affected shipped
  binary by one patch from its executor's starting tip, update Cargo.lock and
  MANAGED_BIN_VERSION_PINS, and validate the pins. Root workspace changes affect
  gcode, gdaemon and ghook; changes confined to gcron/gdaemon affect gdaemon.
  Unpublished gclient, gmcp and gterm do not receive release bumps.
- The release owner rebuilds every affected dependent and promotes through
  `promote_workspace_binary_set`. Only the Orchestrator delegates live cutover;
  announce globally, wait for no live worker/close validator and no protected
  run, then install/promote. This plan author runs no live cutover.
- All Python commands use uv. Pytest uses the isolated hub:
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest <focused-path>`.
  Never run the full suite. Native tests use isolated temporary state and ports.
  Cargo runs require the heavy-run grant, set CARGO_BUILD_JOBS=3 and name the
  package. Until #24949 lands, use cargo test instead of nextest (store denial).
- New APIs retain existing authentication, project authorization, system-row
  protection and shell restrictions. Cron action dispatch never bypasses the
  native spawn SRT chokepoint. Authenticated node requests route to the hub using
  the existing hub/node transport; they do not start a local scheduler.
- No invented workflow event names: any rule integration uses semantic events
  such as turn_start/turn_end, never raw provider callbacks as its authoring API.
- Scoped literal sweeps observed on the main 0.5.0 checkout:
  `gcode grep -F '#21582' docs/contracts/parity-ledger.md docs/contracts/parity-ledger-status.d/ -m 80`;
  `gcode grep -F 'cron_scheduler' src/gobby/ -m 100`;
  `gcode grep -w 'code_index_nightly_repairer' src/ tests/ -m 40`;
  `gcode grep -F 'register_handler(' src/gobby/ -m 80`.
  Also swept `gcode grep -F '#21582' .gobby/plans/ -m 100`: code-index D18 is
  covered by 2.6 and Python retirement; the parked messaging draft's proposed
  D9 received the explicit current-schema scope ruling above.
  The affected call paths are named in the owning sections. Exact targets are
  resolved from gcode outlines; full-file targets have bounded scope reasons.
- Observed source evidence: gcode evidence for
  `src/gobby/scheduler/scheduler.py::CronScheduler._create_scheduled_run`,
  excerpt_hash `ac19e22213f887d8637e68176f711b6aa0e8af8deaa9957c3da696c0dd75d788`,
  quotes `205| with self.storage.db.transaction_immediate(lock=CronRunAdmission()):`
  and `221| if run is None:` / `222| raise _ScheduledRunNotAdmitted`.
  The schedule CAS and insert therefore stay in one rollback boundary.

## Parity ownership
`kind: framing`

The 2026-10-24 ledger has exactly these 28 rows citing #21582. No row is dropped.
HTTP ownership is implemented here; CLI/MCP names are explicitly delegated to
their existing front-door owners. The service contract in 3.4 carries their
behavior, serialization, errors and restrictions. Delegated rows remain proxied
until their named owner actually ports them; this plan never marks them native.

| Ledger row | Outcome owner |
| --- | --- |
| CLI gobby cron | #21571; group/help dispatch, 3.4 service handoff |
| CLI gobby cron add | #21571; 1.3 and 3.4 create contract |
| CLI gobby cron edit | #21571; 1.3 and 3.4 update contract |
| CLI gobby cron list | #21571; 3.1 and 3.4 list contract |
| CLI gobby cron park | #21571; 1.3 and 3.4 park contract |
| CLI gobby cron remove | #21571; 1.3 and 3.4 delete contract |
| CLI gobby cron run | #21571; 2.1 and 3.4 admission contract |
| CLI gobby cron runs | #21571; 3.1 and 3.4 history contract |
| CLI gobby cron toggle | #21571; 1.3 and 3.4 toggle contract |
| CLI gobby cron wake | #21571; 1.3 and 3.4 wake contract |
| HTTP DELETE /api/cron/jobs/{job_id} | 3.2.3 |
| HTTP GET /api/cron/jobs | 3.1.1 |
| HTTP GET /api/cron/jobs/{job_id} | 3.1.2 |
| HTTP GET /api/cron/jobs/{job_id}/runs | 3.1.3 |
| HTTP GET /api/cron/runs/{run_id} | 3.1.4 |
| HTTP PATCH /api/cron/jobs/{job_id} | 3.2.2 |
| HTTP POST /api/cron/jobs | 3.2.1 |
| HTTP POST /api/cron/jobs/{job_id}/run | 3.2.5 |
| HTTP POST /api/cron/jobs/{job_id}/toggle | 3.2.4 |
| MCP gobby-cron:create_cron_job | #21570; 3.4 service/schema handoff |
| MCP gobby-cron:delete_cron_job | #21570; 3.4 service/schema handoff |
| MCP gobby-cron:get_cron_job | #21570; 3.4 service/schema handoff |
| MCP gobby-cron:list_cron_jobs | #21570; 3.4 service/schema handoff |
| MCP gobby-cron:list_cron_runs | #21570; 3.4 service/schema handoff |
| MCP gobby-cron:run_cron_job | #21570; 3.4 service/schema handoff |
| MCP gobby-cron:toggle_cron_job | #21570; 3.4 service/schema handoff |
| MCP gobby-cron:update_cron_job | #21570; 3.4 service/schema handoff |
| HTTP GET /api/admin/cron/protected-runs | 3.3.1 |

## P1: Portable schedule and storage contracts
`kind: framing`

### 1.1 Characterize Cron behavior before takeover [category: test]
`kind: deliverable`

Targets:
- `tests/contracts/cron/schedules.json`
- `tests/contracts/cron/services.json`
- `tests/contracts/cron/lifecycle.json`
- `tests/contracts/http/cron.json`
- `tests/contracts/http/manifest.json::*` — scope-reason: register the Cron corpus with Python origin
- `tests/contracts/test_cron_oracle.py`

**Research context:** Existing oracles are
`src/gobby/storage/cron_schedule.py::compute_next_run`,
`src/gobby/storage/cron.py::CronJobStorage`,
`src/gobby/storage/cron_runs.py::CronRunStorageMixin`,
`src/gobby/servers/routes/cron.py::create_cron_router` and
`src/gobby/mcp_proxy/tools/cron.py::create_cron_registry`.
Existing fixtures live in tests/scheduler and the isolated HTTP corpus harness.
Record frozen-clock/UUID vectors before deleting Python. Include croniter's
five fields, seconds-last sixth field and seventh year, aliases, steps, ranges,
names, day OR rules, leap days, DST folds/gaps, bad zones, invalid expressions,
interval clamping at 60 seconds, disabled and expired once jobs. Exercise all
request fields and omitted/null distinctions, brief/full output, display names,
project scope, name uniqueness, parked rows and run child hydration. Record
status/body plus resulting rows for writes and dispatch calls for actions.
Normalize only generated IDs, clock fields and measured durations with existing
corpus mechanisms. Do not normalize statuses, errors, ownership or child links.
Observed: source inspection only; these new tests have not run.
Planned: isolated pytest of test_cron_oracle and HTTP corpus recording/replay.

**Granularity:** The three vector files characterize one cross-language parity
interface, with no implementation or domain ownership transferred by this leaf.

**Acceptance:**
- 1.1.1 - Schedule vectors reproduce Python results at fixed instants, including field order and DST. test: `tests/contracts/test_cron_oracle.py::test_schedule_vectors_match_python`.
- 1.1.2 - Service vectors reproduce CRUD, parking, filtering, protection and serialization. test: `tests/contracts/test_cron_oracle.py::test_service_vectors_match_python`.
- 1.1.3 - Lifecycle vectors reproduce admission conflicts, durable children, backoff and recovery. test: `tests/contracts/test_cron_oracle.py::test_lifecycle_vectors_match_python`.
- 1.1.4 - The HTTP fixture records all ten owned routes and error branches, with Python origin. file: `tests/contracts/http/cron.json`.

### 1.2 Native schedule and value model [category: code] (depends: 1.1, #21559)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gcron/Cargo.toml`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/model.rs`
- `crates/gcron/src/schedule.rs`
- `crates/gcron/tests/schedule.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pins for this workspace-source change

**Research context:** Port the wire fields of
`src/gobby/storage/cron_models.py::CronJob` and `CronRun`, and the next-instant
contract of `src/gobby/storage/cron_schedule.py::compute_next_run`.
The minimum interval is 60 seconds; invalid stored zones fall back to UTC;
system registrations resolve configured zones against the host local zone.
Use explicit Clock input internally so tests freeze time without a second clock
service. New schedule::next_run returns Option<UTC instant> and typed validation
errors. Normalize croniter's seconds-last expressions to the parser's seconds
position, aliases and day semantics before evaluation; keep timezone and
calendar compatibility in this module. Restrict the parser's additional syntax
to what the Python API accepted. A vector difference requires a compatibility
fix, never silent schedule widening or loss. UTF-8/JSON/timestamps retain Python
wire formatting. This leaf adds no ticking task or database migrations.
Planned: `CARGO_BUILD_JOBS=3 cargo test -p gobby-cron --test schedule`, plus pins.

**Granularity:** More than six production targets are the package setup and its
required release carriers. One pure schedule/value contract remains the outcome.

**Acceptance:**
- 1.2.1 - Every schedule vector gives the Python next instant or no next instant, and accepts/rejects the same grammar. test: `crates/gcron/tests/schedule.rs::schedule_vectors_match_python`.
- 1.2.2 - Job/run/child JSON matches full and brief service vectors, preserving aware UTC values and nulls. test: `crates/gcron/tests/schedule.rs::models_match_python_wire`.
- 1.2.3 - gcode, gdaemon and ghook receive this leaf's patch bumps, lock and pins. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.3 Native job repository and system-row mutations [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gcron/src/jobs.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/system_jobs.rs`
- `crates/gcron/tests/jobs.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the gcron dependency change

**Research context:** Reuse the existing cron_jobs schema and gcore PostgreSQL
pool. `src/gobby/storage/cron.py::CronJobStorage` owns create/update/delete,
system protection, parked-state-preserving reconciliation, project cleanup,
time-zone normalization and obsolete automation-row removal. New Jobs methods
mirror these responsibilities, including exact duplicate-name behavior and
the distinct system-bookkeeping update path. Parking is enabled=true with
next_run_at=NULL; wake recomputes the schedule. Ordinary toggle/edit/delete
cannot alter protected system definitions. Registration is a name-unique
upsert that preserves a parked row while repairing definition drift.
Read existing project deletion callers before preserving their batch contract;
native bindings are owned by 2.7. No new generic repository layer is needed.
Planned: isolated `cargo test -p gobby-cron --test jobs` and release-pin pytest.

**Acceptance:**
- 1.3.1 - CRUD, filtering, prefix cleanup, timestamp/interval normalization and duplicate names match service vectors. test: `crates/gcron/tests/jobs.rs::job_mutations_match_python`.
- 1.3.2 - System protection, bookkeeping, park/wake and idempotent drift reconciliation preserve parked state across restart. test: `crates/gcron/tests/jobs.rs::system_rows_remain_protected_and_parked`.
- 1.3.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P2: One hub scheduler and native action bindings
`kind: framing`

### 2.1 Atomic scheduled and manual admission [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gcron/src/runs.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/admission.rs`
- `crates/gcron/tests/admission.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for native admission

**Research context:** Existing owners are
`src/gobby/scheduler/scheduler.py::CronScheduler._create_scheduled_run` and
`src/gobby/storage/cron_runs.py::CronRunStorageMixin.create_run_if_admitted`.
Use the same transaction-level advisory key as
`src/gobby/storage/hub/postgres_pool.py::advisory_lock_keys` for CronRunAdmission
("cron_run_admission"), including its actual PostgreSQL key derivation. Retain
the existing active-run partial unique index. Scheduled admission checks
machine capacity, CASes the selected due timestamp and inserts the running row
in one transaction; failed insert rolls back the schedule claim. Manual run
uses the same admission lock and machine capacity, never advances next_run_at,
and distinguishes missing job, active job (409) and capacity exhausted (429).
Run ownership is local machine UUID plus a fresh scheduler_owner per daemon.
Dispatched children do not consume scheduler slots; overlap still follows
active child state. Reuse child hydration queries instead of a second ledger.
Planned: `cargo test -p gobby-cron --test admission` against isolated PostgreSQL,
including competing native/native and native/Python transactions.

**Acceptance:**
- 2.1.1 - Two contenders claim one scheduled occurrence, and insertion failure leaves the original due timestamp. test: `crates/gcron/tests/admission.rs::scheduled_claim_is_atomic_and_interoperable`.
- 2.1.2 - Manual runs respect machine capacity and per-job uniqueness without moving schedules; all three rejection cases remain distinct. test: `crates/gcron/tests/admission.rs::manual_admission_matches_python`.
- 2.1.3 - Run read/list/child hydration match lifecycle vectors without mutating foreign-machine rows. test: `crates/gcron/tests/admission.rs::run_queries_preserve_owner_and_children`.
- 2.1.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.2 Scheduler tick, reconciliation and shutdown [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gcron/src/scheduler.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/recovery.rs`
- `crates/gcron/tests/scheduler.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the scheduler state machine

**Research context:** Port
`src/gobby/scheduler/scheduler.py::CronScheduler` with its storage/run helpers,
including due ordering, removed automation cleanup, run retention, backoff,
reconciliation and cron-session reaping when no jobs are due. A tick captures
one native active-config snapshot. Only failed increments consecutive_failures;
skipped/completed/dispatched reset it. Bind one Scheduler to the hub lease's
cancellation lifetime; standby and node own no tick task. Startup reconciliation
is machine scoped; orphan sweeps additionally restrict scheduler_owner and
never fail another live owner's work. Use Tokio task tracking and cancellation
already used by RuntimeState; no persisted second queue. Stop admission first,
then bounded drain/cancel/finalize owned inline runs while durable children
remain separately owned. Callback failures cannot undo committed completion.
Planned: `cargo test -p gobby-cron --test scheduler`, with frozen time,
two-machine fixtures, injected transaction/callback failures and crash restart.

**Acceptance:**
- 2.2.1 - Tick/backoff/retention and once-job disabling match lifecycle vectors. test: `crates/gcron/tests/scheduler.rs::ticks_and_bookkeeping_match_python`.
- 2.2.2 - Restart, stale and orphan handling affect only the intended machine/owner and active child links normalize correctly. test: `crates/gcron/tests/scheduler.rs::recovery_is_machine_and_owner_scoped`.
- 2.2.3 - Lease loss and shutdown stop admission, reap cron parent sessions and terminalize owned inline work without cancelling durable children. test: `crates/gcron/tests/scheduler.rs::shutdown_drains_and_releases_owned_work`.
- 2.2.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.3 Bounded shell and handler action execution [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `crates/gcron/src/executor.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/shell.rs`
- `crates/gcron/tests/executor.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for native inline actions

**Research context:** Read
`src/gobby/scheduler/executor.py::CronExecutor.execute`, `_coerce_action_result`,
`action_timeout_seconds`, `_execute_shell` and `_execute_handler`.
Keep four explicit action variants, a fixed handler-name match supplied by
gdaemon and typed ActionOutcome. Handler outer timeout comes from action_config
or CronConfig; shell uses its current command/args/cwd contract and 60-second
default. Spawn argv directly, never through a shell parser. Capture combined
stdout/stderr, replacement-decode invalid UTF-8, store full output and bound the
error tail at 2,000 characters. Timeout/cancellation kills and awaits the owned
process so no child is leaked. Normalize mapping/string results into completed,
failed or skipped and persist output/error before reporting completion.
Unknown actions/handlers and missing required config fail with typed errors.
Planned: `cargo test -p gobby-cron --test executor` with temporary fake commands.

**Acceptance:**
- 2.3.1 - Handler results, timeout selection, unknown actions and errors match lifecycle vectors. test: `crates/gcron/tests/executor.rs::inline_results_match_python`.
- 2.3.2 - Shell argv/cwd/output/failure tails match vectors, and timeout/cancellation leaves no process. test: `crates/gcron/tests/executor.rs::shell_actions_are_bounded_and_reaped`.
- 2.3.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.4 Durable agent dispatch and cron parent sessions [category: code] (depends: 2.3, #21562, #21564)
`kind: deliverable`

Targets:
- `crates/gcron/src/agent_action.rs`
- `crates/gcron/src/lib.rs`
- `crates/gdaemon/src/cron_agents.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gcron/tests/agent_action.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the native spawn consumer

**Research context:**
`src/gobby/scheduler/executor.py::CronExecutor._execute_agent_spawn` resolves the
definition/provider/workflow, project Git context, readiness and initial agent
variables, then creates a cron parent session and calls spawn_agent_impl.
Consume #21562's native session service and #21564's native spawn admission,
including its SRT gate and machine routing; do not create an alternate launcher.
The new gdaemon cron_agents binding passes identical project and lineage.
Overlap skip/readiness skip occurs before session or child creation. A missing
session manager/failed session creation is failure, never system-session fallback.
Persist the returned durable run ID before status=dispatched; an apparent
success with no ID fails. Recovery must find an already-created child after a
crash before cron finalization and link it, rather than launch a duplicate.
Planned: `cargo test -p gobby-cron --test agent_action`, then isolated native
spawn integration under the same admission service.

**Acceptance:**
- 2.4.1 - Definition/provider/project/lineage and restrictions reach native spawn unchanged; disabled/readiness/overlap skips have no child/session side effects. test: `crates/gcron/tests/agent_action.rs::agent_dispatch_preserves_admission_and_lineage`.
- 2.4.2 - Durable dispatch, missing-ID failure, parent reaping and crash reconciliation preserve one child per admitted run. test: `crates/gcron/tests/agent_action.rs::agent_children_are_durable_and_recoverable`.
- 2.4.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.5 Durable pipeline dispatch [category: code] (depends: 2.4, #21567)
`kind: deliverable`

Targets:
- `crates/gcron/src/pipeline_action.rs`
- `crates/gcron/src/lib.rs`
- `crates/gdaemon/src/cron_pipelines.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gcron/tests/pipeline_action.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the native pipeline consumer

**Research context:** Existing
`src/gobby/scheduler/executor.py::CronExecutor._execute_pipeline` checks active
children and pipeline.enabled before creating the cron session or execution.
Use #21567's native loader/execution service and #21562's session service.
cron.enabled gates scheduled admission; pipeline.enabled gates execution.
A disabled pipeline is terminal skipped and resets backoff. Enabled execution
requires a cron parent and persisted definition/input snapshot before dispatch.
Link pipeline_execution_id before status=dispatched, then let the native pipeline
owner execute it. Completion/failure updates cron status/history through one
binding, including background exceptions and cron parent closure. Reconcile
the create-child/finalize-cron crash boundary without duplicate execution.
Planned: `cargo test -p gobby-cron --test pipeline_action` and native pipeline
integration in isolated state; no Python background worker is an end state.

**Acceptance:**
- 2.5.1 - Disabled and overlap cases create neither session nor execution, and missing cron parent refuses before dispatch. test: `crates/gcron/tests/pipeline_action.rs::pipeline_gates_precede_side_effects`.
- 2.5.2 - Enabled dispatch preserves inputs, definition, project, lineage, child status and error propagation across crash recovery. test: `crates/gcron/tests/pipeline_action.rs::pipeline_dispatch_is_durable`.
- 2.5.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.6 Native code-index nightly registration and invocation [category: code] (depends: 2.5, #24706)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/cron_code_index.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/tests/cron_code_index.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for native code-index scheduling

**Research context:** #24706 ships
`gobby_codeindex::jobs::nightly_repair::run(jobs: &Arc<HubJobs>) -> String`;
its contract deliberately ships no tick and deletes no Python. This leaf
prepares/tests registration functions; production calls wait for 4.1. Existing
`src/gobby/code_index/nightly_repair.py::register_code_index_nightly_repair_cron`
owns the job name gobby:code-index-nightly-repair and handler
code-index:nightly-repair. Register these exact names through 1.3's native
system-row reconciliation with configured expression, local timezone, enabled
state and concurrency. Invoke the native handler directly with the daemon's
HubJobs, using its non-overlap guard and returned summary. Native jobs do not
read nightly_repair_timeout_seconds or maintenance_log_file; remove those
obsolete fields from the repaired action_config without deleting the config
keys themselves (#24713). Do not wrap the handler in the retired per-project
Python repair timeout. Also bind the native hourly-prune handler already
delivered upstream of #24706. Planned: `cargo test -p gobby-daemon --test
cron_code_index`; assert one tick owner and one real native invocation.

**Acceptance:**
- 2.6.1 - New/drifted/disabled/parked nightly definitions reconcile correctly and preserve local-zone scheduling. test: `crates/gdaemon/tests/cron_code_index.rs::nightly_definition_preserves_operator_state`.
- 2.6.2 - A due run calls jobs::nightly_repair once, persists its summary, and neither invokes Python nor imposes the retired project timeout; prune binds its native handler. test: `crates/gdaemon/tests/cron_code_index.rs::native_code_index_jobs_execute_once`.
- 2.6.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.7 Native system-job bindings [category: code] (depends: 2.6, #21563, #21585, #21589)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/cron_system_jobs.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/tests/cron_system_jobs.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the native system-job consumers

**Research context:** Registration call sites observed by
`gcode grep -F 'register_handler(' src/gobby/ -m 80` are
`src/gobby/memory/dream/cron.py`, `src/gobby/feedback/cron.py`,
`src/gobby/projects/purge.py`, and the two code-index modules.
Bind #21563's dream admission/service, #21589's feedback review and #21585's
project-purge service to fixed handler names through the native executor.
Carry each existing registration's schedule, timeout, enabled policy and
parked-state reconciliation. Dream's outer budget includes its maximum runtime,
work-unit budget and finalization grace; restart_protected remains declared in
action_config. Domain owners retain their business logic and durable recovery.
Project purge calls the native job disable/delete methods from 1.3; any consumer
adapter belongs in this binding, not duplicated SQL. Missing required native
services make readiness degraded and prevent starting the production scheduler;
individual disabled jobs stay side-effect free. No Python handler fallback.
Registration/execution stay passive in production until 4.1; this leaf
exercises them only in isolated fixtures.
Planned: `cargo test -p gobby-daemon --test cron_system_jobs` with real native
service constructors and isolated databases; fake business effects, not Python.

**Acceptance:**
- 2.7.1 - Dream, feedback and purge invoke the proper native service with the original job/project contract and no duplicate schedule owner. test: `crates/gdaemon/tests/cron_system_jobs.rs::system_handlers_use_native_services`.
- 2.7.2 - Existing parked/disabled rows, definition drift and dream timeout/restart protection survive reconciliation. test: `crates/gdaemon/tests/cron_system_jobs.rs::system_job_policies_survive_restart`.
- 2.7.3 - Purge's native consumer disables/deletes only its project's jobs and cleans removed automation definitions. test: `crates/gdaemon/tests/cron_system_jobs.rs::project_cleanup_uses_native_jobs`.
- 2.7.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 2.8 Passive hub runtime wiring and completion events [category: code] (depends: 2.7, #21575, #21558)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/cron_runtime.rs`
- `crates/gdaemon/src/lib.rs`
- `crates/gdaemon/src/state.rs::*` — scope-reason: construct and cancel the single Cron owner with the hub lease
- `crates/gdaemon/src/state_services.rs::*` — scope-reason: publish the Cron service handle alongside native services
- `crates/gdaemon/tests/cron_runtime.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for hub runtime wiring

**Research context:** Reuse
`crates/gdaemon/src/state.rs::RuntimeState::prepare` and RuntimeState::run,
the hub singleton lease and #21575's role gates. Python starts Cron via
`src/gobby/runner_lifecycle_subsystems.py::_start_cron_scheduler` in a hub-only
phase. This leaf constructs a passive native service handle and installs
bindings; it starts no production tick, cleanup, reconciliation or system-row
registration and admits no native manual action. Its activation entry point is
called only by 4.1's restart-bound owner transfer. Proxy/compare keep the Python
owner, including run-now; native action/runtime tests activate only isolated
fixtures. No partially landed leaf can activate Rust scheduling. After 4.1
activates the handle, lease loss cancels admission before another owner starts.
`src/gobby/runner_broadcasting.py::setup_cron_event_broadcasting` maps completed,
failed, skipped and dispatched into run_completed/run_failed/run_skipped/
run_dispatched. Publish the same job/run payload via the native WS broker and
retain automation fired/succeeded/failed accounting. Events follow committed
rows, never announce an uncommitted transition, and callback failure cannot
fail the job. Remote node requests use the existing hub route; node, standby
and degraded startup allocate no scheduler tasks or system-job mutations.
Planned: `cargo test -p gobby-daemon --test cron_runtime` with lease loss,
two roles, config changes and broker failure injection.

**Granularity:** Runtime admission and its cancellation/event wiring share one
owner. More than six targets include required release carriers, not a second
runtime state machine.

**Acceptance:**
- 2.8.1 - Before 4.1 activation, production has no native cron tasks, mutations or manual admission; isolated activation starts one hub/standalone owner and none on node/standby/degraded state. test: `crates/gdaemon/tests/cron_runtime.rs::roles_and_lease_gate_cron_ownership`.
- 2.8.2 - Live config is captured once per tick, lease loss stops admission, and committed transitions publish the expected WS/accounting events despite observer failure. test: `crates/gdaemon/tests/cron_runtime.rs::cron_events_follow_committed_state`.
- 2.8.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P3: HTTP family and front-door service handoffs
`kind: framing`

### 3.1 Cron family and job/run read routes [category: code] (depends: 2.8)
`kind: deliverable`

Targets:
- `crates/gcron/src/family.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/http_reads.rs`
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: register the Cron family using the existing routing seam
- `crates/gcron/tests/http_reads.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the Cron HTTP family

**Research context:** #21559 supplies RouteFamily::claims/check_proxied and
compare reporting. Existing
`src/gobby/servers/routes/cron.py::create_cron_router` reads full job payloads,
effective display names from project names, filters removed automation jobs,
and bounds history limit to 1..100 (default 20). Implement a CronFamily claiming
only the listed methods/segments under /api/cron and the one protected-run
route added by 3.3, never all admin paths. Preserve URI decoding, UUID errors,
project authorization, status envelopes, count, sort, nullable fields and
unavailable/404/422/500 behavior from the oracle. Reuse the runtime family's
principal admission; browser/cookie requests must reach the equivalent native
auth path at final takeover rather than fall back to deleted Python. The family
defaults to Proxy until 4.1; compare mutations call only Python. Registering
these native routes does not activate their production scheduler handle.
Planned: `cargo test -p gobby-cron --test http_reads` and HTTP corpus replay.

**Granularity:** The four reads are one read-only family surface over the same
repository, with one acceptance per ledger row. Shared release targets account
for the inventory threshold.

**Acceptance:**
- 3.1.1 - GET /api/cron/jobs matches filtering, count, display names and errors. test: `crates/gcron/tests/http_reads.rs::list_jobs_matches_python`.
- 3.1.2 - GET /api/cron/jobs/{job_id} matches existing, missing and malformed IDs. test: `crates/gcron/tests/http_reads.rs::get_job_matches_python`.
- 3.1.3 - GET /api/cron/jobs/{job_id}/runs matches bounds, ordering and child hydration. test: `crates/gcron/tests/http_reads.rs::list_runs_matches_python`.
- 3.1.4 - GET /api/cron/runs/{run_id} matches row and not-found behavior. test: `crates/gcron/tests/http_reads.rs::get_run_matches_python`.
- 3.1.5 - Family claims, principals, node-to-hub routing and method/path boundaries enforce the existing authorization contract. test: `crates/gcron/tests/http_reads.rs::claims_and_auth_are_bounded`.
- 3.1.6 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.2 Job mutations and immediate execution routes [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcron/src/http_writes.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/family.rs` — planned new file: extend claims and routing to the five owned mutation routes
- `crates/gcron/tests/http_writes.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for Cron HTTP writes

**Research context:** Port
`src/gobby/servers/routes/cron.py::CreateCronJobRequest`, UpdateCronJobRequest,
_validate_schedule_fields, create_job, update_job, delete_job, toggle_job and
run_job_now. Reuse native Jobs and Scheduler admission, never route-local SQL
or an alternate manual executor. Match project resolution, required schedule
fields, enum/type validation, omitted versus null values, ignored empty update
fields, protection status 403 and exact existing error envelopes. Run-now
returns the admitted run and maps active conflict to 409, capacity to 429 and
missing scheduler to 503. Mutating compare requests execute Python only once;
3.5 adds observation of committed effects, never a second native action.
Planned: `cargo test -p gobby-cron --test http_writes` and shared corpus replay.

**Acceptance:**
- 3.2.1 - POST /api/cron/jobs matches create validation, project resolution, status/body and stored rows. test: `crates/gcron/tests/http_writes.rs::create_job_matches_python`.
- 3.2.2 - PATCH /api/cron/jobs/{job_id} matches partial fields, no-fields error and system protection. test: `crates/gcron/tests/http_writes.rs::update_job_matches_python`.
- 3.2.3 - DELETE /api/cron/jobs/{job_id} matches success, missing and protected rows. test: `crates/gcron/tests/http_writes.rs::delete_job_matches_python`.
- 3.2.4 - POST /api/cron/jobs/{job_id}/toggle matches ordinary rows and refuses system rows. test: `crates/gcron/tests/http_writes.rs::toggle_job_matches_python`.
- 3.2.5 - POST /api/cron/jobs/{job_id}/run uses shared admission and matches 200/404/409/429/503. test: `crates/gcron/tests/http_writes.rs::run_now_matches_python`.
- 3.2.6 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.3 Native restart protection query and lifecycle consumers [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gcron/src/protection.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/family.rs` — planned new file: claim only the exact protected-runs admin route
- `crates/gdaemon/src/lifecycle/cron_guard.rs`
- `crates/gdaemon/src/lease/admin_api.rs::*` — scope-reason: expose a bounded native all-active count for local cooperative handoff
- `crates/gdaemon/src/lifecycle/mod.rs`
- `crates/gdaemon/src/main.rs::*` — scope-reason: enforce the shared Cron guard from native lifecycle command entry points
- `crates/gdaemon/src/cron_runtime.rs` — planned new file: expose owner-local protection and forced cancellation to lifecycle commands
- `crates/gdaemon/tests/cron_protection.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for native restart safety

**Research context:** The ledger explicitly assigns this admin route to Cron.
`src/gobby/scheduler/scheduler.py::CronScheduler.list_protected_runs` selects
active rows owned by the current scheduler_owner, reads restart_protected from
action_config and excludes elapsed time beyond action_timeout_seconds. Preserve
job/run/name/start/elapsed/remaining fields and unavailable behavior from
`src/gobby/servers/routes/admin/_lifecycle.py::protected_cron_runs`.
Native lifecycle consumers call the same guard without HTTP loopback inside
Rust; CLI verb ownership stays with #21571. Normal refusal, wait-until-drained and force-interrupt semantics
remain explicit. Force cancels/finalizes the owned run; dream resumes through
its existing durable domain recovery. Lease takeover and stale rows cannot
hold the guard forever. #21571 consumes this guard through the operator-verb
contract; that CLI routing remains delegated. Planned: `cargo test -p
gobby-daemon --test cron_protection` with a fake clock and isolated restart.

Cooperative handoff needs a separate predicate: all pending/running cron rows
for the daemon's local machine, irrespective of restart_protected, elapsed
time or scheduler_owner. Reuse Runs::count_running from 2.1. Add exactly
`GET /api/admin/cron/active-run-count` to the existing native lease-admin control
seam, independently of the switchable Cron family router, so it stays native
while that family is Proxy/Compare. Require the existing authenticated operator
or backend control principal; accept no machine-id selector. Return
`{"machine_id": "<local UUID>", "active_cron_runs": <nonnegative integer>}`.
Missing identity, database failure or unsupported node role gives a typed
non-200 refusal; never synthesize zero. This read requires no scheduler
activation and makes no mutations. Python's temporary client uses the existing
daemon client's bounded timeout; Rust calls the same count directly.
Protected-run reporting cannot substitute for this count.

**Granularity:** Query and lifecycle guard share the same restart lease; testing
them together proves that reporting and enforcement cannot diverge. Release
carriers exceed the file-count inspection threshold.

**Acceptance:**
- 3.3.1 - GET /api/admin/cron/protected-runs returns the original fields and timeout/owner filtering. test: `crates/gdaemon/tests/cron_protection.rs::protected_runs_query_matches_python`.
- 3.3.2 - Native lifecycle refuses protected work, waits when requested and force-interrupts only owned protected work; stale/expired leases do not wedge restart. test: `crates/gdaemon/tests/cron_protection.rs::restart_guard_honors_wait_and_force`.
- 3.3.4 - The authenticated control query counts ordinary unprotected local active runs, excludes foreign-machine work and fails closed on unavailable identity/database; it stays native in Proxy/Compare. test: `crates/gdaemon/tests/cron_protection.rs::active_count_is_local_complete_and_fail_closed`.
- 3.3.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.4 Native service contract for CLI and MCP owners [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gcron/src/service.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/tests/service_contract.rs`
- `docs/contracts/cron-services.md`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the service contract exported to front-door owners

**Research context:** Literal consumers are
`src/gobby/cli/cron.py::get_cron_storage` / _resolve_job_id and
`src/gobby/mcp_proxy/tools/cron.py::create_cron_registry`. CLI additionally owns
schedule shorthands, project references and JSON/text rendering. MCP owns
restricted-caller admission and its declared schemas. Export concrete native
functions over Jobs/Scheduler for create/get/list/update/delete/toggle/run/history,
park/wake, complete local active-run count and ID-or-name resolution; errors remain typed. Do not add a second
transport or public park/wake HTTP route merely for these in-process consumers.
Document a name-by-name map for all ten CLI and eight MCP rows, input/default
schemas, output/errors and ownership. Restricted agents cannot turn on shell
execution or bypass protected rows; front-door owners perform their existing
admission before calling these functions. #21570 owns the MCP adapter and
Python registry removal; #21571 owns client command rendering and Python CLI
removal at S3.2. Retained Python storage helpers still serve other Python
consumers during transition and are retired with their last owner at the
naming switch, never kept as a supervised daemon dependency.
Planned: `cargo test -p gobby-cron --test service_contract` over 1.1 vectors.

**Acceptance:**
- 3.4.1 - Every delegated CLI/MCP operation maps to one native service with matching defaults/results/errors, including park/wake and ID-or-name lookup. test: `crates/gcron/tests/service_contract.rs::delegated_operations_match_service_vectors`.
- 3.4.2 - Restricted-caller shell/protection contracts and the explicit #21570/#21571 removal handoffs are complete. file: `docs/contracts/cron-services.md`.
- 3.4.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.5 Side-effect-safe comparison and native corpus replay [category: code] (depends: 3.4)
`kind: deliverable`

Targets:
- `crates/gcron/src/compare.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/family.rs` — planned new file: implement the existing proxied-result verification hook
- `crates/gcron/tests/compare.rs`
- `tests/contracts/http/manifest.json::*` — scope-reason: mark only proven Cron HTTP cases as native with Python origin
- `tests/e2e/test_cron_family.py`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for the Cron compare verifier

**Research context:** #21559's Compare dispatch executes read-only native legs
and proxies writes once before calling check_proxied. Cron list/run state can
change between observations. Snapshot the relevant native row version/child
state for a read; a changed row makes the comparison skipped with a bounded
reason, never a false match. For writes verify Python's committed post-image
and response or typed rejection without repeating the mutation. For run-now
read the returned run ID and child link; never execute another run. A row that
advanced/disappeared during observation is skipped and must later receive a
stable controlled case for soak coverage. Store pointers/reasons, not action
config, commands, prompts or output. Replay all ten HTTP routes in isolated
native mode and retain the recorded Python origin. Planned: crate comparison
tests and isolated e2e compare/replay with one fake child counter.

**Granularity:** More than six target files include release and corpus carriers;
the single outcome is trustworthy compare evidence for this family.

**Acceptance:**
- 3.5.1 - Reads and verified writes detect tampered response/state, skip concurrent changes and never duplicate writes or actions. test: `crates/gcron/tests/compare.rs::compare_is_side_effect_free_and_race_aware`.
- 3.5.2 - All ten owned HTTP cases replay equal natively, with authentication and failure cases and no Python service needed by the native leg. test: `tests/e2e/test_cron_family.py::test_native_cron_corpus_matches_python`.
- 3.5.3 - A controlled compare window covers every owned route and action kind with zero mismatches and nonzero stable evidence per case. test: `tests/e2e/test_cron_family.py::test_compare_has_coverage_without_double_dispatch`.
- 3.5.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P4: Native takeover and Python retirement
`kind: framing`

### 4.1 Native default and single-owner scheduler cutover [category: code] (depends: 3.5)
`kind: deliverable`

Targets:
- `crates/gcron/src/family.rs` — planned new file: make Native the family default after parity
- `crates/gdaemon/src/cron_runtime.rs` — planned new file: coordinate scheduler admission with the takeover boundary
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: replace Python cron construction with the bounded transition binding
- `src/gobby/runner_init/orchestration_services.py`
- `src/gobby/runner_cron_client.py`
- `tests/scheduler/test_native_cron_transition.py`
- `src/gobby/servers/routes/admin/_lease.py::*` — scope-reason: consume the complete native local active-run count for cooperative handoff
- `tests/servers/routes/test_daemon_lease_routes.py::*` — scope-reason: preserve unprotected-run and foreign-machine handoff behavior
- `tests/e2e/test_cron_family.py` — planned new file: prove native default, backend independence and pre-retirement rollback
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for native takeover

**Research context:** RuntimeState's hub lease owns Rust services; Python's
init_orchestration previously constructed CronExecutor and CronScheduler.
This is the sole production activation leaf: both scheduled and manual native
admission remain closed through P3. Resolve ownership once at the existing
daemon restart boundary from the same startup routing selection used by the
Python construction path. Proxy/compare allow only Python admission; Native
allows only native admission. Drain/stop the outgoing process owner before
starting the incoming owner. Do not change ownership on a live config refresh.
Refuse startup if the outgoing owner's exclusion cannot be proved; no route
claim, cleanup sweep or system registration may open admission first.
Split unrelated remaining service construction out of
`src/gobby/runner_init/orchestration.py` into the new
`src/gobby/runner_init/orchestration_services.py`, keeping both below the ceiling.
The minimal `src/gobby/runner_cron_client.py` uses the existing authenticated
daemon HTTP client for run-now, protected-run queries and the distinct native
all-active local count control query from 3.3. It holds no executor,
tick or database mutation logic. Disable Python ticking before native admission
starts; forward necessary transitional Python manual-execution consumers to
these native routes. This client is removed by its last front-door owner at
S3.2, alongside the Python CLI; 4.2 removes every Python execution path.
Wire `_active_work_blockers` to count_active_runs through its existing
server.run_db offload. All pending/running local work blocks handoff, including
unprotected jobs. A query refusal/timeout fails the handoff closed; never
replace the old count with the length of the protected-runs list.
Mode selection is the existing family routing configuration, not
a new feature flag. A takeover failure restores the previous admission owner
before allowing requests. Drain owned inline work before transfer; durable
children keep their links. No two owners tick during native/compare/proxy
transitions. Native is the default only after the 3.5 corpus and compare checks.
Before Python deletion, proxy rollback and return to default require a routing
edit/restart of the same binaries. All actions and the protected-run guard work
with Python killed on the native default. Planned: isolated scoped transition
pytest and test_cron_family e2e; never kill the operator's backend.

**Granularity:** The transition binding and native default share the single
admission-owner transfer. More than six files are wiring, tests and releases.

**Acceptance:**
- 4.1.1 - Native is the default only after the restart-bound transfer; Python/native never both admit scheduled or manual work, in-flight runs survive transfer, and pre-deletion proxy rollback excludes native before restoring Python with the same binaries. test: `tests/e2e/test_cron_family.py::test_default_flip_has_one_scheduler_owner`.
- 4.1.2 - With isolated Python killed, all ten routes, four action kinds, nightly repair, system jobs and restart protection still work in hub/standalone; nodes create no jobs/runs. test: `tests/e2e/test_cron_family.py::test_cron_survives_python_backend_loss`.
- 4.1.3 - Transition construction cannot leave Python scheduling live when native admission is enabled or native unavailable. test: `tests/scheduler/test_native_cron_transition.py::test_transition_never_has_two_admission_owners`.
- 4.1.5 - Cooperative handoff refuses an ordinary unprotected local active run, ignores foreign-machine work and fails closed when the native count is unavailable. test: `tests/servers/routes/test_daemon_lease_routes.py::test_handoff_uses_complete_local_native_cron_count`.
- 4.1.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.1a Native delivered-mailbox retention takeover [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gcron/src/mailbox_retention.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/scheduler.rs`
- `crates/gcron/tests/mailbox_retention.rs`
- `crates/gdaemon/src/cron_runtime.rs`
- `src/gobby/runner_maintenance/messaging.py::*` — scope-reason: delete only the mailbox adapter branch from communications cleanup
- `tests/communications/test_cleanup.py::*` — scope-reason: preserve communications and attachment cleanup after mailbox extraction
- `tests/communications/test_communications_maintenance.py::*` — scope-reason: stop expecting the removed mailbox adapter call
- `tests/test_runner_maintenance_offload.py::*` — scope-reason: retain offload assertions for the remaining communications work
- `tests/test_runner_maintenance_startup.py::*` — scope-reason: preserve startup pacing for the remaining cleanup loop
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for native mailbox retention

**Research context:** Orchestrator's 2026-10-24 ruling is to port current mailbox
retention now; #22340 remains parked and its unapproved channel-schema draft
supplies no implementation dependency. Current
`src/gobby/runner_maintenance/messaging.py::cleanup_comms_messages_loop` calls
`src/gobby/storage/inter_session_messages.py::InterSessionMessageManager.delete_delivered_before`.
That SQL deletes rows by delivered_at strictly before the UTC cutoff, in
(delivered_at, id) order, at most 500 per cycle. Undelivered rows and rows on
the cutoff survive. Use that existing table and predicate directly in the
native function; add no migration, channel schema, compatibility selector or
new configuration key. Return the deletion count for existing cleanup logging.

The production call in runner_lifecycle_periodic passes no overrides: retention
is 30 days, interval 86,400 seconds measured after a completed attempt, and
startup uses the recurring helper's deterministic 1..300-second offset. The
current comms-message-cleanup name resolves to 194 seconds (BLAKE2s with
four-byte output); preserve that value without adding a hashing dependency.
Existing fixture overrides of cutoff, interval and startup delay remain test
inputs. There is no mailbox-specific enable switch: this hub/standalone
maintenance duty runs even when ordinary cron.enabled is false, and never
runs on a node or standby. It shares the native scheduler's lease/cancellation
lifetime, consumes no job-admission slot and creates no new user job.
After an error log it waits for the next cycle; cancellation/shutdown stops it.
One bounded batch per cycle prevents an unbounded catch-up sweep.

Wire this duty only in this leaf, using 4.1's restart-bound exclusion: the
Python mailbox branch is absent before native maintenance starts. Delete only
the InterSessionMessageManager import/construction, its prune call and its
mailbox-specific log branch from cleanup_comms_messages_loop. Preserve
communications-store pruning, attachment deletion/cleanup, their cadence and
the unrelated zombie-message loop. #22340's later approved channel-schema work
owns replacement with its eventual service; Cron never waits on that planning
task and does not unpark it. Planned: isolated
`cargo test -p gobby-cron --test mailbox_retention` and focused communications,
maintenance offload/startup pytest, with frozen clocks and two-role fixtures.

**Granularity:** The native retention callback and removal of its only Python
caller are one maintenance-owner transfer. More than six targets are the
required consumer tests and release carriers; communications business logic
remains outside this leaf.

**Acceptance:**
- 4.1a.1 - Retention deletes only delivered mailbox rows strictly before the 30-day UTC cutoff, ordered and bounded at 500; undelivered, exact-cutoff and overflow rows survive. test: `crates/gcron/tests/mailbox_retention.rs::current_mailbox_retention_matches_python`.
- 4.1a.2 - One hub/standalone owner preserves the 194-second startup delay and daily post-attempt cadence even with cron.enabled=false; node/standby, errors and shutdown preserve the stated bounds. test: `crates/gcron/tests/mailbox_retention.rs::retention_lifecycle_preserves_current_policy`.
- 4.1a.3 - The Python mailbox branch is absent before native retention activates, while communications and attachment cleanup retain bounded offload/startup behavior. test: `tests/communications/test_communications_maintenance.py::test_native_mailbox_takeover_preserves_other_cleanup`.
- 4.1a.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.2 Delete Cron-owned Python and release the nightly cleanup boundary [category: code] (depends: 4.1a)
`kind: deliverable`

Targets:
- `src/gobby/scheduler/scheduler.py::*` — operation: delete — scope-reason: native scheduler owns ticking and manual admission
- `src/gobby/scheduler/executor.py::*` — operation: delete — scope-reason: all four action variants have native bindings
- `src/gobby/servers/routes/cron.py::*` — operation: delete — scope-reason: ten owned HTTP routes are native
- `src/gobby/code_index/nightly_repair.py::*` — operation: delete — scope-reason: native scheduler calls the #24706 handler
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: remove transition and nightly registration consumers while retaining other orchestration
- `src/gobby/runner_init/orchestration_services.py`
- `src/gobby/runner.py::*` — scope-reason: remove Python scheduler/nightly fields and their type imports
- `src/gobby/runner_cron_client.py`
- `src/gobby/runner_init/servers.py::*` — scope-reason: route retained Python callers through the native service client and drop executor/event wiring
- `src/gobby/runner_lifecycle_subsystems.py::*` — operation: delete-lines — base-blob: e37cc910330a11e0df8f3bf9c606fa5113bb54ba — lines: 52, 338-339, 746 — scope-reason: remove only the Python Cron startup phase and function
- `src/gobby/runner_lifecycle_shutdown.py::*` — operation: delete-lines — base-blob: b2aca3939c682863c87789d2f67d9a6f84d17afd — lines: 355-364 — scope-reason: remove only the Python Cron shutdown block
- `src/gobby/runner_broadcasting.py::*` — scope-reason: remove only Cron completion wiring now emitted by the native broker
- `src/gobby/servers/_app_routes.py::*` — scope-reason: remove the deleted Cron router import/registration
- `src/gobby/servers/routes/admin/_lifecycle.py::*` — scope-reason: native protected-run query and guard replace Python Cron references
- `src/gobby/app_context.py::*` — scope-reason: replace Python scheduler type contract with the transition client for retained callers
- `src/gobby/servers/http.py::*` — scope-reason: supply the native Cron client to retained Python registry callers
- `src/gobby/mcp_proxy/tools/cron.py::*` — scope-reason: replace deleted scheduler type/error imports with the native client while retaining #21570's wrapper ownership
- `src/gobby/runner_init/project_purge.py::*` — scope-reason: remove the deleted executor type dependency from retained domain initialization
- `src/gobby/projects/purge.py::*` — scope-reason: replace the deleted CronHandler type import with its standard-library callable annotation
- `src/gobby/code_index/prune.py::*` — scope-reason: remove a remaining CronHandler type import if the pruner has not already retired upstream
- `tests/scheduler/test_native_cron_transition.py`
- `tests/scheduler/test_cron_executor.py::*` — operation: delete — scope-reason: executor behavior now covered by native action suites
- `tests/scheduler/test_cron_scheduler.py::*` — operation: delete — scope-reason: native scheduler suite covers this retired implementation
- `tests/scheduler/test_cron_integration.py::*` — operation: delete — scope-reason: native end-to-end cases replace Python scheduler integration
- `tests/scheduler/test_cron_machine_scope.py::*` — operation: delete — scope-reason: native owner-scope tests replace the Python executor fixtures
- `tests/scheduler/test_cron_runbook_chain.py::*` — operation: delete — scope-reason: native durable-dispatch cases retain the runbook-chain obligations
- `tests/scheduler/test_cron_session_lifecycle.py::*` — operation: delete — scope-reason: native parent-session recovery tests cover the retired executor
- `tests/scheduler/test_cron_shell_output.py::*` — operation: delete — scope-reason: native shell output and cleanup tests cover this behavior
- `tests/scheduler/test_system_automation_loop.py::*` — scope-reason: migrate only the fixtures importing the deleted scheduler/executor
- `tests/config/test_runtime_loop_consumers.py::*` — scope-reason: replace retired scheduler-capture tests with the native snapshot consumer checks
- `tests/mcp_proxy/test_cron_tools.py::*` — scope-reason: exercise native client errors instead of importing CronRunRejected from the deleted module
- `tests/reports/test_publishing_retirement.py::*` — scope-reason: retain removed-automation assertions using native service fixtures
- `tests/servers/test_http_cron.py::*` — operation: delete — scope-reason: the recorded native HTTP corpus covers the retired router
- `tests/test_runner_init.py::*` — scope-reason: remove deleted constructors from patch/failure fixtures and assert native client wiring
- `tests/code_index/test_nightly_repair.py::*` — operation: delete — scope-reason: removed implementation has native scheduler/handler coverage
- `tests/scheduler/test_cron_retirement.py`
- `tests/e2e/test_cron_family.py` — planned new file: verify operation after Python scheduler/HTTP/nightly deletion
- `docs/contracts/cron-services.md`

**Research context:** The literal cron_scheduler sweep finds app_context,
runner, runner_init/servers, runner_broadcasting, lifecycle start/stop,
HTTP service construction, MCP registry plumbing and admin lease/lifecycle.
Keep a minimal native-service client only for still-live Python CLI/MCP/domain
consumers; it has no tick/executor/business logic and is explicitly removed
by #21570/#21571's naming-switch ownership. Move remaining unrelated service
initialization out of `src/gobby/runner_init/orchestration.py` into
`src/gobby/runner_init/orchestration_services.py` from 4.1 as needed to keep the
former below 850. Keep retained client construction in
`src/gobby/runner_cron_client.py` from 4.1; each file stays below 1,000.
This is a bounded deletion/move, not a new runtime subsystem.

The two typed deletion proofs bind current file blobs. If an upstream landing
changes those bytes, re-resolve the same Cron-only ranges and refresh the proofs
before execution; a stale proof never authorizes a different deletion. The
startup helper currently spans 338-339 and the phase name/call are at 52/746.

Delete every nightly-repair import, field, initialization block and its Python
test. #24713 must find no production reader of the removed module; its later
shared maintenance_log/eligibility/breaker and two config-key cleanup stays
with that task. Keep shared storage/cron primitives only while other Python
callers still need them, with final removal owned at the naming switch.
Sweep removed symbol literals/imports over src and tests, migrate each owned
consumer/test to native coverage, and report delegated last consumers by exact
path in cron-services.md. Remove obsolete scheduler/executor-specific test
classes from tests/scheduler after preserving their behavior in the native
tests; preserve Python storage tests while their shared implementation remains.
No Python HTTP proxy or scheduler rollback branch survives this leaf.
Planned: focused retirement pytest, HTTP corpus replay, native Cron suites,
Ruff, mypy src and scoped changed-test type audit. No full pytest suite.

**Granularity:** Many targets remove one obsolete Python lifecycle owner and
its consumers atomically. Splitting by file would leave import/start/stop paths
broken between commits. Native behavior already has independently delivered
tests in P1-P3; this leaf verifies absence and retained-client boundaries.

**Acceptance:**
- 4.2.1 - Python scheduler/executor/HTTP/nightly modules and nightly init/test consumers are absent; no production import names them. test: `tests/scheduler/test_cron_retirement.py::test_removed_cron_implementations_have_no_readers`.
- 4.2.2 - Retained Python callers use only the native client; startup, stop, lease queries and MCP run-now never construct an executor or scheduler. test: `tests/scheduler/test_cron_retirement.py::test_retained_callers_have_no_python_execution_path`.
- 4.2.3 - Native routes/actions/nightly repair/restart protection work after source deletion and have no proxy fallback. test: `tests/e2e/test_cron_family.py::test_retired_python_is_unnecessary`.
- 4.2.4 - #24713's documented boundary has no nightly module reader, and all remaining transitional/shared-helper paths have a naming-switch removal owner. file: `docs/contracts/cron-services.md`.

### 4.3 Parity ledger and operator handoff [category: docs] (depends: 4.2)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json::*` — scope-reason: mark only the nine delivered Cron HTTP rows native and update delegated-row handoffs
- `docs/contracts/parity-ledger-status.d/daemon-config-auth-infra.json::*` — scope-reason: record the native protected-run query with its verified owner
- `docs/contracts/parity-ledger.md`
- `docs/contracts/cron-services.md`
- `docs/guides/configuration.md`

**Research context:** The ledger generator reads status fragments; edit the
fragments then regenerate its Markdown using the existing ledger tooling.
All 28 rows are accounted for in Parity ownership. Mark ten HTTP rows native
only after their corpus/backend-loss checks; keep ten CLI and eight MCP rows
delegated until #21571/#21570 deliver them. Document hub-only ownership, park/
wake, restart --wait/--force, the native default, pre-deletion routing rollback
and post-deletion prior-release rollback. Carry the S3.2 same-change Python
removal requirement and exact outstanding client/helper paths into the
front-door handoff. No new task expansion or operator action happens here.
Planned: ledger consistency check, scoped documentation validation and base/
expansion plan validation after the adversary stamps M1.

**Acceptance:**
- 4.3.1 - The two status fragments and regenerated ledger account for all 28 rows truthfully, with ten native HTTP rows and explicit CLI/MCP owners. file: `docs/contracts/parity-ledger.md`.
- 4.3.2 - The service handoff records native API mappings, remaining temporary client/helper paths and S3.2 deletion ownership without a Stage2-to-Stage3 cycle. file: `docs/contracts/cron-services.md`.
- 4.3.3 - Configuration guidance documents native ownership, parking, protected restart and the two rollback boundaries. file: `docs/guides/configuration.md`.

## E1 Verification and approval boundary
`kind: verification`

Authoring validation is `uv run gobby plans validate
.gobby/plans/gdaemon-cron-family.md -p /Users/josh/Projects/gobby`.
Expansion validation is unrun for a narrative-only draft. Once the adversary
derives/applies M1, both base validation and `--mode expansion` must pass.

Implementation checks remain planned: isolated focused Python oracle/retirement
tests, package-scoped native suites, native HTTP replay and the controlled
compare/backend-loss/owner-transfer cases. Production cutover waits for the
release owner and the protected-run guard. A passing validation parser does
not attest to future behavior. Josh approves the stamped plan before any
task expansion; this planning seat neither expands nor dispatches work.

## V1 Plan Changelog
`kind: framing`

2026-10-24: Accepted CRON-001/002/003: keep pre-cutover native services passive;
port current-schema mailbox retention under the Orchestrator ruling without
unparking #22340; preserve complete local active-run handoff counting through
a native control query. Revalidate these repairs before renewed review.

2026-10-24: Initial narrative prepared for #24952 after Orchestrator scope and
ownership confirmation. Includes the 28-row inventory, native nightly handler
dependency, Cron Python retirement and explicit CLI/MCP naming-switch handoffs.
