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
database service. Three nullable run-output metadata columns are the sole schema
addition. Each deliverable below is one independently verifiable outcome.

## Decision Record
`kind: framing`

The Orchestrator confirmed these decisions on 2026-10-24 through Triage's live
coordination route, including the Stage 2/Stage 3 ordering clarification:

- Port all job/run services and the hub-only scheduler, including all four action
  kinds. Bind native business services from their owning families; do not port
  dream, agent, pipeline, feedback or project-purge business logic a second time.
- Invoke #24706's `gobby_codeindex::jobs::nightly_repair::run` directly. Depend on
  that leaf, never on the #21586 epic: its #24713 cleanup already waits on #21582.
- #24702 alone owns hourly code-index pruning: code_index_prune in
  `gobby_codeindex::jobs::HubJobs::ticks()` calls
  `gobby_code::node::hub::job_prune::prune_all`. Cron adds no second prune
  binding or tick. #24702 is a transitive prerequisite of #24706.
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
- **Browser ownership ruling (2026-10-24, Orchestrator via Triage).** #24967's
  shared verifier-only implementation owns both existing
  gobby_session cookie classes. Cron's 4.1b registers only the ten /api/cron
  routes for Caller::Browser and preserves cookie, origin and CSRF behavior
  route by route. It carries `security-review` and depends on the shared
  verifier; no Cron verifier, lease-admin browser access or other-family
  admission is added. The 4.1 switch covers key callers only; Cron cookie
  traffic remains proxied until 4.1b passes, before 4.2 deletion.
- Native construction through P3 is passive in production. Only 4.1 activates
  native scheduled/manual admission, reconciliation and system registration,
  after the restart boundary proves Python admission has stopped. Proxy/compare
  retain Python ownership. Isolated tests explicitly exercise native ownership.
- **Debt disposition (2026-10-24, Orchestrator via Triage).** Adopt D1-D7 from
  the committed #24961 report, retaining K1's concurrency, ownership and durable
  child contracts. Correct accidental behavior through the shared #24963
  behavior-waiver manifest; no whole Cron surface is dropped. Native readiness
  must assert each approved replacement, rather than mask or skip a difference.
  #24964 owns the shared native CORS/preflight boundary independently of #24659.
- D7 preserves complete raw shell output in shared files_home storage from
  #24968; 1,048,576 bytes bounds only the inline decoded copy, split equally
  between prefix/tail. The amended ruling retains Josh's full-output preference
  (memory 6b880faa). Existing Cron run deletion/retention in 2.2 owns overflow
  cleanup; #24702 remains exclusively the code-index prune owner. Add one native
  output read plus MCP/CLI handoffs, with 64 KiB default/1 MiB maximum reads.
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

## Pre-expansion capability bindings
`kind: framing`

The Orchestrator's CRON-004/005 ruling establishes the following implementation
prerequisites. These are capability bindings, not dependencies on approval of
another plan or on a family's whole retirement root:

- **Shared browser-cookie verifier implementation, #24967.** Bind 3.1, 4.1
  and 4.1b to that implementation-only
  outcome, including both gobby_session classes and verified class/selectors.
  Exclude #24659's root and cookie retirement: that retirement waits for
  #24713, which already waits for Cron #21582. Plan approval is not landed
  service evidence.
- **Shared native CORS/preflight boundary, #24964 under #21542.** Bind 4.1b to this separate shared
  front-door implementation outcome. Every family consumes one configured
  credentialed CORS boundary on success and auth errors, with applicable
  unauthenticated preflight handled before authentication. This outcome is
  separate from the cookie verifier; do not fold it into #24659 or implement
  CORS independently in Cron.
- **Shared files_home safe I/O, #24968.** Bind 2.2/2.3 and the output-read
  outcome to this implementation-only capability outside both family roots.
  Reuse held-root descriptor open, private 0600 sibling temp raw-stream writing,
  file/directory fsync, durable replace, descriptor range reads and expected-
  object conditional unlink. Runtime injects this shared capability; gcron
  gains no dependency on gprojects, Projects' root or its retirement.

The headings bind #24967 and #24964 directly. Before Cron expansion, the
Orchestrator checks the full cross-plan graph for cycles and carries each
capability's scope through its owning approval process. Base/expansion validation
and fresh adversary-derived M1 cover these concrete edges. Dispatch/activation
requires landed capability proof, including exact-SHA Security approval of the
shared verifier; a planning-task closure never supplies it. Neither edge waits
on #24659 or its retirement branch. #24963 is the shared waiver/readiness
prerequisite, and #24968 is the shared byte-storage prerequisite. These are
concrete implementation task references, not unbound future owners.

## Debt corrections and shared conventions
`kind: framing`

Evidence authority is `.gobby/plans/research/cron-family-debt.md` at
0df80561ea3953af4db3afb386d730960407ba08. Its D1-D7 dispositions are approved
behavior changes, not whole-surface removals. #24963 owns the single manifest
`docs/contracts/parity-waivers.json`, its schema/loader and all shared consumers.
1.1 adds Cron entries with required fields id, surface (the exact ledger key),
behaviour, decision and replacement. IDs use lowercase kebab components in
`<family>.<surface-slug>.<behaviour-slug>` and stay stable once cited:

- D1: `cron.patch-job.schedule-validation` for HTTP PATCH /api/cron/jobs/{job_id};
  decision `.gobby/plans/gdaemon-cron-family.md#3.2`.
- D2: `cron.patch-job.presence-and-merge`, `cron.mcp-update-job.presence-and-merge`
  and `cron.cli-edit.presence-and-merge` for the corresponding PATCH, MCP
  gobby-cron:update_cron_job and CLI gobby cron edit surfaces;
  decision `.gobby/plans/gdaemon-cron-family.md#1.3`.
- D3: `cron.list-jobs.pagination`, `cron.mcp-list-jobs.pagination` and
  `cron.cli-list.pagination` for HTTP GET /api/cron/jobs, MCP
  gobby-cron:list_cron_jobs and CLI gobby cron list;
  decision `.gobby/plans/gdaemon-cron-family.md#3.1`.
- D4: `cron.toggle-job.future-schedule`, `cron.mcp-toggle-job.future-schedule`
  and `cron.cli-toggle.future-schedule` for POST /api/cron/jobs/{job_id}/toggle,
  MCP gobby-cron:toggle_cron_job and CLI gobby cron toggle;
  decision `.gobby/plans/gdaemon-cron-family.md#1.3`.
- D5: `cron.patch-job.atomic-schedule-edit`,
  `cron.mcp-update-job.atomic-schedule-edit` and
  `cron.cli-edit.atomic-schedule-edit` for the three update surfaces above;
  decision `.gobby/plans/gdaemon-cron-family.md#1.3`.
- D6: `cron.run-job.timeout-reap`, `cron.mcp-run-job.timeout-reap` and
  `cron.cli-run.timeout-reap` for HTTP POST /api/cron/jobs/{job_id}/run,
  MCP gobby-cron:run_cron_job and CLI gobby cron run; these cover their shared
  scheduled/manual shell lifecycle contract without accepting leaked processes;
  decision `.gobby/plans/gdaemon-cron-family.md#2.3`.
- D7: `cron.get-run.output-budget`, `cron.list-runs.output-budget`,
  `cron.mcp-list-runs.output-budget` and `cron.cli-runs.output-budget` for HTTP
  GET /api/cron/runs/{run_id}, GET /api/cron/jobs/{job_id}/runs, MCP
  gobby-cron:list_cron_runs and CLI gobby cron runs;
  decision `.gobby/plans/gdaemon-cron-family.md#2.3`.
- D7 additive retrieval: `cron.get-run-output.native-extension`,
  `cron.mcp-get-run-output.native-extension` and `cron.cli-output.native-extension`
  for HTTP GET /api/cron/runs/{run_id}/output, MCP
  gobby-cron:get_cron_run_output and CLI gobby cron output. Their native-only
  extension cases retain explicit provenance and never fabricate Python parity;
  decision `.gobby/plans/gdaemon-cron-family.md#3.1a`.

Each listed ID is one exact-surface entry, not a wildcard or Cron-owned format.
Its decision cites this plan's owning section under #24952. The replacement
binds the approved plan section/decision, exact operation/input, recorded Python
response/effect, required native response/effect and reason. Keep recorded Python
origin. Ledger generation, corpus/replay and compare consume the same #24963
entry and report its citation as Waived for changed behavior, or native-extension
for the additive surfaces; an additive surface without its entry fails. Unlisted
differences fail. Neither a
normalization mask nor Skipped is a waiver. Sequential cases outside the named
correction remain equal. Native race/cleanup assertions supplement the Python
oracle where reproducing its bug would be unsafe or nondeterministic.

Use one **native domain-error convention** across family adapters: validation,
missing, forbidden, conflict, capacity, unavailable and internal are distinct
typed classes. Preserve intentional existing wire envelopes with explicit
transport adapters; Cron's corrected schedule validation uses 400 with
`{"detail":"Invalid cron schedule"}`, forbidden remains 403, and unexpected
internal errors reveal no database diagnostics. Changed envelopes require an
exact #24963 waiver. Reuse shared runtime error facilities where sufficient;
this plan creates no Cron exception framework or unrelated envelope migration.

Use one **presence-preserving partial-update convention** across HTTP, MCP and
CLI service adapters: Absent leaves a field unchanged, Clear is explicit null,
and Set(value) supplies a value. Clear is accepted for description, display_name,
and schedule fields inactive under the merged schedule_type. Reject null for
name, schedule_type, timezone, action_type, action_config and enabled, and for
the active schedule's required field. Preserve display_name's empty-string
reset. Merge under the row lock, validate that candidate, then write once;
repeating the existing schedule_type does not require resending stored fields.
Empty updates retain the existing no-fields rejection.

Use #24963's **shared parity-readiness convention**: at least one ordinary
Passed case, explicit expected-native assertions for every cited Waived case,
and stable coverage of every required case. A report containing only Skipped
fails; an unwaived Skipped case cannot establish readiness. Transient race
diagnostics remain possible, but each required case must receive stable proof.
Use #24964's **shared outer CORS/preflight convention**: configured headers on
success and auth errors and applicable unauthenticated preflight before auth,
without family-local CORS code. Conflict with another family's error,
presence, readiness or CORS convention goes through Triage to the Orchestrator
before review; a family does not silently fork the shared contract.

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
  Consumers of #24883's descriptor contract run
  `cargo test -p gobby-core --test mcp_contract`; after nextest is restored,
  `cargo nextest run -p gobby-core --test mcp_contract` selects the whole
  integration test target, including the two tests outside its named module.
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
The ten HTTP rows are implemented here and change from `proxied` to `delegated`
only after their native parity checks pass. The ten CLI and eight MCP rows stay
`proxied`, with #21571/#21570 owner notes, until those owners port them. The
service contract in 3.4 carries their behavior, serialization, errors and
restrictions. Ledger statuses are exactly `proxied`, `delegated` (ported), or
`dropped by decision #N`; assignment to a future owner does not mark a row done.
All approved D1-D7 deviations carry the applicable #24963 behavior ID in ledger
owner notes and the service/corpus/compare evidence. They do not change the
28-row inventory or create a whole-surface dropped status.
The amended D7 also adds HTTP GET /api/cron/runs/{run_id}/output here, MCP
gobby-cron:get_cron_run_output in #21570, and CLI gobby cron output in #21571.
These are three additive native surfaces, not replacements for original rows.
4.3 records the delivered new HTTP row as delegated (29 Cron-owned/handoff rows
at that point); each front-door owner adds its new delegated row only when its
adapter is delivered. The final cross-owner inventory is 31 rows. Never claim
an unimplemented new adapter is proxied through a nonexistent Python handler.

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
| HTTP GET /api/cron/runs/{run_id}/output | additive D7; 3.1a.1-3.1a.3 |
| MCP gobby-cron:get_cron_run_output | additive D7; #21570; 3.4 service handoff |
| CLI gobby cron output | additive D7; #21571; 3.4 service handoff |

## P1: Portable schedule and storage contracts
`kind: framing`

### 1.1 Characterize Cron behavior before takeover [category: test] (depends: #24963)
`kind: deliverable`

Targets:
- `tests/contracts/cron/schedules.json`
- `tests/contracts/cron/services.json`
- `tests/contracts/cron/lifecycle.json`
- `tests/contracts/http/cron.json`
- `tests/contracts/http/cron-output.json`
- `tests/contracts/http/cron-browser.json`
- `tests/contracts/http/manifest.json::*` — scope-reason: register the Cron corpus with Python origin
- `tests/contracts/test_cron_oracle.py`
- `docs/contracts/parity-waivers.json`

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
The #24961 report at 0df80561ea3953af4db3afb386d730960407ba08 establishes
D1-D7's approved corrections. Record original Python evidence unchanged and bind
native expectations through the real per-surface IDs in Debt corrections and
shared conventions. This leaf adds every listed Cron entry to
`docs/contracts/parity-waivers.json` after #24963 supplies its format/loader.
Each entry records exact ledger surface, one-sentence behaviour, decision
the exact decision path/section enumerated above and the required native
replacement; fixtures retain original Python observations separately. Include invalid expression/timestamp/no-future
PATCH, nullable clears and stored-field merges, more than 50 jobs with retired
rows and equal timestamps, expired ordinary toggle versus protected park/wake,
concurrent schedule edits, TERM-ignoring children and oversized/invalid-UTF-8
output. K1's admission rollback, owner scoping and child-link-before-dispatched
remain unwaived. Native race and cleanup assertions supplement the oracle;
do not reproduce a Python leak or nondeterministic race as the native golden.

This leaf's exact manifest inventory (each surface below uses its full ledger
key, including the HTTP/CLI/MCP prefix) is:
- D1: `cron.patch-job.schedule-validation`, HTTP PATCH /api/cron/jobs/{job_id},
  decision `.gobby/plans/gdaemon-cron-family.md#3.2`; schedule errors become
  safe 400 `{"detail":"Invalid cron schedule"}` with no write.
- D2: `cron.patch-job.presence-and-merge`,
  `cron.mcp-update-job.presence-and-merge`, `cron.cli-edit.presence-and-merge`,
  respectively HTTP PATCH /api/cron/jobs/{job_id}, MCP
  gobby-cron:update_cron_job, CLI gobby cron edit; decision
  `.gobby/plans/gdaemon-cron-family.md#1.3`. Absent/Clear/Set retains omissions,
  clears nullable description/display_name/inactive schedule fields only, and
  validates merged stored fields; required nulls reject, empty display name resets.
- D3: `cron.list-jobs.pagination`, `cron.mcp-list-jobs.pagination`,
  `cron.cli-list.pagination`, respectively HTTP GET /api/cron/jobs, MCP
  gobby-cron:list_cron_jobs, CLI gobby cron list; decision
  `.gobby/plans/gdaemon-cron-family.md#3.1`. HTTP/MCP default 50/max 200 keyset
  pages sort created_at DESC,id DESC, filter retired rows before limit and
  return page count/nullable next_cursor. CLI walks 50-row pages for a complete
  listing; ordinary render shapes stay intact.
- D4: `cron.toggle-job.future-schedule`,
  `cron.mcp-toggle-job.future-schedule`, `cron.cli-toggle.future-schedule`,
  respectively HTTP POST /api/cron/jobs/{job_id}/toggle, MCP
  gobby-cron:toggle_cron_job, CLI gobby cron toggle; decision
  `.gobby/plans/gdaemon-cron-family.md#1.3`. Ordinary invalid/expired enable
  fails unchanged (HTTP safe 400); protected park/wake remains unwaived.
- D5: `cron.patch-job.atomic-schedule-edit`,
  `cron.mcp-update-job.atomic-schedule-edit`, `cron.cli-edit.atomic-schedule-edit`,
  respectively the D2 surfaces; decision
  `.gobby/plans/gdaemon-cron-family.md#1.3`. One row lock covers merge,
  validation and next-run write; deterministic edit/admission tests preserve CAS.
- D6: `cron.run-job.timeout-reap`, `cron.mcp-run-job.timeout-reap`,
  `cron.cli-run.timeout-reap`, respectively HTTP POST /api/cron/jobs/{job_id}/run,
  MCP gobby-cron:run_cron_job, CLI gobby cron run; decision
  `.gobby/plans/gdaemon-cron-family.md#2.3`. Timeout/cancellation force-kill
  and await disposable TERM-ignoring children; timeout failure envelopes stay.
- D7: `cron.get-run.output-budget`, `cron.list-runs.output-budget`,
  `cron.mcp-list-runs.output-budget`, `cron.cli-runs.output-budget`,
  respectively HTTP GET /api/cron/runs/{run_id}, HTTP GET
  /api/cron/jobs/{job_id}/runs, MCP gobby-cron:list_cron_runs, CLI gobby cron runs;
  decision `.gobby/plans/gdaemon-cron-family.md#2.3`. Only the inline decoded
  copy is capped at 1,048,576 UTF-8 bytes (first/last 524,288); complete raw bytes
  persist through #24968, with total raw bytes/truncation/output_url and
  run-deletion-owned cleanup. Old unknown metadata stays absent.
- D7 extensions: `cron.get-run-output.native-extension`,
  `cron.mcp-get-run-output.native-extension`, `cron.cli-output.native-extension`,
  respectively HTTP GET /api/cron/runs/{run_id}/output, MCP
  gobby-cron:get_cron_run_output, CLI gobby cron output; decision
  `.gobby/plans/gdaemon-cron-family.md#3.1a`. Reads default 65,536/max 1,048,576
  bytes, raw HTTP/CLI and MCP base64, with existing run/project auth. These
  report native-extension with citation and native provenance, never Waived
  Python success. Missing registry entries fail the shared gates.

Record ordinary and browser-testing gobby_session cookies separately across all
nine existing /api/cron routes, including mutation rows/effects, selector mismatches,
expiry/revocation, absent/invalid Authorization and missing credentials. Pin
the current credentialed CORS/preflight responses for allowed and disallowed
origins and the absence of an extra Cron CSRF-token/Origin rejection. The
middleware/source citations and shared-verifier ownership are repeated in 4.1b.
The new tenth Cron/browser route, GET /api/cron/runs/{run_id}/output, is the
approved D7 native extension. Its separate cron-output.json fixture records
native provenance, exact raw range/limit/auth errors and both cookie classes'
read authority; it is not a Python success oracle. Include full raw bytes after
the inline cap and invalid-UTF8 cases, storage failures, project substitution,
run deletion and overflow cleanup. Its new MCP/CLI mappings carry their exact
native-extension IDs to the front-door owners.
Observed: source inspection only; these new tests have not run.
Planned: isolated pytest of test_cron_oracle and HTTP corpus recording/replay.

**Granularity:** These fixtures characterize one cross-language parity
interface, with no implementation or domain ownership transferred by this leaf.

**Acceptance:**
- 1.1.1 - Schedule vectors reproduce Python results at fixed instants, including field order and DST. test: `tests/contracts/test_cron_oracle.py::test_schedule_vectors_match_python`.
- 1.1.2 - Service vectors retain original Python CRUD, parking, filtering, protection and serialization evidence and bind D1-D5/D7 native replacements to exact #24963 behavior IDs. test: `tests/contracts/test_cron_oracle.py::test_service_vectors_match_python`.
- 1.1.3 - Lifecycle vectors reproduce admission conflicts, durable children, backoff and recovery; D5-D7 native race/cleanup/resource assertions cite exact waivers. test: `tests/contracts/test_cron_oracle.py::test_lifecycle_vectors_match_python`.
- 1.1.4 - The original ten HTTP routes/error branches retain Python origin; the additive raw-output read has explicit approved native-extension provenance, bounded range/auth/failure/retention cases and no fabricated Python equality. file: `tests/contracts/http/cron.json`; file: `tests/contracts/http/cron-output.json`.
- 1.1.5 - Browser vectors record both cookie classes for the nine Python Cron routes plus explicit native-extension authority for the tenth output read; selectors, credentials, effects and CORS/CSRF policy are not normalized. test: `tests/contracts/test_cron_oracle.py::test_browser_vectors_match_python`.
- 1.1.6 - Every D1-D7 Cron waiver has its real stable per-surface ID, exact ledger surface, behaviour, approved decision and replacement in the one shared manifest; the oracle binds both Python observations and native assertions to it. file: `docs/contracts/parity-waivers.json`.

### 1.1a Durable bounded-output metadata schema [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/478_cron_output_capture.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: register the migration and refresh the embedded schema identity
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the cron_runs column catalog
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: refresh latest-version/checksum/root identity literals and golden grant bindings
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: assert the three nullable output metadata columns and refreshed identity
- `crates/gcore/tests/cron_output_capture.rs`
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: refresh and verify the derived embedded-schema identity contract
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerate latest-version/checksum/assets-root identity
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: re-sign the updated schema identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: re-sign the updated schema identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: re-sign the updated schema identity
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: re-sign the updated schema identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: re-sign the updated schema identity
- `crates/gdaemon/Cargo.toml`
- `crates/gcode/Cargo.toml`
- `crates/ghook/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pins for the shared gcore schema change

**Research context:** The Orchestrator approved D7's fixed output budget and
amended its storage design to typed nullable columns, not JSONB, plus the
overflow reference needed to preserve full raw output. Source
evidence `crates/gcore/assets/schema/baseline.sql:2384-2400`, excerpt_hash
`4c7dee8671d3b18caef2b66518a0b7f82c738002b8ac6b913038923f993e8f55`,
shows `2392|     output text,` and `2393|     error text,` with no capture
metadata. Add output_bytes_seen BIGINT, output_truncated BOOLEAN and
output_overflow_ref TEXT to cron_runs through one additive migration. Leave old
rows NULL/NULL/NULL; add no backfill, table, storage service or config knob.
The runtime role's existing table grants cover these columns. A new shell
completion writes all three together. The reference is a files_home-relative
`cron/<run-UUID>/output.raw` locator, never a caller-provided or absolute path.
Historical/non-shell rows keep NULL when capture facts are unknown.

478 is the next free migration at plan time. At the executor's starting tip,
take the next free number and use it consistently in the filename, migration
registry and every derived carrier. Preserve the shipped baseline and prior
migration receipts. Reuse the schema/identity regeneration and golden-grant
signing path documented by the runtime-family migration; update embedded
assets, catalog, grant identity, Python identity and all five signed fixtures
together. Sweep literal latest-version/checksum consumers before committing.
The gcore dependency change bumps gcode, gdaemon and ghook by one patch with
lock/pins; rebuild other affected dependents, including gclient, at the release
owner's coordinated cutover. No live database migration occurs in planning.
Planned: isolated `cargo test -p gobby-core --features postgres --test
cron_output_capture --test schema_contract --test catalog_manifest_freshness`,
`cargo test -p gobby-daemon --test cli_contract`, release-pin pytest and
`tests/runtime_grants/test_golden_vectors.py` on the isolated hub.

**Granularity:** More than six production targets are the one additive schema
change's mandatory identity, signed-fixture and release carriers. They must
commit together; model/executor changes remain separately owned below.

**Acceptance:**
- 1.1a.1 - Fresh and upgraded schemas expose nullable BIGINT output_bytes_seen, BOOLEAN output_truncated and TEXT output_overflow_ref; old rows retain NULL/NULL/NULL and existing output is unchanged. test: `crates/gcore/tests/cron_output_capture.rs::output_capture_migration_preserves_existing_runs`.
- 1.1a.2 - Embedded schema, catalog, Python expectation, CLI identity and signed grants all identify the same migration bytes. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
- 1.1a.3 - gcode, gdaemon and ghook receive this leaf's patch bumps, lock and pins. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 1.2 Native schedule and value model [category: code] (depends: 1.1a, #21559)
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
wire formatting. Carry 1.1a's nullable output_bytes_seen, output_truncated and
output_overflow_ref as typed optional fields. Omit unknown metadata from
old/non-shell full and brief JSON. For newly captured shell output emit byte
count/truncation, including zero/false, plus output_url pointing to
/api/cron/runs/{run_id}/output; retain the storage locator internally. The output
string stays unchanged below the inline budget. These new
shell metadata fields and bounded-output cases use the exact D7
`cron.get-run.output-budget`, `cron.list-runs.output-budget`,
`cron.mcp-list-runs.output-budget` and `cron.cli-runs.output-budget` entries
from #24963, never a broad JSON mask. This leaf adds no ticking task or migration.
Planned: `CARGO_BUILD_JOBS=3 cargo test -p gobby-cron --test schedule`, plus pins.

**Granularity:** More than six production targets are the package setup and its
required release carriers. One pure schedule/value contract remains the outcome.

**Acceptance:**
- 1.2.1 - Every schedule vector gives the Python next instant or no next instant, and accepts/rejects the same grammar. test: `crates/gcron/tests/schedule.rs::schedule_vectors_match_python`.
- 1.2.2 - Job/run/child full and brief JSON preserve aware UTC/null values; shell metadata follows the four exact D7 surface waivers while old/non-shell payloads remain unchanged. test: `crates/gcron/tests/schedule.rs::models_match_python_wire`.
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
The approved #24961 D2/D4/D5 corrections use the shared #24963 entries
`cron.patch-job.presence-and-merge`, `cron.patch-job.atomic-schedule-edit` and
`cron.toggle-job.future-schedule`, plus their MCP/CLI counterparts recorded by
1.1. Use Absent/Clear/Set consistently: only description, display_name and
inactive schedule fields accept Clear; required name/type/timezone/action/
enabled fields and the active schedule's required value reject null.
Keep the empty display-name reset and no-fields rejection. Lock the job row,
check protection, merge existing fields with the patch, validate the candidate,
compute next_run_at and commit all fields in that same transaction. Repeating
the existing schedule_type retains its stored schedule values. An invalid
expression/timestamp or enabled candidate with no future occurrence is a typed
validation error with no write. No second scheduler/global admission lock is
introduced for edits. Ordinary toggle-on similarly refuses expired/invalid
schedules without changing the row; protected park/wake remains an intentional
enabled=true,next_run_at=NULL contract and is not prohibited by this guard.
The list query applies retired-row/project/enabled filtering before LIMIT,
orders created_at DESC,id DESC and supports 50-default/1..200 keyset pages
under `cron.list-jobs.pagination`; its HTTP contract belongs to 3.1.
Read existing project deletion callers before preserving their batch contract;
native bindings are owned by 2.7. No new generic repository layer is needed.
Planned: isolated `cargo test -p gobby-cron --test jobs` and release-pin pytest.

**Acceptance:**
- 1.3.1 - CRUD, SQL-before-limit retired filtering, deterministic keyset pages, prefix cleanup, normalization and duplicate names match service vectors plus the exact D3 waiver. test: `crates/gcron/tests/jobs.rs::job_mutations_match_python`.
- 1.3.2 - System protection, bookkeeping, park/wake and idempotent drift reconciliation preserve parked state across restart. test: `crates/gcron/tests/jobs.rs::system_rows_remain_protected_and_parked`.
- 1.3.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 1.3.4 - Concurrent interval/timezone edits serialize merge, protection, validation, next-instant calculation and write under one job-row lock; each committed combination is self-consistent. test: `crates/gcron/tests/jobs.rs::concurrent_schedule_edits_validate_committed_candidate`.
- 1.3.5 - Absent/null/value updates follow field nullability and validate merged stored fields; repeated schedule_type succeeds without resending its unchanged value, and invalid candidates write nothing. test: `crates/gcron/tests/jobs.rs::patch_presence_and_merged_validation_are_atomic`.
- 1.3.6 - Toggle-on refuses expired/invalid ordinary schedules unchanged while protected park/wake retains its separate enabled-null-next contract. test: `crates/gcron/tests/jobs.rs::toggle_requires_future_schedule_without_breaking_park`.

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
K1's CAS/insertion rollback boundary remains unwaived. For D5, a concurrent
Jobs schedule edit holds only its job-row lock; admission takes its existing
advisory lock before the due-row CAS. An edit never acquires that global lock
after taking the row lock. A changed due value makes the scheduled CAS fail,
not admit the old occurrence. Test edit-versus-admission with deterministic
barriers and validate the committed schedule/run pair under
`cron.patch-job.atomic-schedule-edit` and its adapter-specific entries.
Planned: `cargo test -p gobby-cron --test admission` against isolated PostgreSQL,
including competing native/native and native/Python transactions.

**Acceptance:**
- 2.1.1 - Two contenders claim one scheduled occurrence, and insertion failure leaves the original due timestamp. test: `crates/gcron/tests/admission.rs::scheduled_claim_is_atomic_and_interoperable`.
- 2.1.2 - Manual runs respect machine capacity and per-job uniqueness without moving schedules; all three rejection cases remain distinct. test: `crates/gcron/tests/admission.rs::manual_admission_matches_python`.
- 2.1.3 - Run read/list/child hydration match lifecycle vectors without mutating foreign-machine rows. test: `crates/gcron/tests/admission.rs::run_queries_preserve_owner_and_children`.
- 2.1.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 2.1.5 - A competing schedule edit and due admission obey the row/CAS ordering without deadlock, a stale occurrence or an inconsistent committed next instant. test: `crates/gcron/tests/admission.rs::schedule_edit_and_admission_share_consistent_row_order`.

### 2.2 Scheduler tick, reconciliation and shutdown [category: code] (depends: 2.1, #24968)
`kind: deliverable`

Targets:
- `crates/gcron/src/scheduler.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/recovery.rs`
- `crates/gcron/src/output_files.rs`
- `crates/gcron/src/runs.rs` — edit the file created by 2.1 to couple all run deletion to owned-output cleanup
- `crates/gcron/src/jobs.rs` — edit the file created by 1.3 to retain output cleanup on job deletion/cascade
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
Full raw output uses #24968's shared held-files_home descriptors, injected by
the runtime. Its Python compatibility mechanism is
`src/gobby/paths.py::open_files_home_descendant`, lines 173-198, excerpt_hash
`507d158e62195fcbee805760a6e82c2c408aef3e13d37d77b79b5d0715616955`,
including `180|     """Open a descendant file through the held files_home fd (no-follow)."""`
and `191|         return os.open(parts[-1], open_flags, mode, dir_fd=directory_fd)`.
Consume the native #24968 port for private 0600 temp creation, stream writing,
fsync/replace/directory-fsync, held-descriptor range reads and expected-object
conditional unlink. New output_files functions bind this capability only to
`cron/<run-UUID>/output.raw` and sibling private partial files; they are not a
second files_home implementation, attachment lifecycle or generic artifact store.

D7 overflow follows Cron's existing cleanup_old_runs eligibility and six-hour
cleanup cadence using cleanup_after_days: only non-pending/non-running rows
older than the cutoff are pruned. No new TTL and no #24702 dependency. All run
deletion paths, including job/project cascade, collect owned output locators
before row deletion, commit deletion, then conditionally unlink the expected
objects. Unlink failure remains recoverable: the same startup/cleanup lifecycle
checks deterministic run-UUID directories and retries orphan cleanup when the
row no longer exists. A still-existing run, including an active writer with an
unpublished reference, prevents orphan deletion. Never unlink another object's
replacement or rely on a removed DB reference to rediscover its path. Bound
each namespace scan to 500 entries with continuation in the existing lease-
owned cleanup loop; this is deletion recovery, not an independent TTL/queue.
At crash recovery preserve any partial raw bytes for the failed owned run,
finish safe publication where provable, and record capture failure explicitly
otherwise. Stop writers before lease transfer. Test DB commit/unlink failures,
process loss between commit and unlink, and active/foreign locator protection.
Planned: `cargo test -p gobby-cron --test scheduler`, with frozen time,
two-machine fixtures, injected transaction/callback failures and crash restart.

**Granularity:** One hub scheduler owns ticking, recovery and run retention;
raw-output deletion is part of that same lifecycle. Shared storage implementation
is #24968. Additional targets attach deletion paths and release carriers.

**Acceptance:**
- 2.2.1 - Tick/backoff/retention and once-job disabling match lifecycle vectors. test: `crates/gcron/tests/scheduler.rs::ticks_and_bookkeeping_match_python`.
- 2.2.2 - Restart, stale and orphan handling affect only the intended machine/owner and active child links normalize correctly. test: `crates/gcron/tests/scheduler.rs::recovery_is_machine_and_owner_scoped`.
- 2.2.3 - Lease loss and shutdown stop admission, reap cron parent sessions and terminalize owned inline work without cancelling durable children. test: `crates/gcron/tests/scheduler.rs::shutdown_drains_and_releases_owned_work`.
- 2.2.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 2.2.5 - Pruning and every explicit/cascade run deletion remove owned raw output with the same cutoff/lifecycle; no code-index tick or new TTL is introduced. test: `crates/gcron/tests/scheduler.rs::run_deletion_owns_raw_output_retention`.
- 2.2.6 - Startup/cleanup recover committed-delete/unlink failures idempotently while active runs and substituted/foreign objects retain their bytes. test: `crates/gcron/tests/scheduler.rs::raw_output_cleanup_recovers_without_foreign_unlink`.

### 2.3 Bounded shell and handler action execution [category: code] (depends: 2.2, #24968)
`kind: deliverable`

Targets:
- `crates/gcron/src/executor.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/shell.rs`
- `crates/gcron/src/output_files.rs` — edit the file created by 2.2 to stream raw shell bytes through the shared capability
- `crates/gcron/src/runs.rs` — edit the file created by 2.1 to persist inline output and all capture metadata in one completion update
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
default. Spawn argv directly, never through a shell parser. Preserve one combined
stdout/stderr stream. D7 deliberately corrects unbounded communicate(): spool
every raw byte from the first read through #24968's private temp descriptor,
using a fixed-size buffer and disk backpressure instead of accumulating it.
Retain this complete raw file even below the inline cap, so invalid UTF-8 is
exactly retrievable. Publish by file-fsync/replace/directory-fsync before
persisting its reference; a capture/publication failure kills/reaps the owned
child and fails explicitly without claiming complete output. Keep provable
partial bytes for recovery. Never reopen a pathname or expose the private locator
as a caller-controlled read path. In parallel count all raw bytes observed,
decode invalid UTF-8 with replacement incrementally and preserve incomplete codepoints
across input chunks. Retain at most 1,048,576 encoded UTF-8 bytes of decoded
text: at overflow, concatenate its first and last at most 524,288 bytes, trimming
at codepoint boundaries without adding a marker. Count raw input bytes before
replacement; the budget applies to decoded UTF-8 bytes, so replacement expansion
also cannot exceed it. Output at/below the decoded budget stays exact and
output_truncated=false. Above it sets true; even after the limit, continue
draining to avoid a blocked child. No unbounded intermediate buffer, output
storage service or configurable cap is introduced. Persist output_bytes_seen,
output_truncated and output_overflow_ref with output/error in one completion
write to 1.1a's columns. New shell runs retain all three facts, including a
published empty raw file for zero bytes/false; historical/non-shell unknowns
stay NULL. The cap affects only the inline copy and never discards raw bytes.
Run payloads expose the derived output_url; 3.1a serves byte ranges without
loading the whole file. Bound the error tail at 2,000 characters.
The four D7 read/history surface waivers from 1.1 cover changed output and new
metadata; ordinary output text remains exact.

D6 deliberately strengthens Python's timeout terminate-without-wait behavior.
Timeout/cancellation kills and awaits the owned process, escalating to a force
kill if TERM is ignored, then drains the bounded pipe before terminalization.
Use the existing runtime process/cancellation facility; do not leave an owned
child alive when a terminal run is reported. Cite `cron.run-job.timeout-reap`,
`cron.mcp-run-job.timeout-reap` and `cron.cli-run.timeout-reap`; preserve the
existing timeout failure envelope. Test only disposable children, including
one ignoring TERM. Normalize mapping/string results into completed,
failed or skipped and persist output/error before reporting completion.
Unknown actions/handlers and missing required config fail with typed errors.
Planned: `cargo test -p gobby-cron --test executor` with temporary fake commands.

**Granularity:** One inline executor owns timeout/cancellation, bounded inline
capture and durable raw publication before terminal completion. The shared
files_home capability and scheduler-owned retention stay external to that
outcome. More than six targets connect its existing run model and release.

**Acceptance:**
- 2.3.1 - Handler results, timeout selection, unknown actions and errors match lifecycle vectors. test: `crates/gcron/tests/executor.rs::inline_results_match_python`.
- 2.3.2 - Shell argv/cwd/ordinary output/failure tails match vectors; D6 force-kills and awaits a TERM-ignoring disposable child on timeout and cancellation before reporting a terminal run. test: `crates/gcron/tests/executor.rs::shell_actions_are_bounded_and_reaped`.
- 2.3.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.
- 2.3.4 - D7 retains the complete raw stream in #24968 storage while bounding the decoded inline copy to 1,048,576 UTF-8 bytes; count/truncation/reference commit together, exact/multibyte/invalid-byte boundaries reload correctly, and storage failure never reports successful full capture. test: `crates/gcron/tests/executor.rs::shell_output_budget_is_durable_and_utf8_safe`.

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
Python repair timeout. #24702, upstream of #24706, owns hourly pruning as
code_index_prune in `gobby_codeindex::jobs::HubJobs::ticks()`, calling
`gobby_code::node::hub::job_prune::prune_all(..., PruneEntry::Skip)`.
It also removes the old Python hourly Cron registration. Cron adds no second
prune binding or tick. Planned: `cargo test -p gobby-daemon --test
cron_code_index`; assert one nightly scheduler owner and no duplicate prune.

**Acceptance:**
- 2.6.1 - New/drifted/disabled/parked nightly definitions reconcile correctly and preserve local-zone scheduling. test: `crates/gdaemon/tests/cron_code_index.rs::nightly_definition_preserves_operator_state`.
- 2.6.2 - A due run calls jobs::nightly_repair once, persists its summary, and neither invokes Python nor imposes the retired project timeout; #24702 remains the only hourly-prune owner, with no Cron prune binding or tick. test: `crates/gdaemon/tests/cron_code_index.rs::native_code_index_jobs_execute_once`.
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

### 3.1 Cron family and job/run read routes [category: code] (depends: 2.8, #24964, #24967)
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
principal admission. #24967's implementation-only shared browser-cookie
verifier supplies the single native verifier, independently of cookie retirement;
#21559's runtime family and #21575's mode gate do not supply cookie verification.
This leaf cannot dispatch without landed #24967 verifier proof.
The #24964 shared outer CORS/preflight boundary is also required from the first
native key-caller read; browser API-key callers need it before cookie activation.
Keep Cron cookie requests proxied until 4.1b registers the ten
Cron routes against it and proves parity. No browser claim includes either
admin Cron endpoint. The family defaults to Proxy until 4.1; compare mutations
call only Python. Registering
these native routes does not activate their production scheduler handle.
For approved D3, GET /api/cron/jobs accepts limit (default 50, range 1..200)
and optional cursor. Order created_at DESC,id DESC; apply project, enabled and
retired-automation SQL predicates before LIMIT. The cursor is unpadded base64url
of the last returned [UTC RFC3339 created_at, canonical UUID] JSON pair;
subsequent queries use a strict tuple-less-than bound. It grants no authority:
reapply project/caller filters on every page. Reject malformed cursors with
400 `{"detail":"Invalid cursor"}` and out-of-range limits with the normal
422 parameter-validation response. Fetch one extra row to determine continuation;
return next_cursor as that last returned pair only when another row exists,
otherwise null. count remains returned-page length. Equal timestamps use id
as the deterministic tie-breaker; inserts newer than the cursor do not shift
older pages. This is keyset traversal, not a frozen snapshot: later mutations
may change membership, but an unchanged matching row is neither duplicated nor
hidden by retired rows. Cite `cron.list-jobs.pagination` in #24963; no other
ordering or envelope difference is waived.
Run reads consume `cron.get-run.output-budget` and
`cron.list-runs.output-budget`: expose known D7 output_bytes_seen and
output_truncated in full/brief payloads, omit unknown historical/non-shell facts,
and keep ordinary output text exact. Use the shared native domain-error
convention with intentional existing envelopes; no route-local error framework.
Planned: `cargo test -p gobby-cron --test http_reads` and HTTP corpus replay.

**Granularity:** The four reads are one read-only family surface over the same
repository, with one acceptance per ledger row. Shared release targets account
for the inventory threshold.

**Acceptance:**
- 3.1.1 - GET /api/cron/jobs preserves display names and page count while the exact D3 waiver asserts default 50/max 200 cursor pages, SQL-before-limit retirement filtering, deterministic equal-timestamp order, null-at-end and invalid cursor/limit errors across more than 50 rows. test: `crates/gcron/tests/http_reads.rs::list_jobs_matches_python`.
- 3.1.2 - GET /api/cron/jobs/{job_id} matches existing, missing and malformed IDs. test: `crates/gcron/tests/http_reads.rs::get_job_matches_python`.
- 3.1.3 - GET /api/cron/jobs/{job_id}/runs preserves history bounds, ordering and child hydration and asserts D7's bounded output/known metadata through cron.list-runs.output-budget. test: `crates/gcron/tests/http_reads.rs::list_runs_matches_python`.
- 3.1.4 - GET /api/cron/runs/{run_id} preserves existing/missing row behavior and asserts D7's bounded output/known metadata through cron.get-run.output-budget. test: `crates/gcron/tests/http_reads.rs::get_run_matches_python`.
- 3.1.5 - Family claims, principals, node-to-hub routing and method/path boundaries enforce the existing authorization contract. test: `crates/gcron/tests/http_reads.rs::claims_and_auth_are_bounded`.
- 3.1.6 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.1a Bounded raw-output retrieval [category: code] (depends: 3.1, #24968)
`kind: deliverable`

Targets:
- `crates/gcron/src/http_output.rs`
- `crates/gcron/src/output_files.rs` — edit the file created by 2.2 to read the admitted run's raw bytes through shared held descriptors
- `crates/gcron/src/family.rs` — edit the file created by 3.1 to claim the additional output-read route
- `crates/gcron/src/lib.rs`
- `crates/gcron/tests/http_output.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for bounded raw-output retrieval

**Research context:** The amended D7 ruling preserves complete raw output and
approves GET /api/cron/runs/{run_id}/output, default 65,536 bytes and maximum
1,048,576 bytes per read. Consume #24968's held-root descendant and descriptor
range streaming capability; gcron owns only the admitted run/locator binding.
The existing storage mechanism is src/gobby/paths.py::open_files_home_descendant
at lines 173-198, excerpt_hash
`507d158e62195fcbee805760a6e82c2c408aef3e13d37d77b79b5d0715616955`;
the native implementation is external #24968, not a Python runtime dependency.
Use the stored output_overflow_ref only after run/job/project authorization.
Validate the expected run-UUID namespace and regular-file identity through the
shared capability. Never accept a filesystem path or locator from the request,
reopen a pathname, load the complete file, or give a node local storage access.

Without Range, offset defaults to 0 and limit to 65,536 (range 1..1,048,576);
return 200 raw application/octet-stream bytes, X-Output-Bytes-Seen and
X-Next-Offset headers, omitting the latter at EOF. Support one standard bytes
Range including closed/open/suffix forms, clamp its end at EOF, and return 206
with Content-Range and Accept-Ranges. A requested span beyond 1,048,576 bytes
is 400 rather than an unbounded read; a wholly out-of-bounds range is 416 with
Content-Range: bytes */total. Reject
multipart ranges, malformed ranges or mixed Range and explicit offset/limit
with 400. Invalid numeric query bounds use 422. An active unpublished capture is
409, missing run/output or historical/non-shell unknown raw bytes is 404, and
known capture/storage failure is 503; do not substitute the lossy inline string.
Use Cache-Control:no-store and nosniff, preserve raw invalid UTF-8, and close
the held descriptor on completion/cancellation. An unbounded HTTP download is
not a hidden second path around the read limit.

`cron.get-run-output.native-extension` records this additive native surface's
approved contract/provenance through #24963. Original get-run/list-runs JSON
stays a run envelope with inline output/metadata/output_url, never raw-response
overloading. The family stays passive/Proxy through P3; isolated native mode
tests the new route. Native default enables it in 4.1, and 4.1b admits its cookie
read through the same bounded project authority, making ten Cron browser
routes and eleven native HTTP routes including protected-runs.
Planned: `cargo test -p gobby-cron --test http_output` using temporary files_home
and isolated run rows, including invalid bytes, range boundaries and auth errors.

**Granularity:** One read service/route owns streaming, bounds and refusals;
the external storage implementation and existing run-retention owner are reused.
Release carriers account for the inventory threshold.

**Acceptance:**
- 3.1a.1 - Raw reads reconstruct the complete shell byte stream beyond the inline cap, including invalid UTF-8, while each response stays within the approved default/max bound. test: `crates/gcron/tests/http_output.rs::raw_output_is_complete_and_bounded`.
- 3.1a.2 - Single ranges, EOF, malformed/oversized/multipart requests, query bounds and active/missing/failed capture errors follow the specified statuses and headers. test: `crates/gcron/tests/http_output.rs::range_and_capture_states_are_explicit`.
- 3.1a.3 - Project/caller substitution, forged locators, symlink/ancestor swaps and cancellation never reveal another run's bytes or reopen an unsafe path; node reads use the hub relay. test: `crates/gcron/tests/http_output.rs::raw_output_authority_and_descriptor_are_bounded`.
- 3.1a.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.2 Job mutations and immediate execution routes [category: code] (depends: 3.1a)
`kind: deliverable`

Targets:
- `crates/gcron/src/http_writes.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/family.rs` — edit the file created by 3.1 to extend claims and routing to the five owned mutation routes
- `crates/gcron/tests/http_writes.rs`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for Cron HTTP writes

**Research context:** Port
`src/gobby/servers/routes/cron.py::CreateCronJobRequest`, UpdateCronJobRequest,
_validate_schedule_fields, create_job, update_job, delete_job, toggle_job and
run_job_now. Reuse native Jobs and Scheduler admission, never route-local SQL
or an alternate manual executor. Match project resolution, required schedule
fields, enum/type validation and protection status 403. Adopt the shared
Absent/Clear/Set convention, not Python's blanket None dropping. Explicit null
clears description/display_name and inactive schedule fields; it cannot clear
required fields or the active schedule's required value. Preserve the empty
display-name reset and existing no-fields rejection. Jobs merges/validates
under its row lock; a repeated schedule_type uses stored values. For PATCH, D1 schedule
validation (invalid expression/timestamp/no future occurrence) returns 400
`{"detail":"Invalid cron schedule"}` and leaves the row unchanged, rather
than PATCH's accidental Python 500. Required-null/type failures keep the normal
422 validation response. D4 toggle-on of an invalid/expired ordinary job uses
that same safe 400 and writes nothing; protected park/wake is unaffected.
Consume `cron.patch-job.schedule-validation`, `cron.patch-job.presence-and-merge`,
`cron.patch-job.atomic-schedule-edit` and `cron.toggle-job.future-schedule`
from #24963. Use the shared typed domain-error classification and intentional
wire-envelope adapters; unexpected internal errors expose no DB diagnostics.
Create retains its existing oracle error envelope; D1 does not waive a create
response difference.
Run-now
returns the admitted run and maps active conflict to 409, capacity to 429 and
missing scheduler to 503. Mutating compare requests execute Python only once;
3.5 adds observation of committed effects, never a second native action.
Planned: `cargo test -p gobby-cron --test http_writes` and shared corpus replay.

**Acceptance:**
- 3.2.1 - POST /api/cron/jobs matches create validation, project resolution, status/body and stored rows. test: `crates/gcron/tests/http_writes.rs::create_job_matches_python`.
- 3.2.2 - PATCH follows D1/D2/D5 exact waivers: safe 400 schedule rejection with no write, presence-preserving nullable clears and merged stored-field validation; no-fields and 403 protection remain intact. test: `crates/gcron/tests/http_writes.rs::update_job_matches_python`.
- 3.2.3 - DELETE /api/cron/jobs/{job_id} matches success, missing and protected rows. test: `crates/gcron/tests/http_writes.rs::delete_job_matches_python`.
- 3.2.4 - POST toggle preserves protection and normal rows; the exact D4 waiver returns safe 400 and an unchanged row for expired/invalid ordinary toggle-on without changing protected park/wake. test: `crates/gcron/tests/http_writes.rs::toggle_job_matches_python`.
- 3.2.5 - POST /api/cron/jobs/{job_id}/run uses shared admission and matches 200/404/409/429/503. test: `crates/gcron/tests/http_writes.rs::run_now_matches_python`.
- 3.2.6 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.3 Native restart protection query and lifecycle consumers [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gcron/src/protection.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/family.rs` — edit the file created by 3.1 to claim only the exact protected-runs admin route
- `crates/gdaemon/src/lifecycle/cron_guard.rs`
- `crates/gdaemon/src/lease/admin_api.rs::*` — scope-reason: expose a bounded native all-active count for local cooperative handoff
- `crates/gdaemon/src/lifecycle/mod.rs`
- `crates/gdaemon/src/main.rs::*` — scope-reason: enforce the shared Cron guard from native lifecycle command entry points
- `crates/gdaemon/src/cron_runtime.rs` — edit the file created by 2.8 to expose owner-local protection and forced cancellation to lifecycle commands
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
The HTTP count endpoint is a temporary bridge for `_lease.py`, owned here by
#21582. At S3.2, #21571 removes `/api/admin/cron/active-run-count` and its Python
`_lease.py` consumer together with the final native-client bridge. Native lease
handoff keeps calling Runs::count_running directly; no HTTP loopback remains.

**Granularity:** Query and lifecycle guard share the same restart lease; testing
them together proves that reporting and enforcement cannot diverge. Release
carriers exceed the file-count inspection threshold.

**Acceptance:**
- 3.3.1 - GET /api/admin/cron/protected-runs returns the original fields and timeout/owner filtering. test: `crates/gdaemon/tests/cron_protection.rs::protected_runs_query_matches_python`.
- 3.3.2 - Native lifecycle refuses protected work, waits when requested and force-interrupts only owned protected work; stale/expired leases do not wedge restart. test: `crates/gdaemon/tests/cron_protection.rs::restart_guard_honors_wait_and_force`.
- 3.3.3 - The authenticated control query counts ordinary unprotected local active runs, excludes foreign-machine work and fails closed on unavailable identity/database; it stays native in Proxy/Compare until its documented S3.2 removal with `_lease.py`. test: `crates/gdaemon/tests/cron_protection.rs::active_count_is_local_complete_and_fail_closed`.
- 3.3.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

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
removal at S3.2. #21570 removes the Cron MCP adapter's use of
`src/gobby/runner_cron_client.py` and `src/gobby/storage/cron*.py`; #21571 removes
the CLI's uses and, as final S3.2 owner, deletes the client, remaining shared
storage helpers and `_lease.py` consumer plus its temporary native active-count
HTTP endpoint. Sweep imports and string consumers before deletion; the shared
helpers remain only until their last live Python caller is ported. Neither
owner leaves a supervised daemon dependency after the naming switch.
Carry #24961 D1-D7's exact #24963 IDs from 1.1 into every affected operation
mapping, distinguishing ordinary Passed cases from explicit native replacements.
Use the single native domain-error classification (validation/missing/forbidden/
conflict/capacity/unavailable/internal) and preserve each adapter's intentional
envelope. HTTP's corrected schedule failure is safe 400; MCP and CLI expose
the same typed validation without copying the accidental PATCH server failure.
Apply Absent/Clear/Set at the service boundary with the field-specific nullable
contract from 1.3; adapter defaults never turn omitted values into Clear.
The D2/D4/D5 entries for MCP/CLI explicitly bind merged-candidate, atomic edit
and toggle-on invariants to their exact ledger surfaces.

D3 traversal is fixed: HTTP and MCP return one page, default 50, limit 1..200,
optional cursor and next_cursor null at end. #21570 adds optional limit/cursor
parameters and continuation output to list_cron_jobs, retaining success/jobs/
page-count fields, under `cron.mcp-list-jobs.pagination`. It never silently
walks all pages into an unbounded tool result. CLI list walks 50-row pages
through this same service for the complete listing, preserving its existing
text/JSON shape under `cron.cli-list.pagination`. Both retain filters and the
created_at DESC,id DESC keyset contract; only CLI aggregates the listing.
Planned: `cargo test -p gobby-cron --test service_contract` over 1.1 vectors.
The #21570 descriptor consumer also runs
`cargo test -p gobby-core --test mcp_contract` (or nextest with the same
`--test mcp_contract` target once #24949 lands), never a module-name filter.
The approved additive adapters are MCP get_cron_run_output (#21570) and CLI
gobby cron output (#21571), both calling 3.1a's same native bounded service.
Use offset default 0 and limit default 65,536/max 1,048,576. HTTP/CLI return raw
bytes; MCP returns run_id, offset, data_base64, bytes_returned, total_bytes and
next_offset (null at EOF), preserving invalid UTF-8 exactly. CLI accepts the run
ID plus optional --offset/--limit and writes the selected chunk to stdout.
Neither adapter silently fetches unbounded data; repeated bounded reads can
reconstruct the full raw stream. Transfer the exact
`cron.mcp-get-run-output.native-extension` and `cron.cli-output.native-extension`
waivers, runtime/project admission and 404/409/503 failure semantics with the
handoff. The original eighteen adapter rows remain owned as before; these two
new surfaces become delegated ledger rows when their owners deliver them.

**Acceptance:**
- 3.4.1 - Every CLI/MCP operation maps to one native service, preserving ordinary defaults/results/errors and asserting exact D1-D7 native replacements, field presence, atomic edit/toggle guards, bounded MCP pages and CLI traversal; park/wake and ID-or-name lookup remain intact. test: `crates/gcron/tests/service_contract.rs::delegated_operations_match_service_vectors`.
- 3.4.2 - Restricted-caller shell/protection, per-surface waivers, shared conventions, explicit #21570/#21571 cleanup, bounded list traversal and new raw-output adapter/ledger handoffs are complete. file: `docs/contracts/cron-services.md`.
- 3.4.3 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 3.5 Side-effect-safe comparison and native corpus replay [category: code] (depends: 3.4, #24963)
`kind: deliverable`

Targets:
- `crates/gcron/src/compare.rs`
- `crates/gcron/src/lib.rs`
- `crates/gcron/src/family.rs` — edit the file created by 3.1 to implement the existing proxied-result verification hook
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
native key-caller mode and retain the recorded Python origin. Cookie cases in
cron-browser.json remain a recorded Python oracle until 4.1b, never a native
parity success. Planned: crate comparison
tests and isolated e2e compare/replay with one fake child counter.
Use #24963's shared `docs/contracts/parity-waivers.json` loader/readiness
contract. For D1-D7 load 1.1's exact per-surface IDs and preserve their Python
oracle. A Waived outcome proves the specified native response and state/cleanup
effect and reports its decision citation; unrelated differences fail. Never
execute a second live mutation to verify changed Python behavior: preserve the
observed Python result, prove the replacement in isolated native replay/effect
tests, and associate that evidence with the shared waiver outcome. Unchanged
stable live cases remain ordinary Passed evidence. No mask or Skipped result
substitutes for a waiver. Require at least one ordinary Passed case and stable
proof of every required route/action/correction case; all-Skipped and unwaived-
Skipped readiness fail. Race skips remain diagnostic until that case is rerun
stably. D5 uses deterministic interleavings, D6 disposable-process liveness and
D7 exact raw bytes/inline limits/reload/retrieval/retention assertions. Zero
mismatches alone does not establish coverage. The additive output-read route
is an explicit D7 native extension, not fabricated Python-origin evidence.

**Granularity:** More than six target files include release and corpus carriers;
the single outcome is trustworthy compare evidence for this family.

**Acceptance:**
- 3.5.1 - Reads and verified writes detect tampered response/state, skip concurrent changes and never duplicate writes or actions. test: `crates/gcron/tests/compare.rs::compare_is_side_effect_free_and_race_aware`.
- 3.5.2 - All ten original HTTP cases replay ordinary parity or exact cited #24963 replacements; the additive output route has explicit native-extension coverage, origin, auth and effect assertions. Native legs require no Python; cookie proof waits for 4.1b. test: `tests/e2e/test_cron_family.py::test_native_cron_corpus_matches_python`.
- 3.5.3 - Controlled compare covers every required route/action/correction with stable evidence, zero unwaived mismatches and at least one ordinary Passed case; all-Skipped or unwaived-Skipped readiness fails and Waived needs its citation plus native effect proof. test: `tests/e2e/test_cron_family.py::test_compare_has_coverage_without_double_dispatch`.
- 3.5.4 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

## P4: Native takeover and Python retirement
`kind: framing`

### 4.1 Native default and single-owner scheduler cutover [category: code] (depends: 3.5, #24967)
`kind: deliverable`

Targets:
- `crates/gcron/src/family.rs` — edit the file created by 3.1 to make Native the family default after parity
- `crates/gdaemon/src/cron_runtime.rs` — edit the file created by 2.8 to coordinate scheduler admission with the takeover boundary
- `src/gobby/runner_init/orchestration.py::*` — scope-reason: replace Python cron construction with the bounded transition binding
- `src/gobby/runner_init/orchestration_services.py`
- `src/gobby/runner_cron_client.py`
- `tests/scheduler/test_native_cron_transition.py`
- `src/gobby/servers/routes/admin/_lease.py::*` — scope-reason: consume the complete native local active-run count for cooperative handoff
- `tests/servers/routes/test_daemon_lease_routes.py::*` — scope-reason: preserve unprotected-run and foreign-machine handoff behavior
- `tests/e2e/test_cron_family.py` — edit the file created by 3.5 to prove native default, backend independence and pre-retirement rollback
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for native takeover

**Research context:** RuntimeState's hub lease owns Rust services; Python's
init_orchestration previously constructed CronExecutor and CronScheduler.
This is the sole production scheduler activation leaf: both scheduled and manual
native admission remain closed through P3. #24967 supplies the shared
implementation-only browser verifier, excluding #24659's planning/root/retirement
outcome. Dispatch requires its landed exact-SHA Security-approved proof.
The routing switch here covers key
callers only: cookie requests continue to Python's Cron router, which uses the
native client for manual admission. Keep that remainder explicit until 4.1b
enables native Cron browser claims; Python Cron routes cannot be deleted first.
Resolve ownership once at the existing
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
That gate consumes #24963's shared readiness result: every D1-D7 correction
has its exact manifest ID and native replacement/effect proof, at least one
ordinary case is Passed, and no required case relies on unwaived Skipped.
Before Python deletion, proxy rollback and return to default require a routing
edit/restart of the same binaries. All key-caller actions and the protected-run
guard work with Python killed on the native default; cookie parity is proved
in 4.1b. Planned: isolated scoped transition
pytest and test_cron_family e2e; never kill the operator's backend.

**Granularity:** The transition binding and native default share the single
admission-owner transfer. More than six files are wiring, tests and releases.

**Acceptance:**
- 4.1.1 - Native is the key-caller default only after the restart-bound transfer; cookie requests stay proxied until 4.1b. Python/native never both admit scheduled or manual work, in-flight runs survive transfer, and pre-deletion proxy rollback excludes native before restoring Python with the same binaries. test: `tests/e2e/test_cron_family.py::test_default_flip_has_one_scheduler_owner`.
- 4.1.2 - With isolated Python killed, key callers can use the original ten HTTP routes plus bounded raw-output retrieval, four actions, nightly repair, system jobs and restart protection in hub/standalone; nodes create no jobs/runs. Cookie proof waits for 4.1b. test: `tests/e2e/test_cron_family.py::test_cron_survives_python_backend_loss`.
- 4.1.3 - Transition construction cannot leave Python scheduling live when native admission is enabled or native unavailable. test: `tests/scheduler/test_native_cron_transition.py::test_transition_never_has_two_admission_owners`.
- 4.1.4 - Cooperative handoff refuses an ordinary unprotected local active run, ignores foreign-machine work and fails closed when the native count is unavailable. test: `tests/servers/routes/test_daemon_lease_routes.py::test_handoff_uses_complete_local_native_cron_count`.
- 4.1.5 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

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

### 4.1b Admit Cron browser routes through the shared verifier [category: code] (depends: 4.1a, #24964, #24967)
`kind: deliverable`

Targets:
- `crates/gcron/src/browser.rs`
- `crates/gcron/src/family.rs` — edit the file created by 3.1 to register only Cron browser claims
- `crates/gcron/src/http_writes.rs` — edit the file created by 3.2 to consume verified browser authority without changing mutation semantics
- `crates/gcron/src/lib.rs` — edit the file created by 1.2 to export the bounded admission predicate
- `crates/gcron/Cargo.toml` — edit the manifest created by 1.2 to reuse the shared browser principal type
- `crates/gdaemon/src/front_door/mod.rs::FrontDoor::handle` — register Cron admission against the shared verifier/CORS boundary without implementing either capability here
- `crates/gdaemon/tests/cron_browser.rs`
- `tests/e2e/test_cron_family.py` — edit the file created by 3.5 to prove cookie operation with the isolated Python backend absent
- `docs/contracts/cron-services.md`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `src/gobby/install/version_pins.py::*` — scope-reason: release pin for bounded Cron browser admission

**Research context:** This leaf binds directly to #24967's shared browser-cookie
verifier implementation and #24964's shared native CORS/preflight boundary.
Neither #24659, a planning approval nor a whole-family retirement root is a
substitute. The Orchestrator checks the cross-plan graph before expansion.
Dispatch and activation require landed proof of
both capabilities. The shared verifier must expose both existing cookie
classes, verified user and applicable project/session selectors. Reuse its
parsing, storage lookup, expiry, revocation and typed principal.
Cron adds only its route allowlist and authority checks; no second verifier,
token issuer, session table, query cache or cookie lifecycle is introduced.
Immediately after expansion, the Orchestrator applies `security-review` to
this leaf and checks its persisted label before dispatch. M1 remains entirely
server-derived; no manifest-emitter change or hand-edited label is required.
Cookie activation stays blocked until a Security Reviewer records an approved
step for the exact implementation SHA, with all blocking findings disposed.

The allowlist is exactly GET /api/cron/jobs, GET /api/cron/jobs/{job_id},
GET /api/cron/jobs/{job_id}/runs, GET /api/cron/runs/{run_id}, POST /api/cron/jobs,
PATCH /api/cron/jobs/{job_id}, DELETE /api/cron/jobs/{job_id},
POST /api/cron/jobs/{job_id}/toggle and POST /api/cron/jobs/{job_id}/run.
The tenth route is GET /api/cron/runs/{run_id}/output, approved by the amended
D7 ruling. Its run/project lookup, byte bounds and range errors use 3.1a's
native service. Both cookie classes may read only within their existing
verified project/session authority; a caller cannot substitute a locator.
Use the family's existing method/path matcher for verifier admission and claims,
including its URI decoding and UUID behavior. Caller::Browser is opt-in for
these rows only; do not widen Caller::ALL, either /api/admin/cron endpoint,
lease admin or another family. HEAD/OPTIONS acquire no new business handler.
The separate shared native boundary answers applicable unauthenticated
preflights before auth and applies configured credentialed CORS headers to
native success and auth-error responses. Python app_factory's current layer
does not run after backend loss; it is the compatibility oracle, not the
native owner. Cron registers its ten-route method/path policy with
the shared boundary and adds no independent CORS implementation.

Python compatibility references are
`src/gobby/servers/middleware/auth.py::AuthMiddleware.dispatch`,
`src/gobby/servers/auth_service.py::AuthService.authenticate`,
AuthService._legacy_rejection, AuthService._cookie_user,
`src/gobby/servers/browser_sessions.py::browser_route_allowed`, and
`src/gobby/servers/app_factory.py`'s CORS middleware construction.
`src/gobby/servers/routes/auth.py::login` continues to issue the same
gobby_session cookie with HttpOnly, SameSite=Lax and path=/; this leaf changes
neither issuer nor cookie attributes. Ordinary cookies retain their current
GET and mutation behavior. Browser-testing cookies retain loopback, live
managed-authority and project/session selector restrictions: their GETs may
read, while Cron mutations are refused. Consume verified class/selectors from
the shared verifier to enforce this policy, rather than parsing a token or resolving
managed authority inside Cron. Missing/expired/revoked credentials, forged
identity/BrowserSession headers and unsupported callers fail before handlers.
Preserve existing bearer precedence and original routes' status/body/effects
against 1.1's Python vectors plus exact D1-D7 waiver assertions. The new output
read uses its approved native-extension vectors. Failed shared verification
never falls back to Python.

Observed evidence is complete gcode source reads: AuthMiddleware.dispatch,
excerpt_hash `65ab26e9e827b274251c9a8fff4d58cdd22cdd3cefebf6c0acc267245831f5fb`,
quotes `105| decision = await self.server.run_db(auth_service.authenticate, request)`
and `138| if not isinstance(body, dict) or not browser_selectors_match(body, project_id):`;
AuthService.authenticate, excerpt_hash
`5446a360254d1560903cc90fbf627e5ca101d65f38d32725d63d8fed11a9b79e`,
quotes `284| if cookie.startswith(BROWSER_COOKIE_PREFIX):` and
`295| else browser_route_allowed(str(request.scope["method"]), request_path(request))`.
AuthService._legacy_rejection, excerpt_hash
`48440f857e5f7cc0ed3a7099c327ed44dd4a98c77e7bc43dc01f35d87962a926`,
quotes `441| return None if self.validate_session(session_token) else "session_invalid"`.
`src/gobby/servers/app_factory.py:96-133`, excerpt_hash
`f916f63b2c3f71bbb83a501e49e9d3c3de37d8f92500edf6bffc0254d5df6595`,
sets configured origin matching and `127| allow_credentials=True`.
These inspected Cron/auth paths have no separate Origin or CSRF-token rejection.
Preserve the observed credentialed CORS/preflight and SameSite behavior; do not
silently add a stricter policy during this migration. Record same-origin,
cross-origin and preflight parity rather than assuming CORS rejects a mutation.

Keep cookie traffic proxied until this leaf's shared-verifier and route proofs
pass. Security reviews the exact admission diff and fixture results; dispose
all blocking findings before enabling native browser claims. Then prove both
cookie classes through hub and relaying-node front doors with the isolated
Python backend absent. Node doors verify no cookie locally. Release and
rollback use the existing coherent binary and routing boundaries.
Planned: `cargo test -p gobby-daemon --test cron_browser`, the focused
test_cron_family cookie case, and the required security review. All state,
credentials and backend termination remain inside isolated test fixtures.

**Granularity:** One lifecycle owner: opt-in Cron browser admission through the
shared prerequisite capabilities. The ten routes share one policy and fixture table; targets
above the six-file inspection threshold include release and evidence carriers.
The verifier and outer CORS boundary remain independently owned prerequisites.

**Acceptance:**
- 4.1b.1 - Nine original routes preserve ordinary-cookie status/body/effects under 1.1's Python vectors and exact D1-D7 waivers; the tenth output read follows its approved raw-byte contract. All use #24967 after landed proof, preserving credential precedence and expiry/revocation. test: `crates/gdaemon/tests/cron_browser.rs::cookie_get_and_mutations_match_python`.
- 4.1b.2 - Browser-testing GET authority and selector/loopback restrictions match Python; mutations, missing/non-cookie/non-key credentials and forged authority are refused before effects. No admin or other-family route gains browser admission. test: `crates/gdaemon/tests/cron_browser.rs::browser_authority_is_route_and_class_bounded`.
- 4.1b.3 - The bound shared native CORS boundary preserves same-origin/cross-origin status, headers and effects on success and auth-error responses, and handles applicable unauthenticated preflight before auth after Python backend loss. Current cookie behavior gains no stricter CSRF-token or Origin rejection. test: `crates/gdaemon/tests/cron_browser.rs::origin_and_preflight_match_python`.
- 4.1b.4 - Both cookie classes work through hub and relaying-node doors with the isolated Python backend absent; nodes verify no cookie locally and Python retirement waits for this proof. test: `tests/e2e/test_cron_family.py::test_cookie_cron_survives_python_backend_loss`.
- 4.1b.5 - The Orchestrator applies `security-review` after expansion and verifies the persisted label before dispatch. Cookie activation waits for a Security Reviewer's approved step on the exact implementation SHA; the service handoff records scope and disposition of all blocking findings. file: `docs/contracts/cron-services.md`.
- 4.1b.6 - gdaemon receives this leaf's patch bump, lock and pin. test: `tests/install/test_version_pins.py::test_managed_bin_pins_match_crate_versions`.

### 4.2 Delete Cron-owned Python and release the nightly cleanup boundary [category: code] (depends: 4.1b)
`kind: deliverable`

Targets:
- `src/gobby/scheduler/scheduler.py::*` — operation: delete — scope-reason: native scheduler owns ticking and manual admission
- `src/gobby/scheduler/executor.py::*` — operation: delete — scope-reason: all four action variants have native bindings
- `src/gobby/servers/routes/cron.py::*` — operation: delete — scope-reason: its nine Cron HTTP routes are native; the protected-run admin route is retired with its separate consumer
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
- `tests/e2e/test_cron_family.py` — edit the file created by 3.5 to verify operation after Python scheduler/HTTP/nightly deletion
- `docs/contracts/cron-services.md`

**Research context:** 4.1b's reviewed browser admission and backend-loss proof
must pass before this deletion; 4.1's key-caller takeover alone is insufficient.
The literal cron_scheduler sweep finds app_context,
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
- 4.2.3 - Native key and cookie routes, actions, nightly repair and restart protection work after source deletion and have no proxy fallback; browser authority remains bounded by 4.1b. test: `tests/e2e/test_cron_family.py::test_retired_python_is_unnecessary`.
- 4.2.4 - #24713's documented boundary has no nightly module reader, and all remaining transitional/shared-helper paths have a naming-switch removal owner. file: `docs/contracts/cron-services.md`.

### 4.3 Parity ledger and operator handoff [category: docs] (depends: 4.2)
`kind: deliverable`

Targets:
- `docs/contracts/parity-ledger-status.d/comms-cron-hub-nodes.json::*` — scope-reason: delegate the nine proven original Cron HTTP rows, add the delivered native output HTTP row and retain original CLI/MCP rows with owner notes
- `docs/contracts/parity-ledger-status.d/daemon-config-auth-infra.json::*` — scope-reason: move the proven protected-run HTTP row from proxied to delegated with its evidence note
- `docs/contracts/parity-ledger.md`
- `docs/contracts/cron-services.md`
- `docs/guides/configuration.md`

**Research context:** The ledger generator reads status fragments; edit the
fragments then regenerate its Markdown using the existing ledger tooling.
All 28 rows are accounted for in Parity ownership. Change the ten HTTP rows
from `proxied` to `delegated` only after key and cookie corpus/backend-loss
checks, including 4.1b's accepted Security review.
The ten CLI and eight MCP rows remain `proxied`, with explicit #21571/#21570
owner notes, until those owners deliver their ports. Use only the ledger's
three valid statuses: `proxied`, `delegated`, `dropped by decision #N`.
Also record the delivered additive GET /api/cron/runs/{run_id}/output as
delegated, retaining its native-extension origin and exact
`cron.get-run-output.native-extension` citation. #21570/#21571 add the new MCP
get_cron_run_output/CLI cron output rows when delivered; document their owner
handoffs now without fabricating a proxied Python implementation. Thus this
handoff accounts for all original 28 plus the new HTTP row, and the complete
cross-owner inventory reaches 31 after both adapter additions land.
The ledger generator, replay and readiness consume #24963's same
docs/contracts/parity-waivers.json entries. Link every D1-D7 affected owner
note to its exact surface waiver and explicit replacement proof. A Waived case
does not drop its surface, normalize an unrelated difference or excuse missing
stable evidence. Document the single error/presence/readiness/CORS conventions
and #24968 output storage/run-owned retention. Include the amended full-raw
retention ruling, inline budget, raw byte count, retrieval limits and capture
failure semantics in cron-services.md.
Document hub-only ownership, park/
wake, restart --wait/--force, the native default, pre-deletion routing rollback
and post-deletion prior-release rollback. Carry the S3.2 same-change Python
removal requirement and exact outstanding client/helper paths into the
front-door handoff. Carry 4.1b's post-expansion Orchestrator label verification
and exact-SHA Security approved-step gate into that handoff. No new task
expansion or operator action happens here.
Planned: ledger consistency check, scoped documentation validation and base/
expansion plan validation after the adversary stamps M1.

**Acceptance:**
- 4.3.1 - All original 28 rows remain accounted: ten proven HTTP rows become delegated and eighteen CLI/MCP rows stay proxied with their owners. The new output HTTP row is delegated with native provenance; the two new adapter rows are explicit #21570/#21571 delivery handoffs. Every D1-D7 affected row cites its exact shared waiver; only valid statuses are used. file: `docs/contracts/parity-ledger.md`.
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
After approved future expansion, the Orchestrator applies and verifies
`security-review` on 4.1b before dispatch. Native cookie activation requires a
Security Reviewer's approved step for the exact implementation SHA.
Cron expansion requires cycle-checked concrete #24963/#24964/#24967/#24968
implementation edges in freshly reviewed/validated bytes. Dispatch/activation
requires landed shared-capability proof. The #24961 debt report is folded into
D1-D7 exact-surface waivers and K1 is retained; #24963 readiness rejects zero-
Passed/all-Skipped evidence. D7's full raw storage/retrieval/retention and
three nullable schema columns are explicit, separately verified outcomes.

## V1 Plan Changelog
`kind: framing`

2026-10-24: Incorporated the Orchestrator's #24961 debt dispositions and later
clarifications: D1-D7 exact shared waivers, K1 retained, concrete pagination,
presence/error/readiness conventions, #24963/#24964/#24967/#24968 prerequisite
bindings, additive schema and raw-output retrieval outcomes. The initial
lossy D7 proposal was amended to preserve all raw bytes under Josh's existing
preference; only the inline copy is bounded, and Cron run deletion owns its
overflow. HTTP/MCP/CLI traversal and new output handoffs are concrete.
Fresh adversary consensus and M1 derivation remain required for these bytes.

2026-10-24: Accepted CRON-004/005 under the Orchestrator's explicit binding
ruling. Only the early shared-verifier implementation may gate Cron; exclude
the planning/root/retirement back-edge through #24713. A separate shared native
CORS/preflight owner under #21542 supplies outer middleware after backend loss.
Both concrete references, graph checks and refreshed review are mandatory before
Cron expansion; dispatch/activation require landed capability proof. The
read-only #24961 debt pass must be dispositioned and folded before stamping.

2026-10-24: Coordinator review withdrew the prior consensus and stamp. Accepted
the ledger-status correction and six clarifications: ordered acceptance IDs,
earlier-created edit targets, #24702's sole prune ownership, #24659's shared
browser verifier with bounded security-reviewed Cron admission, S3.2 active-count
bridge removal, and the #21570/#21571 cleanup handoffs now recorded on their
tasks. Added 4.1b and current-cookie oracle coverage; key takeover precedes cookie
parity and deletion follows it. Descriptor consumers select the complete
`--test mcp_contract` target. The old M1 is superseded and will be withdrawn
whole before fresh narrative validation and adversary review.

2026-10-24: Writer and Plan Adversary reached consensus on all 20 deliverables
after fresh review of 3e0b2cce525c4cc4b413a7c21c75db72c1611519. CRON-001 is
resolved by passive native services until the exclusive restart-bound owner
transfer; CRON-002 by current-schema delivered-mailbox retention under the
explicit Orchestrator ruling, with #22340 remaining parked; CRON-003 by the
complete local active-run count used for lease handoff. No blocking or nit
findings and no unresolved questions remain.

2026-10-24: Accepted CRON-001/002/003: keep pre-cutover native services passive;
port current-schema mailbox retention under the Orchestrator ruling without
unparking #22340; preserve complete local active-run handoff counting through
a native control query. Revalidate these repairs before renewed review.

2026-10-24: Initial narrative prepared for #24952 after Orchestrator scope and
ownership confirmation. Includes the 28-row inventory, native nightly handler
dependency, Cron Python retirement and explicit CLI/MCP naming-switch handoffs.
