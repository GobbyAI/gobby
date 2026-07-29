# Memory Dream Reliability Redesign

**Plan ID:** memory-dream-reliability-redesign

## Overview
`kind: framing`

Redesign Memory Dream as a durable, bounded maintenance pipeline that makes
useful progress across every due memory scope without exhausting daemon
resources. Healthy runs complete without warning/error logs or timeout events;
real dependency failures remain visible, checkpoint completed work, and leave
unprocessed memories due.

The 0.5.0 scheduler uses a 2:00–6:00 AM admission window. Work is divided into
25-candidate units, distributed round-robin across project and global scopes,
and limited to one Dream planner call at a time.

## Constraints
`kind: framing`

- PostgreSQL remains authoritative for run admission, status, checkpoints,
  memory cursors, snapshots, and revert data.
- Keyword, stored-vector, and hydration evidence channels are all required in
  every deployment mode. This plan removes the local-mode stored-vector gate
  (`supports_stored_vector_search`) entirely — the gate was a workaround for
  the old per-page fanout, and qdrant-client 1.16.2 local mode implements
  `retrieve` and `query_batch_points`. A successful empty result is valid
  evidence; unavailable or exhausted channels stop the run before affected
  candidates are planned or stamped.
- Nightly runs apply every validated action that passes the existing general,
  delete, and rescope confidence thresholds.
- The final batch admitted before 6:00 AM may finish afterward, bounded by the
  25-minute work-unit deadline.
- Dream consumes at most one of the host-wide three spawn-cold generation
  slots, preserving two slots for summaries, recall, and interactive work.
  Today `planner_max_concurrency` defaults to 3 — equal to
  `spawn_cold_max_concurrency` — so Dream can occupy every host-wide slot;
  this plan reduces Dream-local planner concurrency to exactly one.
- No compatibility layer is required before 0.5.0. Remove obsolete page-era
  settings and synchronous request paths instead of aliasing them.
- Preserve missing-pane liveness INFO telemetry and compact-continuation
  behavior. This redesign does not change liveness monitoring.
- Keep every touched non-test Python module below 1,000 lines. The current
  979-line Dream service must delegate orchestration to a focused module before
  receiving substantial new behavior.
- Do not run the full pytest suite. Use the focused commands in P4.

## P1: Evidence and Durable Run Foundations
`kind: framing`

**Goal**: Replace per-candidate fanout with bounded evidence calls and make run
admission/checkpoints authoritative and race-safe.

### 1.1 Implement batched required-evidence retrieval [category: code]
`kind: deliverable`

Targets:
- `src/gobby/memory/dream/related.py`
- `src/gobby/memory/services/keyword.py`
- `src/gobby/memory/services/crossref.py`
- `src/gobby/memory/services/lifecycle.py`
- `src/gobby/memory/vectorstore.py`
- `src/gobby/memory/vectorstore_client.py`
- `src/gobby/memory/vectorstore_queries.py`
- `src/gobby/memory/dream/service.py`
- `tests/memory/test_dream_related.py`

Replace the 200-task keyword dispatch with one bounded keyword operation per
work unit. Add a bulk keyword-search entry point that accepts up to 25
`(candidate_id, distinctive_terms)` queries, preserves the existing scope
filters and per-candidate hit limit, and returns results keyed by candidate ID.
Execute it through one database connection/round trip; a parameterized
`UNION ALL` of the existing rendered per-query statements is acceptable and
preserves current PG Search ranking semantics
(`MemoryKeywordSearchService.render_search` already renders SQL + params
without executing).

Delete the `supports_stored_vector_search` gate entirely:

- property definitions at `src/gobby/memory/vectorstore_client.py:103-104` and
  `src/gobby/memory/vectorstore.py:106-107`;
- the `VectorStoreUnavailableError("Stored-vector search is disabled in local
  mode")` raise at `src/gobby/memory/vectorstore_queries.py:113-114`;
- the Dream consumer at `related.py:479` and the crossref consumer at
  `src/gobby/memory/services/crossref.py:224`.

Local Qdrant mode serves stored-vector batch search through the existing
`asyncio.to_thread` local-call path. Crossref revalidation keeps its existing
exception fallback to previously computed scores. Update the write-time
related-evidence consumer in `src/gobby/memory/services/lifecycle.py` to the
revised `gather_related_evidence` contract.

For each 25-candidate work unit:

1. Run one bulk keyword call and one `search_by_stored_vectors` call
   concurrently.
2. Rank their results only after both channels succeed.
3. Run one bulk hydration call for the deduplicated ranked IDs.
4. Treat empty keyword/vector/hydration results as successful channel output.
5. Preserve successful channel output while retrying another channel.
6. Require all three successful channels before returning enriched candidates.

Use these fixed budgets:

- 30 seconds per channel attempt, including database-pool admission.
- Three attempts per channel, with 1- and 4-second backoffs.
- 210 seconds for the complete evidence phase.

Expected retry attempts log structured INFO telemetry containing run ID, scope,
batch, channel, attempt, pool-wait duration, execution duration, and outcome.
After the final failed attempt, raise one typed dependency failure to the
coordinator. Remove the page-era bounds and their machinery:
`RELATED_EVIDENCE_PAGE_DEADLINE_SECONDS` (15s page deadline),
`RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS` (5s per-call timeout charged before
DB-semaphore admission), `RELATED_EVIDENCE_CHANNEL_TRIP_LIMIT` (the
session-scoped per-channel trip latch — a permanent latch that trips silently
and never resets), `RELATED_EVIDENCE_DRAIN_TIMEOUT_SECONDS`, the four-slot
`_db_semaphore`, the 200 per-candidate tasks, and the page-timeout warnings.
Drain and cancellation must leave no child tasks alive.

**Acceptance:**

- 1.1.1 - A 25-candidate work unit performs one keyword DB operation, one vector operation, and at most one hydration DB operation while preserving per-candidate scope and ranking semantics. symbol: `gobby.memory.dream.related.gather_related_evidence`.
- 1.1.2 - Empty results from all three available channels complete successfully and attach empty evidence. test: `tests/memory/test_dream_related.py`.
- 1.1.3 - A failed channel retries independently while successful channel results are reused; three exhausted attempts raise a typed channel-specific failure and drain every task. test: `tests/memory/test_dream_related.py`.
- 1.1.4 - Saturation coverage above the former four-connection capacity proves bounded DB usage, completion within the 210-second evidence budget, and zero `channels=page` or keyword-timeout warning paths. test: `tests/memory/test_dream_related.py`.
- 1.1.5 - Stored-vector evidence executes in local Qdrant mode; the `supports_stored_vector_search` symbol no longer exists repo-wide; crossref revalidation runs in local mode with its exception fallback preserved. symbol: `gobby.memory.vectorstore_queries.VectorStoreQueries.search_by_stored_vectors`.

### 1.2 Add singleton admission and durable batch checkpoints [category: code]
`kind: deliverable`

Targets:
- `src/gobby/memory/dream/storage.py`
- `src/gobby/memory/dream/models.py`
- `src/gobby/memory/dream/service.py`
- `src/gobby/memory/dream/cron.py`
- `src/gobby/runner_init/orchestration.py`
- `src/gobby/sessions/lifecycle.py`
- `src/gobby/storage/postgres_baseline_schema.sql`
- `src/gobby/storage/migrations/348_memory_dream_admission.sql` (new)
- `tests/memory/test_dream.py`

`memory_dream_runs` is defined in two places that must stay in sync: the
baseline schema (`postgres_baseline_schema.sql:938-955`) and the inline
`CREATE TABLE IF NOT EXISTS` duplicate in `MemoryDreamStore.ensure_schema()`
(`storage.py:95-120`). Apply every schema change to both plus migration 348:

- add `partial` to the `memory_dream_runs_status_check` CHECK constraint as a
  terminal status;
- add a PostgreSQL partial unique index that permits only one `running` row
  (`WHERE status = 'running'`);
- add a `checkpoint` JSONB column for durable progress, and extend the
  `_RUN_UPDATE_SET_CLAUSES` allowlist (`storage.py:48-61`) to accept it.

Name the terminal-status set as one shared constant; today it is an inline set
literal in `record_run_failure` (`service.py:540-546`).

Startup recovery must mark stale pre-restart rows `interrupted` before
reconciling the index. Reuse/extend the existing `mark_interrupted_runs`
(`storage.py:299-323`), which sweeps `status IN ('started', 'running')`. The
startup call site is `reconcile_interrupted_dream_runs` invoked from
`src/gobby/runner_init/orchestration.py:595-608`; keep recovery ahead of the
single-running-row index reconciliation.

Add an atomic admission method that either:

- creates the sole running row and returns it as newly admitted;
- returns the existing row as coalesced when its normalized options are
  equivalent to or cover the request; or
- returns a typed conflict containing the active run ID, scope, options, phase,
  and checkpoint.

Normalized options cover every `DreamRunOptions` field: `dry_run`,
`skip_consolidation`, `memory_type`, `full_sweep`, `project_id`,
`global_only`, and `include_global`. An all-due run covers a project request
when `dry_run`, `skip_consolidation`, `memory_type`, and `full_sweep` match
and the request does not narrow `include_global` incompatibly. Project runs
cover only the same project and options. All other combinations conflict.
Define the admission scope key explicitly for rows whose `project_id` IS NULL:
`memory_dream_runs.project_id` is nullable and NULL for global/all-due runs,
unlike the `memories` table where global scope is `is_global = true` with a
non-null owning `project_id`. A failed background-task launch must transition
its admitted row to `failed`.

Persist the checkpoint after every completed work unit with:

- current phase and scope;
- pass number and batch number;
- selected, completed, skipped-fence, and remaining candidate counts;
- per-channel attempts and latency;
- planned/action/mutation counts;
- backlog by scope;
- `stop_reason` and the latest actionable dependency failure.

Candidate actions remain individually transactional and fenced by due version
and selected state. On cancellation, committed candidates remain complete, the
in-flight transaction rolls back, and untouched candidates remain due for the
next run.

Update service/cron construction and the startup recovery consumer to use the
new admission and stale-run reconciliation methods. Keep the periodic
retention consumer of `MemoryDreamStore` in `src/gobby/sessions/lifecycle.py`
(`_purge_dream_hidden_memories`, `prune_runs`) consistent with the revised
store construction.

**Acceptance:**

- 1.2.1 - PostgreSQL prevents concurrent running Dream rows and atomically distinguishes admitted, coalesced, and conflicting requests under a race. symbol: `gobby.memory.dream.storage.MemoryDreamStore`.
- 1.2.2 - `partial` runs preserve completed counts, remaining backlog, stop reason, and channel telemetry in their persisted checkpoint. test: `tests/memory/test_dream.py`.
- 1.2.3 - Restart recovery marks stale started/running rows interrupted and a later admission continues from naturally due candidates without replaying committed actions. test: `tests/memory/test_dream.py`.
- 1.2.4 - Equivalent and covered requests return the active run ID; incompatible requests create no row and return active-run details. test: `tests/memory/test_dream.py`.
- 1.2.5 - Migration 348, the baseline schema, and `ensure_schema()` define identical status vocabulary, partial unique index, and checkpoint column. file: `src/gobby/storage/migrations/348_memory_dream_admission.sql`.

## P2: Bounded Work-Unit Orchestration
`kind: framing`

**Goal**: Execute fair, resumable Dream work with correct planner semantics,
resource admission, mutation policy, and nightly timing.

### 2.1 Extract and implement the work-unit runner [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/memory/dream/orchestrator.py` (new)
- `src/gobby/memory/dream/service.py`
- `src/gobby/memory/dream/planner.py`
- `src/gobby/memory/dream/options.py`
- `tests/memory/test_dream.py`

Move long-running selection/evidence/planning/apply orchestration out of
`MemoryDreamService` into typed work-unit/coordinator objects. Keep the service
as the facade for admission, status, revert, and execution entry points.

One ordinary work unit:

1. Select at most `planner_batch_size` eligible candidates from one explicit
   scope; default and maximum supported unit size is 25.
2. Gather required evidence through 1.1.
3. Call the planner serially for that unit. Reduce Dream-local planner
   concurrency from today's three (`planner_max_concurrency` default 3, equal
   to the host-wide `spawn_cold_max_concurrency`) to exactly one, so Dream can
   occupy at most one shared spawn-cold generation slot.
4. Validate every action against candidate IDs and existing confidence
   thresholds.
5. Apply validated actions with existing snapshots, fences, reconciliation,
   and notifications.
6. Persist the checkpoint from 1.2 before yielding to the next scope.

The generation service enforces a 600-second per-candidate cap on spawn-cold
lanes, but no overall provider-fallback deadline exists on the in-process
Dream path today: `LLMService._build_request` never sets
`total_timeout_seconds`, so `generate_result` skips its whole-chain
`wait_for` and N fallback candidates can take N × 600 seconds. The planner
generation request must set the overall 1,200-second deadline explicitly so
the work-unit ceiling is sound. Wrap the whole work unit in a 1,500-second
deadline. Planner absence, invalid terminal output, or exhausted provider
fallback is a typed dependency failure; it must not degrade to implicit keep
actions or stamp candidates.

Propagate `DreamRunOptions.skip_consolidation` instead of hardcoding `False`
(the literal `skip_consolidation=False` sites are `service.py:744` and
`service.py:816`). For `skip_consolidation=true`, materialize the immutable
eligible-ID inventory and counts, then finish with zero evidence calls,
planner calls, actions, snapshots, mutations, or cursor writes. Inventory
candidates remain due.

Dry runs keep the existing immutable, strictly ordered eligible-ID snapshot
contract: materialize the snapshot at run start (bounded by
`dry_run_max_candidates`), iterate it in 25-candidate work units in snapshot
order, and store complete validated dry-run actions in `run.plan.actions` for
`gobby memory dream status` rendering.

**Acceptance:**

- 2.1.1 - Ordinary work units enforce the sequence select → required evidence → one planner → validate → apply → checkpoint and never exceed 25 candidates. symbol: `gobby.memory.dream.orchestrator`.
- 2.1.2 - Dream planner concurrency is exactly one (reduced from three) while the host-wide generation limit remains three, allowing two unrelated generation calls to proceed. test: `tests/memory/test_dream.py`.
- 2.1.3 - Planner dependency failures create no keep actions or cursor stamps and return a typed failure with all candidates still due. test: `tests/memory/test_dream.py`.
- 2.1.4 - `skip_consolidation=true` records only candidate inventory/counts and performs zero evidence, LLM, action, snapshot, mutation, and cursor calls. test: `tests/memory/test_dream.py`.
- 2.1.5 - Refactoring leaves every touched production Python module below 1,000 lines without changing apply/revert fences. file: `src/gobby/memory/dream/service.py`.
- 2.1.6 - Dry runs materialize one immutable ordered eligible-ID snapshot, process it in snapshot order, and render complete validated actions from `run.plan.actions`. test: `tests/memory/test_dream.py`.

### 2.2 Implement fair nightly scheduling and actionable stop states [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/memory/dream/orchestrator.py`
- `src/gobby/memory/dream/cron.py`
- `src/gobby/config/persistence.py`
- `tests/memory/test_dream.py`
- `tests/memory/test_dream_cron.py`
- `tests/config/test_persistence.py`
- `tests/config/test_feature_base.py`

Replace scope-draining serial execution with round-robin passes. Each pass
executes one work unit for every currently due project/global scope in stable
order, refreshes backlog counts, then starts another pass while time remains.
Scopes with no remaining candidates drop out (a scope can be enumerated as due
by `list_dream_scopes` yet yield zero candidates, because scope enumeration
does not apply the `review-lesson` tag exclusion that candidate listing does).
A dependency failure stops the entire coordinator after retries; completed
checkpoints remain durable and the failed/untouched candidates remain due.

`max_runtime_seconds` bounds every coordinator run regardless of trigger. For
cron-triggered runs:

- change the default schedule from `0 3 * * *` to `0 2 * * *`;
- admit new work units for 14,400 seconds from cron start;
- allow the final admitted unit to finish under its 1,500-second deadline;
- run with `dry_run=false` (today the nightly cron runs
  `dry_run=not allow_unattended_mutations`, i.e. dry-run by default — this
  plan makes nightly mutating maintenance the default);
- apply all validated actions using existing confidence gates, snapshots, and
  revert support;
- a cron fire that finds an active run follows the same admission contract:
  coalesce or conflict logs INFO and returns without error.

Window exhaustion yields `status=partial`,
`stop_reason=window_exhausted`, and an INFO completion summary. Exhausted
dependency retries yield `status=partial`,
`stop_reason=dependency_failure`, and exactly one actionable WARNING naming
the channel/provider, attempts, last error, completed work, and remaining
backlog. Structural/invariant failures yield `failed` and ERROR.

Update `MemoryDreamConfig` as a pre-0.5 schema change:

- keep `planner_batch_size=25`, `planner_batch_max_chars`,
  `dry_run_max_candidates`, cooldown, confidence, retention, and reconciliation
  settings;
- set `schedule_cron="0 2 * * *"`;
- add `max_runtime_seconds=14400`,
  `work_unit_timeout_seconds=1500`,
  `evidence_channel_timeout_seconds=30`,
  `evidence_retry_attempts=3`, and
  `evidence_phase_timeout_seconds=210`;
- remove `allow_unattended_mutations`, `planner_max_concurrency`, `page_size`,
  and `candidate_page_timeout_seconds`, plus the deprecated ignored fields
  `scan_limit`, `max_scan_rows`, and `stale_age_days`;
- update the field validators: `validate_positive_float` loses
  `candidate_page_timeout_seconds` and gains the new float timeout fields;
  `validate_positive_int` covers the new integer fields.

**Acceptance:**

- 2.2.1 - Multiple due scopes receive one batch per pass in round-robin order, and a large first project cannot starve later project/global scopes. test: `tests/memory/test_dream.py`.
- 2.2.2 - No new unit starts after the four-hour admission deadline; the last admitted unit may finish within 25 minutes and the run persists a normal window-exhausted partial checkpoint. test: `tests/memory/test_dream.py`.
- 2.2.3 - An exhausted evidence or planner dependency stops further scope processing, emits one actionable failure summary, and leaves remaining candidates due. test: `tests/memory/test_dream.py`.
- 2.2.4 - Nightly cron starts at 2:00 AM, executes mutating maintenance, and reports completed/partial outcomes without warning on window exhaustion. test: `tests/memory/test_dream_cron.py`.
- 2.2.5 - Config validation exposes only the batch/window settings, rejects non-positive timeout, attempt, and size values, and no longer accepts the removed page-era or deprecated fields. test: `tests/config/test_persistence.py`.

## P3: Immediate Triggers and Observable Progress
`kind: framing`

**Goal**: Eliminate synchronous proxy timeouts and invisible lock queues across
MCP, HTTP, and CLI entry points.

### 3.1 Make HTTP and MCP Dream triggers always asynchronous [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/memory_dream.py`
- `src/gobby/mcp_proxy/tools/memory_dream.py`
- `src/gobby/runner_init/orchestration.py`
- `tests/servers/routes/test_memory_routes.py`
- `tests/mcp_proxy/tools/test_memory.py`

Remove the public `wait` argument and every synchronous long-running path.
Today the defaults are asymmetric — HTTP `wait=False`, MCP `wait=True` — and
MCP `memory_dream` is an internal tool bounded only by the stdio proxy's
client-side HTTP timeout (30s default / 300s extended), which a real
synchronous sweep cannot survive. HTTP `POST /memory/dream` and MCP
`memory_dream` must perform admission, launch the coordinator when newly
admitted, and return the run ID immediately.

Response behavior:

- newly admitted: HTTP 202 / MCP success with
  `status="running"`, `run_id`, and `coalesced=false`;
- equivalent or covered active run: HTTP 200 / MCP success with the same
  `run_id`, current progress, and `coalesced=true`;
- incompatible active run: HTTP 409 / MCP failure with
  `error_code="dream_run_conflict"` and active-run details;
- launch failure: terminal `failed` row plus HTTP/MCP failure containing its
  run ID.

Admission control today is split and inconsistent: `MAX_BACKGROUND_DREAM_TASKS`
and its four-slot `BoundedSemaphore` exist only in the MCP tool module
(`mcp_proxy/tools/memory_dream.py:22-25`), while the HTTP route has no
admission control at all; service construction differs per surface (HTTP fresh
per request, MCP cached closure, cron fresh per fire); and background-task
tracking is split between `HTTPServer._background_tasks` and the MCP module
task set. Remove `MAX_BACKGROUND_DREAM_TASKS`, the semaphore, the MCP
module-level task set with its `cleanup_background_dream_tasks` shutdown hook,
and the duplicated HTTP/MCP task-launch branches. Route cron, HTTP, and MCP
through one daemon-owned coordinator/admission owner constructed at runner
init; HTTP routes and MCP tools resolve it instead of constructing
`MemoryDreamService` ad hoc. `memory_dream_status` returns the persisted
checkpoint fields from 1.2 for running and terminal runs.

**Acceptance:**

- 3.1.1 - HTTP and MCP triggers return a run ID without waiting for evidence or generation and expose no `wait` parameter. behavior: `POST /memory/dream`.
- 3.1.2 - Concurrent equivalent/covered requests coalesce and conflicting requests return active-run details without creating phantom running rows. test: `tests/servers/routes/test_memory_routes.py`.
- 3.1.3 - MCP uses the same admission contract and holds at most one background Dream coordinator task. test: `tests/mcp_proxy/tools/test_memory.py`.
- 3.1.4 - Status responses expose phase, scope, batch counts, backlog, attempts/latency, mutations, and stop reason from durable state. behavior: `memory_dream_status`.

### 3.2 Make the CLI poll asynchronous runs to terminal state [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/memory/dream.py`
- `tests/cli/test_memory_cli.py`

The CLI is already asynchronous and broken: `gobby memory dream` never sends
`wait`, receives HTTP 202, and renders `Swept 0/0 project(s): 0 mutation(s)`
from absent aggregate fields, while its docstring and
`tests/cli/test_memory_cli.py:564-567` assert synchronous behavior against a
fabricated mock response. This deliverable replaces a live bug, not a working
synchronous path.

Change `gobby memory dream` to POST once with a short network timeout, print
the returned run ID, then poll `GET /memory/dream/{run_id}` every two seconds.
Render phase, scope, pass/batch, completed/remaining candidates, mutations, and
the latest retry state whenever the checkpoint changes.

Redefine `--timeout` as a client-side polling deadline. Default `0` waits until
terminal status. An explicit deadline or Ctrl-C stops only the CLI wait,
preserves the daemon run, and prints the status command needed to resume
observation. Terminal `completed` exits zero; `partial`, `failed`, and
`interrupted` exit non-zero with run ID, stop reason, completed counts, and
remaining backlog. Inventory-only runs render candidate IDs/counts and clearly
state that candidates remain due.

**Acceptance:**

- 3.2.1 - CLI starts asynchronously, prints the run ID immediately, polls changed progress, and renders a completed summary. test: `tests/cli/test_memory_cli.py`.
- 3.2.2 - Default polling has no client deadline; explicit timeout and Ctrl-C leave the server run active and print a resumable status command. test: `tests/cli/test_memory_cli.py`.
- 3.2.3 - Partial dependency/window outcomes and inventory-only outcomes render their distinct stop/progress semantics and correct exit codes. test: `tests/cli/test_memory_cli.py`.

## P4: Focused and Live Acceptance
`kind: framing`

**Goal**: Prove the redesign under deterministic saturation tests and one real
project-scoped Dream run after daemon restart.

### 4.1 Validate focused regressions and a live Dream dry run [category: test] (depends: P3)
`kind: deliverable`

Targets:
- `tests/memory/test_dream_related.py`
- `tests/memory/test_dream.py`
- `tests/memory/test_dream_cron.py`
- `tests/mcp_proxy/tools/test_memory.py`
- `tests/servers/routes/test_memory_routes.py`
- `tests/cli/test_memory_cli.py`

Run focused validation after the final implementation edit:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/memory/test_dream_related.py \
  tests/memory/test_dream.py \
  tests/memory/test_dream_cron.py \
  tests/config/test_persistence.py \
  tests/config/test_feature_base.py \
  tests/mcp_proxy/tools/test_memory.py \
  tests/servers/routes/test_memory_routes.py \
  tests/cli/test_memory_cli.py \
  tests/mcp_proxy/tools/sessions/test_compact_self_readiness.py \
  tests/sessions/test_compact_continuation.py \
  tests/sessions/test_liveness_monitor.py -q
```

Run Ruff on every touched source/test path, Mypy on touched production modules,
and both test audits on touched Python tests:

```bash
uv run ruff check <touched-source-and-test-paths>
uv run mypy <touched-production-modules>
uv run gobby test-quality audit <touched-test-paths> \
  --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high
uv run gobby test-types audit <touched-test-paths> \
  --baseline .gobby/test-types-baseline.json --fail-on-new
```

Then capture daemon PID and log offset, gracefully restart Gobby, and verify a
new healthy PID. Start one real project-scoped `dry_run=true`,
`skip_consolidation=false` Dream through MCP, capture its immediate run ID, and
poll status until terminal. The run must complete with all three evidence
channels and real planner generation; inventory-only mode is insufficient for
this acceptance.

Inspect only the exact daemon-log interval from admission through completion.
Require:

- terminal `completed` status;
- no WARNING or ERROR records;
- no timeout or circuit-breaker records;
- no `channels=page` or `channels=keyword` timeout attribution;
- no old compact-readiness or missing-`project_id` warning;
- complete progress/checkpoint fields and zero leaked Dream tasks;
- successful compact continuation after the restart.

Missing-pane liveness INFO events remain allowed for genuinely vanished panes.

**Acceptance:**

- 4.1.1 - The exact focused pytest command passes under `GOBBY_TEST_PROTECT=1`, covering retrieval, orchestration, cron, config, HTTP, MCP, CLI, compact continuation, and liveness. behavior: focused validation transcript.
- 4.1.2 - Ruff, Mypy, test-quality audit, and test-types audit pass on all touched paths with definitive exit codes. behavior: static-validation transcript.
- 4.1.3 - Graceful restart produces a new healthy daemon PID and compact continuation succeeds. behavior: daemon restart validation.
- 4.1.4 - A real project-scoped consolidation dry run reaches `completed` with zero warnings, errors, timeout records, circuit-breaker records, or leaked Dream tasks in its exact log interval. behavior: live Memory Dream acceptance.

## F1: Post-0.5 Load-Aware Scheduling Follow-up
`kind: framing`

After this plan is approved and expanded into its implementation epic, create a
post-0.5.0 child task under that epic for a “dream when idle” scheduler. The
future scheduler replaces the fixed admission window with daemon load signals,
starts/resumes bounded work only while idle, yields immediately when
interactive work appears, and reuses this plan’s checkpoints, required-evidence
contract, singleton admission, and one-slot generation budget. The 0.5.0 work
does not implement idle detection or preemption.

## V1 Plan Changelog
`kind: verification`

No enhancement or adversarial review rounds have run. The confirmed interactive
decision record is represented in this draft. A pre-review investigative
revision (2026-07-29) verified every premise against the codebase and applied
corrections: removed the local-mode stored-vector gate from scope constraints
(user decision), fixed the startup-recovery target to
`runner_init/orchestration.py`, added migration 348 and dual-schema mechanics,
corrected planner concurrency (3 → 1) and the unenforced 1,200-second overall
generation deadline, extended coalescing to all `DreamRunOptions` fields,
expanded the config removal list with deprecated fields, preserved the dry-run
snapshot contract, and recorded the current CLI/MCP trigger behavior as live
bugs rather than working synchronous paths.
