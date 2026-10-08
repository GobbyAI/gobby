Plan artifact: `.gobby/plans/provider-usage-spend.md`

# Provider quota, a per-call usage ledger, and per-session and per-task spend

**Plan ID:** provider-usage-spend

## Overview
`kind: framing`

This plan specifies #23099 (Plan provider usage remaining and per-session/task
spend tracking). The research inputs are
`.gobby/plans/research/usage-tracking-context-23099.md` (working notes, verified
facts, and Orchestrator rulings) and
`.gobby/plans/research/usage-monitor-2026-09.md` (the Codex credit-burn
incident and the per-CLI usage sources).

**The problem.**
- Gobby cannot tell an operator how much provider quota remains. Codex writes
  `rate_limits` (weekly window, reset time, credit balance) into every
  `token_count` rollout line, and Gobby ignores it. The 09-22 incident drew
  16,391.66 credits over 50.9 hours at 100% of the weekly window with nothing
  firing.
- `token_events` is the only usage ledger, and it is not a per-call ledger:
  - Codex repeats a cumulative `token_count` without a new call. 19 of 1331
    rows in one rollout were repeats, and summing them overcounts.
  - A Claude server-tool turn holds several model calls in one message
    (`usage.iterations`), so rows are not calls.
  - Claude subagent transcripts (`<transcript>/subagents/agent-*.jsonl`) never
    reach the ledger, so Claude session totals undercount the process.
- Provider-reported spend is on disk and unused: Claude `cost-state` records,
  Grok `costUsdTicks`, and Droid `factoryCredits`.
- Nothing attributes usage to a task. `session_tasks` keeps only a session's
  first claim, and `tasks.claimed_by_session_id` keeps only the current holder.
- `get_session_messages` pages rendered message groups. Agents read it as if
  it were a call ledger, and it cannot answer per-call questions.

**When the leaves close:**
- `token_events` is the per-call ledger. Every row has a stable identity,
  Codex repeats collapse, and `api_calls` counts model calls where the provider
  says so (2.1). Claude subagent calls are in the parent session's ledger
  (2.2). Provider-reported run totals (cost by unit, API time, wall time) live
  in `session_reported_usage` (2.3).
- A trigger records task claim intervals (1.1), so usage can be attributed to
  the task whose claim was latest when each call happened.
- `gobby-metrics:get_usage_ledger`, `GET /api/admin/usage/ledger`, and
  `gobby tokens ledger` serve per-session and per-task totals with keyset
  paging. Totals are computed over the whole scope on every request, so cursor
  and limit never change them; paging is a live traversal (Decision 10).
  Spend that no provider record covers reads as unknown, never as zero
  (3.1, 3.2).
- All six providers have an observation path. Verified structured/reporter
  quota feeds `ProviderCapacityService`; fresh pane-text matches send an
  explicitly uncertain "possible quota limit" alert through the same operator
  sink. Pane text never establishes exhausted/recovered capacity or fail-fast.
  Unknown numeric quota stays unknown.
  Live transcripts, AGY reporting and current spawned/interactive/placed
  terminal scans have explicit collection owners (4.1-4.5).
- The observability guide separates three surfaces: account quota, context
  occupancy, and spend (5.1).

## Decision Record
`kind: framing`

**2026-10-06 P4 amendment (confirmed by LM7 for #23715):**
Items 1-3 were amended by Orchestrator gobby#14972's 21:58 CT ruling,
forwarded by LM7 on 2026-10-06:
1. All six providers are covered. Verified structured/reporter signals drive
   quota state; text-only pane matches drive only a "possible quota limit"
   operator alert. Unknown numeric/reset fields stay unknown.
2. Existing spawned and interactive/placed owners observe promptly independent
   of idle/coordination/attention guards. Only fresh verified evidence tied to
   the current spawned source can cause the existing provider_quota_exhausted
   fail-fast; pane text alone cannot terminate a run.
3. Both confidence paths use one bounded CommunicationsManager sink. Verified
   warning/exhausted/reached/reset retain the edge design; pane hints have a
   separate deduped possible-limit edge and cannot clear or refresh quota state.
   Recovery needs newer positive quota evidence for the exact scope/limit.
Items 4-6 remain: no statusline/credentials/undocumented endpoints/price table/
spawn-gate/fallback expansion; separately closeable lifecycle outcomes and
preserved completed bodies; Orchestrator-owned P1 leaf transition after approval,
with canonical writes gated by Merge Manager CLEAR and LM7 GO.

The six-provider inventory and P4 below supersede Decision 2's absence of all
Claude signals and Decision 11's Codex-only collection scope. Claude statusline
ingestion remains excluded. Verified provider error envelopes and actually emitted
structured quota events are authoritative; pane strings are uncertain hints.
Numeric remaining/reset is never inferred
from spent tokens, credits, generic 429, overload, authentication or context-full.
All six providers share the existing observation owners and one operator sink;
interactive/placed and spawned owners observe independently of idle eligibility.
No quota endpoint, credential read, spawn gate or fallback is added. Missing
recovery evidence remains unknown/stale. P4 leaves are P1; the Orchestrator owns
rescoping/re-expansion of #23603 and #23604 after approval. Canonical writes wait
Merge Manager CLEAR and LM7 GO. The historical rulings below remain recorded;
this amendment governs the affected P4 scope.

Orchestrator rulings (gobby#14972, 2026-10-05, about 14:50 CT):

1. **Cost is provider-reported only, tagged by unit.** Claude `cost-state` USD,
   Grok `costUsdTicks` converted to USD, and Droid `factoryCredits` are stored
   as reported. Gobby keeps no price table and does not depend on ccusage.
   Codex reports only an account credit balance, which belongs to quota (4.1)
   and is never apportioned to sessions.
2. **Claude quota is `unknown` with reason "no local source".** The only local
   Claude quota signal is the statusline stdin (or `rate_limit_event` in print
   mode). #19319 (Remove statusline usage ingestion) made the transcript the
   sole usage authority, and this plan does not reverse it. A ghook statusline
   quota forward is listed under Not In Scope as a Josh decision.
3. **Alerts are in scope, delivered through the operator communications path.**
   They never use `send_message` fanout, which would put quota noise in every
   worker's context and stack wakes (Josh's #23125 concern). The sink is
   `CommunicationsManager.send_message(channel_name, content)`, which routes to
   the channel's `default_destination`
   (`src/gobby/communications/outbound.py::OutboundCommunications.send_message`;
   `docs/guides/telegram.md` sets the Chat ID). The spawn gate stays with
   #22075 (Detect provider usage outages across all providers and fall back to
   the next candidate).
4. **Surfaces are MCP, HTTP, and CLI.** Web is out of scope.
5. **Task attribution uses claim intervals; the latest claim wins; no
   backfill.** Restraint rung 2 (derive intervals from existing events) was
   tried first and does not suffice:
   - `session_tasks` has a UNIQUE (session_id, task_id, action) key
     (`baseline.sql:4576`), so a re-claim is dropped and no release is recorded.
   - `src/gobby/storage/tasks/_automation.py` clears `claimed_by_session_id`
     without writing a `session_tasks` row.
   - `task_lifecycle_events` has no session column.
   - About 50 source files write `claimed_by_session_id`.
   A trigger on `tasks` is therefore the single interception point.

Writer decisions:

6. **Ledger identity.** `token_events` keeps its unique index on
   (session_id, message_id). Each parser supplies a stable message id:
   - Claude uses the API `message.id`, which already collapses content-block
     lines.
   - Codex uses `<session>:codex:<total>:<input>:<cached>:<output>` built from
     `info.total_token_usage`, so a repeated cumulative line gets the same id
     and the index drops it. On the eight largest October rollouts, summing
     the deduplicated rows equals the final cumulative total exactly; without
     deduplication they overcount by 101k to 2.92M tokens per rollout. The one
     collision case, the same four-tuple after a counter decrease, was never
     observed.
   - Grok, Qwen, and Droid ids are already stable. AGY transcripts carry no
     usage, so AGY has no rows.
7. **`api_calls` is nullable and reported only where the provider states it.**
   - Claude: the number of `usage.iterations` entries of type `message`, else
     1. `advisor_message` iterations are excluded, matching Claude's own totals
     (memory d51df55b).
   - Codex: 1 per distinct cumulative increase.
   - Grok: `modelCalls` on `turn_completed`, or NULL when the provider sets
     `usageIsIncomplete` (3 of 856 local turns).
   - Droid, Qwen: NULL, because a row can span several calls.
8. **Provider-reported runs are rows of their own.** `cost-state` is
   cumulative per Claude process run, keyed by `startTime`, and monotonic; a
   resumed run starts again at zero. It must never become per-message deltas
   (memory a827e1da). Grok turns and the Droid session sidecar are runs too.
   One upsert rule (newest `observed_at` wins) covers all three.
9. **Claude subagent calls belong to the parent session's ledger** with
   `metadata.agent_id`. They never update the parent's context occupancy,
   because accounting is not occupancy (memory 37fcc94c).
10. **Totals come from SQL over the full scope on every call.** Rows page with
    an opaque keyset cursor over (event_at, session_id, message_key). Message
    ids survive a rebuild, so a cursor stays valid after
    `_persist_session_transcript` replaces the rows. Paging is a live
    traversal, not a frozen export. Cursor and limit never filter totals, so
    pages read from unchanged data carry identical totals. Usage ingested
    between two pages raises the totals of the later page and appears on it
    or a page after it. A row that lands before an already-issued cursor (a
    late subagent row with an earlier timestamp) counts in the totals but is
    returned only by a traversal restarted without a cursor. There is no
    snapshot table, export job, or cursor lease.
11. **Codex quota is pushed.** It is never polled. The live processor observes
    the newest `rate_limits` of a caught-up pass. Gobby never reads CLI
    credentials and never calls undocumented quota endpoints (`wham/usage`,
    `api/oauth/usage`).
12. **Claim intervals start with migration 460.** Nothing before it is
    reconstructed. The migration opens one interval, stamped at migration
    time, for each open task that has a holder. That records current state;
    it does not backfill history. Tasks in flight at deploy are therefore
    attributable from that moment on. `attribution_since` reports the earliest
    interval of the scope.

## Constraints
`kind: framing`

- **Isolation.** Every pytest run uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and never runs the full suite. Rust tests read
  `GOBBY_SCHEMA_TEST_DATABASE_URL` and skip without it. No leaf restarts the
  running daemon, touches port 60891, or reads `~/.gobby/local_cli_token` or
  `~/.gobby/bootstrap.yaml`.
- **Schema identity.** Migration 460 updates the full carrier set in 1.1,
  following commit `b5a436f0e3`. The carriers are:
  - the migration file, `catalog.manifest.json`, and
    `crates/gcore/src/schema/assets.rs::MIGRATIONS`;
  - `crates/gcore/src/grant/bundle.rs` (`GOLDEN_LATEST_CHECKSUM` and
    `expected_schema_identity`);
  - the two identity contract tests,
    `src/gobby/storage/schema_expected_identity.json`, and
    `tests/contracts/http/runtime_handshake.json`;
  - the five `tests/runtime_grants/golden/*.json` vectors.
  `baseline.sql` is not regenerated. The parked `gobby-messaging` plan also
  names migrations 460 to 462, so whichever epic lands second renumbers and
  regenerates its carriers.
- **Config carriers.** 4.2 adds
  `communications.operator_alert_channel`, so it regenerates
  `crates/gcore/assets/config/runtime_config_contract.json` and
  `tests/contracts/http/config_schema.json`.
- **Reference audit.** `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`
  fails on a public MCP tool or CLI command missing from
  `docs/reference-audit/*.json`. The leaf that adds an operation adds its audit
  entry and the matching mention in its skill reference.
- **File sizes.**
  - `src/gobby/runner_init/services.py` is 847 lines. 4.3 adds exactly one line
    there (848), under the 850-line growth threshold.
  - `src/gobby/sessions/transcripts/codex.py` (825) gains at most 12 lines in
    2.1.
  - `src/gobby/sessions/transcripts/claude.py` (823) is not edited.
  - New logic goes in new modules: `usage_calls.py`, `subagent_usage.py`,
    `reported_usage.py` (sessions and storage), `processor_ledger.py`,
    `usage_ledger.py`, `codex_rate_limits.py`, `quota_observations.py`,
    `quota_signals.py`, `processor_quota.py`, `provider_quota_monitor.py`
    and `quota_alerts.py`.
- **Privacy.**
  - Ledger rows and reported runs carry ids, timestamps, model names, counts,
    durations, and amounts only. They never carry message content, tool input,
    prompts, or `raw_json`.
  - Alert text carries provider, window, percent, reset time, and credit
    balance only.
- **No secrets in output.** No leaf logs a credential, token, or header value.
- **No backward compatibility.** 0.5.0 has not shipped. Pre-460 rows (Codex
  index ids, NULL `api_calls`, missing subagent rows, missing reported runs)
  are rewritten by the audit repair, with no aliasing.
- **Repair at activation.** After each daemon restart that activates a P2
  leaf or 3.1, the cutover runs `gobby tokens audit --all --fix` before
  DAEMON BACK is sent. The repair is a diff repair (2.1) that never deletes
  a row the live processor inserted after the audit's read, so it is safe
  while sessions are live, and running it again is safe. The run after the
  restart that activates 3.1, the first restart that exposes the ledger, is
  the completion gate. The ledger is authoritative once a following
  `gobby tokens audit --all` reports `stale=0` for every session and
  `missing=0` for every session that is not `active`. Missing rows on an
  `active` session are ingestion lag that the live processor fills. V2
  step 3 confirms the gate.
- **Consumer sweeps** ran read-only on `0.5.0` after `4858476f43` (2026-10-05):
  - `gcode grep -w` for `TokenEvent`, `_persist_session_transcript`,
    `ProviderCapacityRecord`, `ProcessorHost`, `SessionMessageProcessor`,
    `CommunicationsConfig`, and `init_servers`;
  - `gcode grep -F` for `rate_limits`, `cost-state`, `get_provider_capacity`,
    and `/api/admin/usage`.

## Requirement Mapping
`kind: framing`

| #23099 validation item | Here |
| --- | --- |
| 1. Authoritative quota sources and refresh rules for used, remaining, and reset times, including permissions and unavailable or rate-limited handling | Provider Coverage; 4.1; Decisions 2, 11 |
| 2. Normalized per-session and per-task tokens, cache, cost, and duration, with attribution and privacy boundaries | 1.1, 2.1, 2.2, 2.3, 3.1; Decisions 1, 5, 7, 8, 12; Constraints (Privacy) |
| 3. Distinct surfaces for account quota, context occupancy, and spend | 3.1, 3.2, 4.1, 5.1 |
| 4. Reuse analysis of #19323, #22075, #21700, #12000, and #7580 | Reuse |
| 5. Validation and staged slices | P1 to P5; V2 |
| 6. The `get_session_messages` per-call boundary: an authoritative interface with call identity, aggregation, pagination and dedupe, provider coverage, provenance and missing data, plus a fixture of two or more calls with distinct usage that proves paging and regrouping neither lose nor double-count | 2.1, 3.1 (acceptance 3.1.1 to 3.1.3); Decisions 6, 7, 10 |

## Reuse
`kind: framing`

| Task | What it left | Use here |
| --- | --- | --- |
| #12000 (Implement per-event token tracking end-to-end) | `token_events`, `TokenEventStore`, the live and rebuild writers, `gobby tokens audit` | Reused as the ledger. 2.1 makes its identity per call and adds `api_calls`. |
| #7580 (Add cost and token tracking display per task) | A web TaskDetail display that no longer exists, because web is Chat-only (memory ac77469d) | Replaced by task scope in the MCP, HTTP, and CLI ledger (3.1, 3.2). No web work (Decision 4). |
| #19323 (Evaluate Claude OpenTelemetry as optional usage telemetry) | Closed obsolete, with no recommendation | Not reused. The transcript stays the Claude usage authority. |
| #21700 (Detect provider usage-limit exhaustion in agent output and fail runs fast instead of reporting them as stuck) | Output-based fail-fast for exhausted runs | Reuse bounded recovery for verified current-source terminal quota errors. Pane-text fail-fast is replaced by possible-limit hints under the 21:58 ruling; quota observation 4.1 and notification 4.2 retain confidence boundaries. |
| #22075 (Detect provider usage outages across all providers and fall back to the next candidate) | Escalated, needs-decision; owns spawn fallback and gating | Owns any spawn gate. It can read provider state from `get_provider_capacity` once 4.1 lands. |
| #19364 (Add AGY usage-capacity reporting when CLI becomes compatible) | `AgyUsageReporter` and `ProviderCapacityService` | Reused. 4.1 adds observed providers beside the AGY reporter. |
| #19319 (Remove statusline usage ingestion) | The transcript is the sole Claude usage authority | Respected (Decision 2). |

## Provider Coverage
`kind: framing`

| Source | Ledger rows | `api_calls` | Reported spend | Quota |
| --- | --- | --- | --- | --- |
| Claude | One per API `message.id`, main and subagent transcripts | `usage.iterations` of type `message`, else 1 | `cost-state` USD per process run, with API and wall time | Verified structured events only when actually emitted with native fixture/time provenance; fresh pane matches give possible-limit hints only; no statusline; otherwise unknown quota |
| Codex | One per increase of `total_token_usage` | 1 | None. Codex reports an account credit balance only, shown in quota details. | Verified live `rate_limits` windows/flags and typed terminal quota errors; fresh pane matches give hints only |
| Grok | One per `turn_completed` | `modelCalls`; NULL when `usageIsIncomplete` | `costUsdTicks` / 1e10 USD per turn, with API time | Fresh pane matches give possible-limit hints only; no verified structured/numeric quota feed, so quota remains unknown |
| Droid | One per parse-pass delta | NULL | `factoryCredits` per session (cumulative sidecar), with active time | Fresh pane matches give possible-limit hints only; no verified structured/numeric quota feed, so quota remains unknown |
| Qwen | One per usage record | NULL | None | Fresh pane matches give possible-limit hints only; no verified structured/numeric quota feed, so quota remains unknown |
| AGY | None (no usage in AGY transcripts) | None | None | Verified `agy -p /usage` reporter (#19364); fresh pane matches give hints only; ERROR fixture format does not create a new live structured source |

The ledger reports each row of this table as a coverage descriptor (3.1), so a
reader can see what is exact, what is reported, and what is missing, with a
reason.

## Not In Scope
`kind: framing`

- **Josh decision, unscoped:** forwarding Claude statusline `rate_limits`
  through ghook for quota. It is a crate change and touches the #19319
  decision.
- Web surfaces (Decision 4).
- Any spawn gate or provider fallback (#22075).
- ccusage or any price table (Decision 1).
- Calling provider quota endpoints or reading CLI credentials (Decision 11).

## P1: Claim intervals and ledger columns in the schema
`kind: framing`

**Goal**: the schema can record per-call counts, provider-reported runs, task
claim intervals, and quota details.

### 1.1 Usage ledger schema and the claim-interval trigger [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/460_usage_ledger.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog entries for migration 460
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/src/grant/bundle.rs::GOLDEN_LATEST_CHECKSUM`
- `crates/gcore/src/grant/bundle.rs::expected_schema_identity`
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: packaged expected identity at version 460
- `tests/contracts/http/runtime_handshake.json::*` — scope-reason: handshake records the schema identity
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/storage/test_schema_usage_ledger.py`

**Research context:**
- `token_events` (`baseline.sql:3970`):
  - Columns: `id` (identity), `session_id`, `project_id`, `message_id`
    (nullable), `source`, `origin`, `model`, `model_family`, four token
    counts, `context_window`, `event_at`, `created_at`, and `metadata`.
  - Indexes: unique `idx_token_events_dedup` on (session_id, message_id)
    WHERE message_id IS NOT NULL, and `idx_token_events_session` on
    (session_id, event_at).
- `provider_capacity_snapshots` (`baseline.sql:3085`):
  - Keyed by (machine_id, provider).
  - `state` CHECK allows only `available` and `exhausted`; `windows` is a
    jsonb array.
- `tasks` (`baseline.sql:3869`):
  - `id uuid`, `claimed_by_session_id uuid` (FK to sessions, `ON DELETE
    RESTRICT DEFERRABLE`), and `closed_at`.
  - A closed task can keep its `claimed_by_session_id`.
- Identity columns need a sequence grant to the runtime role
  (`GRANT ALL ON SEQUENCE token_events_id_seq TO gobby_daemon_runtime`,
  `baseline.sql:6502`). A trigger insert runs as the invoking role, so the new
  interval table uses a `uuid` key with `gen_random_uuid()` and needs no
  sequence grant.

**Implementation:** migration `460_usage_ledger.sql` is additive. New tables
grant `SELECT, INSERT, UPDATE, DELETE` to `gobby_daemon_runtime`, and the new
function grants `EXECUTE` to the same role.

- `ALTER TABLE token_events ADD COLUMN api_calls integer CHECK (api_calls >= 0)`.
  It is nullable, and NULL means the provider does not state a call count.
- `session_reported_usage`:
  - `session_id uuid NOT NULL` (FK to sessions `ON DELETE CASCADE`),
    `run_key text NOT NULL`, `source text NOT NULL`;
  - `cost_unit text CHECK (cost_unit IN ('usd', 'factory_credits'))` and
    `cost_amount numeric`, both nullable, and a CHECK that they are both set
    or both NULL;
  - `cost_complete boolean NOT NULL DEFAULT true`;
  - `api_duration_ms bigint` and `wall_duration_ms bigint`;
  - `started_at timestamptz NOT NULL`, `observed_at timestamptz NOT NULL`,
    and a CHECK that `observed_at >= started_at`;
  - `model_usage jsonb NOT NULL DEFAULT '{}'`;
  - `PRIMARY KEY (session_id, run_key)`.
- `task_claim_intervals`:
  - `id uuid PRIMARY KEY DEFAULT gen_random_uuid()`;
  - `task_id uuid NOT NULL` (FK to tasks `ON DELETE CASCADE`) and
    `session_id uuid NOT NULL` (FK to sessions `ON DELETE CASCADE`);
  - `claimed_at timestamptz NOT NULL`, `released_at timestamptz`, and a CHECK
    that `released_at >= claimed_at`.
  - Indexes: a partial unique index on (task_id) WHERE released_at IS NULL,
    and an index on (session_id, claimed_at).
- `sync_task_claim_interval(task uuid)` reconciles one task:
  1. Its holder is `claimed_by_session_id` when `closed_at IS NULL`, else
     NULL.
  2. If an open interval exists for that holder, it returns.
  3. Otherwise it closes any open interval for the task with
     `released_at = greatest(clock_timestamp(), claimed_at)`, then opens a new
     interval at `clock_timestamp()` when the holder is not NULL.
  Calling it twice is a no-op.
- The `tasks_claim_interval` trigger runs `AFTER INSERT OR UPDATE OF
  claimed_by_session_id, closed_at ON tasks FOR EACH ROW` and calls
  `sync_task_claim_interval(NEW.id)`.
  - Interval edges use the database clock. Token events carry
    transcript-stamped `event_at`. Skew between the two moves a call at an
    interval edge by at most the clock difference. That is accepted and
    documented in 5.1.
- The migration seeds current state, not history:
  `SELECT sync_task_claim_interval(id) FROM tasks WHERE closed_at IS NULL AND claimed_by_session_id IS NOT NULL`.
- `ALTER TABLE provider_capacity_snapshots ADD COLUMN details jsonb NOT NULL DEFAULT '{}'`.
- Regenerate the carriers as the Constraints list them.

**Granularity:** one leaf. Migration 460 is one schema unit: three additive
column or table changes and one trigger. Together with the derived carriers it
is one identity bump. The five acceptance items each test one invariant of that
unit.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_schema_usage_ledger.py -q`,
then `cargo nextest run -p gobby-core --test schema_contract` and
`cargo nextest run -p gobby-daemon --test cli_contract` (heavy work).

**Acceptance:**

- 1.1.1 - Claiming a task, re-claiming it from another session, and clearing `claimed_by_session_id` produce, in order, an interval for the first session, then one for the second, then no open interval. Each release time is greater than or equal to its claim time. test: `tests/storage/test_schema_usage_ledger.py::test_trigger_tracks_holder_changes`.
- 1.1.2 - Closing a task that keeps `claimed_by_session_id` closes its open interval. Reopening it opens a new one. A second open interval for one task is rejected by the partial unique index. test: `tests/storage/test_schema_usage_ledger.py::test_close_and_reopen_follow_holder`.
- 1.1.3 - `sync_task_claim_interval` opens an interval for an open, held task that has none (the migration seed), and a second call is a no-op. test: `tests/storage/test_schema_usage_ledger.py::test_sync_seeds_current_holder_idempotently`.
- 1.1.4 - `token_events.api_calls` rejects a negative value. `session_reported_usage` rejects an unknown `cost_unit`, a unit without an amount, and `observed_at` before `started_at`. Its rows and intervals cascade when their session is deleted. test: `tests/storage/test_schema_usage_ledger.py::test_ledger_columns_constrain_values`.
- 1.1.5 - `provider_capacity_snapshots.details` defaults to `{}`, and the packaged schema identity reports version 460. test: `tests/storage/test_schema_usage_ledger.py::test_capacity_details_default_and_identity`.

## P2: The ledger records calls, subagents, and reported runs
`kind: framing`

**Goal**: every provider call that a transcript records is one keyed ledger row,
and provider-reported run totals sit beside it.

### 2.1 Per-call identity and `api_calls` [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/transcripts/codex.py::CodexTranscriptParser._parse_event_msg`
- `src/gobby/sessions/usage_calls.py`
- `src/gobby/storage/token_events.py::TokenEvent`
- `src/gobby/storage/token_events.py::_TOKEN_EVENT_COLUMNS`
- `src/gobby/storage/token_events.py::_record_params`
- `src/gobby/storage/token_events.py::TokenEventStore.list_session_events`
- `src/gobby/storage/token_events.py::TokenEventStore._row_to_event_dict`
- `src/gobby/sessions/processor_usage.py::ProcessorUsageMixin._persist_usage_events`
- `src/gobby/sessions/transcript_processing.py::TranscriptProcessingMixin._persist_session_transcript`
- `src/gobby/cli/tokens.py::*` — scope-reason: the audit's event conversion, drift check, and fix path all change
- `tests/sessions/test_usage_call_identity.py`

**Research context:**
- `CodexTranscriptParser._parse_event_msg` (codex.py:625) reads
  `info.last_token_usage`, else `info.total_token_usage`. It sets
  `message_id=self._message_id_for(index, payload.get("message_id") or payload.get("id"))`.
  `_message_id_for` returns a non-empty string id unchanged, else
  `<session>:codex:<index>`.
- `ClaudeTranscriptParser._usage_payload(data)` (claude.py:699) is a static
  helper that returns the message usage dict.
- Grok usage is only on `turn_completed` updates: 527 usage-bearing updates in
  125 local files, all of type `turn_completed`. Its `usage` keys include
  `modelCalls`, `apiDurationMs`, `costUsdTicks`, and `usageIsIncomplete`.
- The live writer (`_persist_usage_events`) and the rebuild writer
  (`_persist_session_transcript`) both build `TokenEvent` and insert through
  `record_batch` (ON CONFLICT DO NOTHING). Both write origin `transcript`.
  The rebuild deletes the `backfill` and `transcript` rows first.
- `cli/tokens.py::_messages_to_events` sums parsed usage before
  `store.record` deduplicates, so a Codex session would show drift forever.

**Implementation:**
- Codex: when `info.total_token_usage` is a dict and the payload has no id,
  pass `f"{self.session_id or 'codex'}:codex:{total}:{input}:{cached}:{output}"`
  as the raw id. The fields are `total_tokens`, `input_tokens`,
  `cached_input_tokens`, and `output_tokens` of `total_token_usage`. Token
  counts still come from `last_token_usage`.
- New `src/gobby/sessions/usage_calls.py::api_call_count(source, message) -> int | None`:
  - `claude`: count the entries of `usage.iterations` whose `type` is
    `message`, using `ClaudeTranscriptParser._usage_payload(message.raw_json)`.
    Return at least 1. Return 1 when there are no iterations.
  - `codex`: 1 for `content_type == "usage"`.
  - `grok`: `params.update.usage.modelCalls` for `content_type ==
    "turn_completed"`. Return None when `usageIsIncomplete` is true or the
    value is missing.
  - Any other source: None.
- `TokenEvent` gains `api_calls: int | None = None`. `_TOKEN_EVENT_COLUMNS`,
  `_record_params`, and the `list_session_events` SELECT carry it.
  `_row_to_event_dict` reads it through `_row_value(row, "api_calls")`, so a
  row mapping without the key still converts.
- Both writers set `api_calls=api_call_count(source, message)`.
- Audit (`gobby tokens audit`):
  - `_messages_to_events` keeps the first event for each `message_id` before
    totals are compared.
  - Drift compares rows as well as totals, because equal token totals do
    not mean an equal ledger. The audit must tolerate the live processor
    appending while it runs, so read order matters:
    1. It reads the stored rows first (set S), every row of the session in
       one direct query because `list_session_events` is limited, with each
       row's `id`.
    2. It then derives rows from the transcript (set D). Every derived row
       carries a message id, since the parsers assign fallback ids.
    The processor inserts a row only after reading its line from the file,
    and the file only grows, so every row in S has its line already present
    when D is read.
  - Rows are matched by `message_id` and compared on
    `(api_calls, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens)`.
    The audit reports two classes per session:
    - `stale`: a row of S that is absent from D or differs from its match,
      or a row with NULL `message_id`. Equal totals therefore no longer hide
      an obsolete Codex index id or a NULL `api_calls` on a pre-460 Claude
      or Grok row. A stale row is never live lag.
    - `missing`: a row of D absent from S, or `sessions.usage_*` differing
      from the stored totals. On a session that is still `active` this
      includes lines the processor has not ingested yet.
    Each audit is a snapshot bounded by its transcript read. A line appended
    after that read is outside the pass, and the next audit reports it
    `missing` if it is still not ingested.
  - `--fix` is a diff repair. The old delete-all-and-reinsert is removed,
    because it deleted rows the processor inserted after the transcript
    read. One transaction per session:
    1. deletes, by `id`, only the rows of S whose `message_id` is NULL or
       absent from D;
    2. upserts D with
       `ON CONFLICT (session_id, message_id) WHERE message_id IS NOT NULL DO UPDATE`
       of the compared columns, `model`, and `event_at`. A stale row whose
       key is in D, such as a pre-460 Claude row with NULL `api_calls`, is
       corrected in place and keeps its `id`;
    3. upserts the reported runs (2.3);
    4. rewrites `sessions.usage_*` from `get_session_totals`.
    A row the processor inserts after step 1 of the read is not in S, so it
    is never deleted, and the processor's own later `update_usage` also
    reads `get_session_totals`.
  - The summary prints `audited`, `stale`, and `missing` session counts,
    and with `--fix`, `repaired`.

Consumers unchanged:
- `src/gobby/servers/routes/admin/_testing.py` — no-edit-reason: builds `TokenEvent` by keyword without `api_calls`, which defaults to None.
- `tests/sessions/test_sessions_processor_unit.py` — no-edit-reason: builds `TokenEvent` fixtures by keyword without `api_calls`.
- `tests/sessions/test_token_usage.py` — no-edit-reason: builds `TokenEvent` fixtures and reads listed events by token keys; the added key is ignored.
- `tests/storage/test_token_events.py` — no-edit-reason: its row mappings lack `api_calls`, which `_row_value` defaults to None, and its assertions read named keys.
- `src/gobby/servers/routes/sessions/core.py` — no-edit-reason: returns `list_session_events` dicts as they are, so `api_calls` passes through.
- `src/gobby/sessions/context_usage.py` — no-edit-reason: reads token and window fields of listed events only.

**Granularity:** one leaf. Call identity and call counts are one contract
(what a ledger row is). The parser id, the counter, the column plumbing, and
the audit must change together, or the audit reports false drift on every Codex
session.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_usage_call_identity.py tests/storage/test_token_events.py -q`.

**Acceptance:**

- 2.1.1 - A Codex rollout fixture with three `token_count` lines, one of which repeats the previous cumulative total, yields two ledger rows whose ids are the cumulative key. Their summed tokens equal the final `total_token_usage` (input minus cached, cached, and output). test: `tests/sessions/test_usage_call_identity.py::test_codex_cumulative_id_dedupes_repeats`.
- 2.1.2 - A Codex `token_count` without `total_token_usage` keeps the index id. test: `tests/sessions/test_usage_call_identity.py::test_codex_without_cumulative_keeps_index_id`.
- 2.1.3 - `api_call_count` returns 2 for a Claude message with two `message` iterations and one `advisor_message`, 1 without iterations, `modelCalls` for a Grok turn, None for a Grok turn with `usageIsIncomplete`, and None for Droid and Qwen. test: `tests/sessions/test_usage_call_identity.py::test_api_call_count_per_source`.
- 2.1.4 - The live writer and the rebuild writer both persist `api_calls`, and `list_session_events` returns it. test: `tests/sessions/test_usage_call_identity.py::test_writers_persist_api_calls`.
- 2.1.5 - The audit deduplicates parsed events by `message_id`, so a Codex session with repeated totals shows no drift. A stored row with NULL `message_id` is drift, and `--fix` replaces it with keyed rows. test: `tests/sessions/test_usage_call_identity.py::test_audit_dedupes_and_flags_unkeyed_rows`.
- 2.1.6 - Stored rows whose token totals equal the transcript's still drift when they keep a pre-460 identity. This covers a Claude row and a Grok row with valid ids and NULL `api_calls`, and a Codex rollout without repeated totals whose rows keep index ids. They are reported `stale`. `--fix` rewrites them to keyed rows with `api_calls`, and a second audit reports `stale=0` and `missing=0`. test: `tests/sessions/test_usage_call_identity.py::test_audit_flags_equal_total_identity_drift`.
- 2.1.7 - `--fix` is safe against live ingestion. In a fixture that interleaves the audit with processor inserts, a row inserted after the audit's stored-row read survives `--fix`, and `sessions.usage_*` afterwards equals `get_session_totals` including it. A line appended after the stored-row read but before the transcript read is reported `missing`, never `stale`, and nothing deletes it. A line appended after the transcript read is outside that pass: that audit does not report it, and a following audit reports it `missing` while it is still not ingested. Only pre-read rows that are unkeyed or absent from the derived set are deleted. A pre-read row with the same key but NULL `api_calls` is corrected in place to `api_calls` 1, keeps its `id`, and is still present after `--fix`. test: `tests/sessions/test_usage_call_identity.py::test_fix_never_deletes_rows_ingested_during_audit`.

### 2.2 Claude subagent calls enter the parent ledger [category: code] (depends: 2.1, 4.3)
`kind: deliverable`

Targets:
- `src/gobby/sessions/subagent_usage.py`
- `src/gobby/sessions/processor_ledger.py`
- `src/gobby/sessions/processor.py::SessionMessageProcessor`
- `src/gobby/sessions/processor.py::SessionMessageProcessor.__init__`
- `src/gobby/sessions/processor_types.py::ProcessorHost`
- `src/gobby/sessions/processor_transcripts.py::ProcessorTranscriptMixin._process_session_unlocked`
- `src/gobby/sessions/processor_lifecycle.py::ProcessorLifecycleMixin.unregister_session`
- `src/gobby/sessions/processor_lifecycle.py::ProcessorLifecycleMixin._reset_transcript_state`
- `src/gobby/sessions/transcript_processing.py::TranscriptProcessingMixin._persist_session_transcript`
- `src/gobby/cli/tokens.py::*` — scope-reason: the audit reads subagent transcripts into the compared and fixed events
- `tests/sessions/test_claude_subagent_usage.py`

**Research context:**
- `src/gobby/sessions/transcript_paths.py::find_supplemental_transcripts_on_disk("claude", path)`
  returns `<transcript>/subagents/agent-*.jsonl`. Only
  `tasks/transcript_evidence.py` uses it today.
- In 7 days of local data, 19 of 69 Claude transcripts had subagent files
  (115 files in total). Async Task tool results carry no usage.
- `_process_session_unlocked` reads new complete lines from a per-session
  byte offset (`self._byte_offsets`) and calls `_process_parsed_batch` with
  `publish_occupancy=caught_up`. When the parent has no new lines it returns
  early (`if not new_lines: return True`, processor_transcripts.py:244-245),
  before it looks up the parser. When the parsed lines yield no stats
  records, the `if not stats_records` branch advances the index and the byte
  offset and returns without `_process_parsed_batch`.
- `ProcessorLifecycleMixin._loop` calls `_process_all_sessions` every
  `poll_interval`, which runs `_process_session` for every registered
  session. An idle parent therefore still gets a pass on every poll.
- The rebuild builds an occupancy snapshot for each event and replays the
  snapshots in Pass 3 (transcript_processing.py:507-546).
  `_persist_usage_events` publishes tail occupancy.
- `SessionMessageProcessor` combines `ProcessorLifecycleMixin`,
  `ProcessorStatsMixin`, `ProcessorUsageMixin`, and
  `ProcessorTranscriptMixin`. `ProcessorHost` (processor_types.py) is the
  Protocol the mixins type against.
- `session_manager.update_usage` keeps `context_window` and `model` when they
  are passed as None (`COALESCE`, `storage/sessions/_usage.py`).

**Implementation:**
- New `src/gobby/sessions/subagent_usage.py` with a frozen `SubagentCursor`
  dataclass (`offset`, `next_index`, `dev`, `ino`, `mtime_ns`,
  `parser_state`) and
  `read_subagent_events(session_id, project_id, transcript_path, cursors) -> tuple[list[TokenEvent], dict[str, SubagentCursor]]`.
  Byte progress and event identity share one coordinate: a message's
  fallback id comes from its message index counted from the start of its
  file, the same index a full read assigns.
  1. List the files with `find_supplemental_transcripts_on_disk("claude", transcript_path)`.
     The agent id is the file stem after `agent-`.
  2. Stat each file. A file with no cursor, or whose cursor no longer
     matches, starts at offset 0 and index 0 with empty parser state. A
     cursor no longer matches when the size is below `offset`, `(dev, ino)`
     differs, or `mtime_ns` decreased. This is the rule
     `_process_session_unlocked` applies to the parent.
  3. Read the complete lines from the cursor's offset. Build the parser with
     `get_parser("claude", session_id=f"{session_id}:{agent_id}", transcript_path=file)`,
     so fallback ids are scoped to the agent, and hydrate it from
     `parser_state`. Parse with
     `processor_transcripts._parse_incremental_records(parser, lines, start_index=next_index)`.
  4. Emit one `TokenEvent` for each message with token usage: source
     `claude`, origin `transcript`, `api_calls` from `api_call_count`, and
     `metadata={"content_type": …, "agent_id": agent_id}`.
  5. Return the events and each file's new cursor. The new `offset` is the
     end of the last complete line, and `next_index` is the last parsed
     message index plus 1, or unchanged when none parsed. `parser_state` is
     `parser.snapshot_state()`, and the stat fields are the file's current
     ones.
- New `src/gobby/sessions/processor_ledger.py::ProcessorLedgerMixin` with
  `async _persist_ledger_batch(session_id, transcript_path, source, lines, records, caught_up)`.
  For Claude it runs `read_subagent_events` with
  `self._subagent_cursors[session_id]` and inserts through `record_batch`.
  When rows were inserted, it refreshes `sessions.usage_*` from
  `get_session_totals` through `session_manager.update_usage` with
  `context_window=None` and `model=None`. It stores the new cursors only after
  the insert succeeds.
- `SessionMessageProcessor` adds `ProcessorLedgerMixin` to its bases, and
  `__init__` sets
  `self._subagent_cursors: dict[str, dict[str, SubagentCursor]] = {}`.
  `ProcessorHost` declares the attribute and the method.
- Every exit of `_process_session_unlocked` that consumes input awaits
  `_persist_ledger_batch` exactly once, before it advances `_byte_offsets`.
  Every consumed line therefore reaches its collector. There are three such
  exits:
  1. The parsed-batch exit awaits it after `_process_parsed_batch` succeeds,
     with the pass's lines and parsed records.
  2. The `if not stats_records` exit advances the offset over lines that
     carry no messages, for example a Claude append that holds only a
     `cost-state` record, which the parser filters. It awaits the ledger
     first, with the pass's lines and parsed records.
  3. The no-new-lines exit resolves the session's parser from
     `self._parsers` and, when one exists, awaits
     `_persist_ledger_batch(session_id, transcript_path, _parser_source(parser), [], [], caught_up)`.
     A subagent append or a Droid sidecar change is then ingested on the
     next poll while the parent transcript is idle. The existing poll loop
     drives that pass, so no watcher, polling service, or event bus is added.
- When `_persist_ledger_batch` raises on exits 1 or 2, the parser state is
  restored from `parser_state` (as `_process_parsed_batch` failures already
  are) and the error propagates. The offset stays where it was, and the next
  pass rereads the lines. The reread is idempotent: message ids are unique,
  `sessions.usage_*` is rewritten from `get_session_totals`, and the
  reported-run upsert is strict.
- `unregister_session` and `_reset_transcript_state` drop the session's
  subagent cursors. A daemon restart reads each subagent file from offset 0
  and index 0, assigning the same ids as the live passes did, so the reread
  inserts nothing new.
- The rebuild and the audit read every subagent file in full (empty
  cursors), so they assign the same ids as the live path.
  The rebuild appends the subagent events to the `record_batch` call without
  snapshot entries.
- Subagent events never reach `_snapshot_from_token_usage`,
  `_publish_tail_occupancy`, or the Pass 3 replay. `sessions.usage_*` and the
  agent-run usage served by `src/gobby/servers/routes/agents.py` now include
  subagent tokens. That is the intended accounting change.

Consumers unchanged:
- `src/gobby/sessions/liveness_monitor.py` — no-edit-reason: constructs or reads the processor through unchanged arguments; the mixin adds no constructor parameter.
- `src/gobby/runner.py` — no-edit-reason: types and holds the processor and calls `init_servers` with unchanged signatures.
- `src/gobby/sessions/processor_stats.py` — no-edit-reason: types against `ProcessorHost`, whose new members are additive.
- `tests/sessions/test_transcript_index_journal.py` — no-edit-reason: types against `ProcessorHost`, whose new members are additive.
- `tests/hooks/test_session_coordinator.py` — no-edit-reason: calls `unregister_session` with its unchanged signature.
- `tests/provider_contracts/test_droid_live_captures.py` — no-edit-reason: constructs the processor with unchanged arguments.
- `tests/sessions/test_e2e_session_tracking.py` — no-edit-reason: constructs the processor with unchanged arguments.
- `tests/sessions/test_liveness_monitor.py` — no-edit-reason: constructs the processor with unchanged arguments.
- `tests/sessions/test_processor_catchup_occupancy.py` — no-edit-reason: constructs the processor with unchanged arguments; subagent rows never touch occupancy.
- `tests/sessions/test_processor_metadata_exclusion_integration.py` — no-edit-reason: constructs the processor with unchanged arguments.
- `tests/sessions/test_processor_renderer.py` — no-edit-reason: constructs the processor with unchanged arguments.
- `tests/sessions/test_sessions_processor_integration.py` — no-edit-reason: constructs the processor with unchanged arguments.
- `tests/sessions/test_sessions_processor_unit.py` — no-edit-reason: constructs the processor with unchanged arguments.
- `tests/sessions/test_transcript_index.py` — no-edit-reason: constructs the processor with unchanged arguments.
- `tests/sessions/transcripts/test_droid_parser.py` — no-edit-reason: constructs the processor with unchanged arguments.
- `tests/workflows/test_observer_context_usage.py` — no-edit-reason: constructs the processor with unchanged arguments.

**Granularity:** one leaf. Subagent ingestion is one behavior (a parent session
owns its subagents' calls), with three entry points that must agree: live,
rebuild, and audit. The shared reader keeps them identical.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_claude_subagent_usage.py tests/sessions/test_processor_usage.py -q`.

**Acceptance:**

- 2.2.1 - A parent transcript with one subagent file that holds two API calls yields two parent-session rows tagged with `agent_id`, through both the live path and the rebuild. `sessions.usage_*` includes them. test: `tests/sessions/test_claude_subagent_usage.py::test_subagent_calls_join_parent_ledger`.
- 2.2.2 - Lines appended to the subagent file between two live passes are read once. Rereading from offset 0 after a simulated restart inserts nothing new. test: `tests/sessions/test_claude_subagent_usage.py::test_live_offsets_read_each_line_once`.
- 2.2.6 - Two usage-bearing messages without an API id, appended to one subagent file in separate live passes, stay two rows with distinct ids. A simulated restart and the rebuild assign the same two ids and the same totals. Truncating the file below its cursor, or replacing it with a new inode, resets that file to offset 0 and index 0, so the next pass reads the new content and the cursor is never stranded. test: `tests/sessions/test_claude_subagent_usage.py::test_subagent_identity_is_stable_across_passes`.
- 2.2.3 - The parent's context occupancy snapshot and published tail occupancy are unchanged by subagent rows. test: `tests/sessions/test_claude_subagent_usage.py::test_subagent_rows_never_touch_occupancy`.
- 2.2.4 - Two subagents whose messages lack an API id get distinct agent-scoped fallback ids, and the audit includes subagent rows without reporting drift. test: `tests/sessions/test_claude_subagent_usage.py::test_fallback_ids_are_agent_scoped_and_audited`.
- 2.2.5 - With the parent transcript unchanged, two complete usage lines appended to a subagent file are ingested by the next normal live pass. The parent gains exactly two rows, `sessions.usage_*` includes them, and the parent's occupancy is unchanged. A further idle pass inserts nothing and leaves `sessions.usage_*` unchanged. test: `tests/sessions/test_claude_subagent_usage.py::test_idle_parent_pass_ingests_subagent_appends`.

### 2.3 Provider-reported run totals [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/sessions/reported_usage.py`
- `src/gobby/storage/reported_usage.py`
- `src/gobby/sessions/processor_ledger.py`
- `src/gobby/sessions/transcript_processing.py::TranscriptProcessingMixin._persist_session_transcript`
- `src/gobby/cli/tokens.py::*` — scope-reason: the audit and its fix path upsert reported runs
- `tests/sessions/test_reported_usage.py`

**Research context:**
- Claude `cost-state` records are top-level transcript lines. They carry
  `totalCostUSD`, `totalAPIDuration`, `totalDuration`, `startTime` (epoch
  milliseconds), `modelUsage`, and `hasUnknownModelCost`.
  - The parser skips them (claude.py:123).
  - They are cumulative per process run and monotonic.
  - 282 of 426 transcripts from 7 days have them; one transcript had up to 14.
  - `modelUsage` includes Haiku background calls that are absent from the
    assistant messages.
- A Grok `turn_completed` usage carries `costUsdTicks` (1e10 ticks = $1),
  `apiDurationMs`, `modelUsage`, and `usageIsIncomplete`.
- Droid writes a cumulative `tokenUsage.factoryCredits` and
  `assistantActiveTimeMs` to `<transcript>.settings.json`, read by
  `DroidTranscriptParser._load_sidecar` with
  `jsonl_path.with_suffix(".settings.json")`.

**Implementation:**
- New `src/gobby/sessions/reported_usage.py` with a frozen `ReportedRun`
  dataclass mirroring the table columns, and:
  - `claude_cost_runs(lines)`:
    - one run per `startTime`, keeping the record with the largest
      `observed_at` (`startTime + totalDuration`), the latest cumulative
      reading. The live path and the rebuild therefore pick the same record
      even when two records tie on cost, and the strict upsert orders on the
      same field;
    - run key `claude:<startTime>`, unit `usd`;
    - `api_duration_ms=totalAPIDuration`, `wall_duration_ms=totalDuration`;
    - `started_at=startTime` and `observed_at=startTime + totalDuration`;
    - `cost_complete = not hasUnknownModelCost`, and
      `model_usage=modelUsage`.
  - `grok_turn_runs(messages)`: one run per `turn_completed` message with
    usage.
    - Run key `grok:<message_id>`, `usd = costUsdTicks / 1e10`, and
      `api_duration_ms=apiDurationMs`.
    - `started_at = observed_at = message.timestamp`.
    - `cost_complete = not usageIsIncomplete`.
  - `droid_sidecar_run(transcript_path, started_at)`: one run with run key
    `droid:session`, unit `factory_credits`,
    `wall_duration_ms=assistantActiveTimeMs`, and `observed_at` set to the
    sidecar's mtime. It returns None when the sidecar or the field is absent.
  - `collect_runs(source, *, lines, messages, transcript_path, session_started_at)`
    dispatches on source and returns a list.
- New `src/gobby/storage/reported_usage.py::ReportedUsageStore` with:
  - `upsert_runs(session_id, runs)`:
    `ON CONFLICT (session_id, run_key) DO UPDATE … WHERE session_reported_usage.observed_at < excluded.observed_at`.
    A newer cumulative reading wins. A replay or an unchanged reading writes
    no row version, which matters because idle passes reread the Droid
    sidecar on every poll.
  - `list_runs(session_ids)`.
- `_persist_ledger_batch` calls `collect_runs` on each pass for the batch's
  lines and messages, and upserts the result. Droid reads the sidecar on every
  pass, including the no-new-lines pass that 2.2 adds, so a sidecar-only
  change refreshes the run while the transcript is idle. A Claude
  `cost-state` line reaches `claude_cost_runs` through whichever 2.2 exit
  consumes it, including the `if not stats_records` exit when the append
  holds no message.
- The rebuild calls `collect_runs` over the full transcript and upserts. The
  audit reads `list_runs` before the transcript, as 2.1 orders the row
  read, and compares it with `collect_runs` over the full transcript. A
  derived run that is absent from storage or newer than the stored one is
  `missing`. A stored run absent from the derived set is `stale`. `--fix`
  upserts the derived runs inside the 2.1 transaction. The audit prints the run count and the amount by unit for
  each session.

**Granularity:** one leaf. The three providers share one table, one upsert
rule, and one dispatcher. Splitting them per provider would triple the store
and wiring work for about 30 lines of parsing each.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_reported_usage.py -q`.

**Acceptance:**

- 2.3.1 - Claude `cost-state` lines from two process runs, one repeated with a larger cumulative cost, yield two runs. Each run keeps its largest reading and its own `startTime`, and a resumed run does not carry the earlier run's cost. test: `tests/sessions/test_reported_usage.py::test_claude_cost_state_runs_are_per_start_time`.
- 2.3.2 - A Grok turn converts `costUsdTicks` to USD exactly, and a turn flagged `usageIsIncomplete` stores `cost_complete` false. test: `tests/sessions/test_reported_usage.py::test_grok_turn_runs_convert_ticks`.
- 2.3.3 - A Droid sidecar yields one `factory_credits` run, and a missing sidecar yields none. test: `tests/sessions/test_reported_usage.py::test_droid_sidecar_run`.
- 2.3.4 - Upserting an older reading after a newer one leaves the newer one, and the live path, rebuild, and `audit --fix` produce identical rows. A plain audit reports a session whose run is missing as `missing` and writes nothing. `--fix` upserts the run, and a second audit reports `stale=0` and `missing=0`. Two cost-state records that tie on `totalCostUSD` resolve to the one with the larger `observed_at` on both the live path and the rebuild. test: `tests/sessions/test_reported_usage.py::test_upsert_keeps_newest_and_paths_agree`.
- 2.3.5 - With the Droid transcript unchanged, rewriting its sidecar with a larger `factoryCredits` and a later mtime refreshes the `droid:session` run on the next normal live pass. A further pass with the sidecar unchanged leaves the row untouched, with the same `xmin`. test: `tests/sessions/test_reported_usage.py::test_idle_pass_refreshes_droid_sidecar`.
- 2.3.6 - Appending only a Claude `cost-state` line to an otherwise idle parent transcript: the next live pass leaves through the `if not stats_records` exit, upserts the run, and advances the offset. Replaying that pass from the earlier offset changes nothing. When `_persist_ledger_batch` raises on that exit, the byte offset and parser state stay unchanged, and the next pass ingests the line. test: `tests/sessions/test_reported_usage.py::test_cost_state_only_append_reaches_the_ledger`.

## P3: The ledger interface
`kind: framing`

**Goal**: one authoritative per-session and per-task ledger, reachable from MCP,
HTTP, and the CLI.

### 3.1 `UsageLedgerStore`, `get_usage_ledger`, and `/api/admin/usage/ledger` [category: code] (depends: 1.1, 2.3)
`kind: deliverable`

Targets:
- `src/gobby/storage/usage_ledger.py`
- `src/gobby/mcp_proxy/tools/metrics.py::create_metrics_registry`
- `src/gobby/servers/routes/admin/_usage.py::register_usage_routes`
- `src/gobby/mcp_proxy/tools/sessions/_messages.py::register_message_tools`
- `docs/reference-audit/observability.json::*` — scope-reason: inventory entries for the new MCP tool and HTTP route
- `src/gobby/install/shared/skills/gobby/references/observability/usage.md`
- `tests/storage/test_usage_ledger.py`
- `tests/mcp_proxy/tools/test_usage_ledger_tool.py`

**Research context:**
- `create_metrics_registry(metrics_manager, session_storage, event_store, provider_capacity_resolver)`
  builds a `SessionTokenTracker` from `getattr(session_storage, "db", None)`
  and has a nested `require_calling_project_id()`.
- `GET /api/admin/usage` (`register_usage_routes.get_usage`) builds
  `TokenEventStore(server.services.database)` and is operator-scoped under
  `/api/admin`.
- `GET /api/sessions/{id}/token-events` returns at most 2000 rows ordered
  newest first, with an inclusive `since` and no continuation token. It is
  not a ledger and stays unchanged.
- `get_session_messages` (`register_message_tools`) pages rendered message
  groups.
- Task refs resolve through
  `src/gobby/storage/tasks/_id.py::resolve_task_reference(db, ref, project_id)`.
  Session refs resolve through
  `src/gobby/mcp_proxy/tools/workflows/_resolution.py::resolve_session_id(session_manager, ref)`.

**Implementation:**
- New `src/gobby/storage/usage_ledger.py::UsageLedgerStore(db)` with
  `page(*, session_id=None, task_id=None, cursor=None, limit=200) -> dict`.
  Exactly one of `session_id` and `task_id` is set. `limit` is clamped to
  1 to 1000.
  - **Scope rows.** A session scope covers that session's `token_events`. A
    task scope covers each `token_events` row whose attributed task is the
    given task. The attributed task comes from a `LATERAL` subquery: the
    interval in the row's session with
    `claimed_at <= event_at < coalesce(released_at, 'infinity')`, ordered by
    `claimed_at DESC, id DESC`, `LIMIT 1` (the latest claim wins). Candidate
    sessions are those with an interval for the task.
  - **Rows.** Each row has `event_at`, `session_id`, `message_id`, `source`,
    `model`, `api_calls`, the four token counts, and `agent_id`, ordered by
    (event_at, session_id, message_key). `message_key` is `message_id`, or
    `#<id>` when it is NULL.
  - **Cursor.** The cursor is URL-safe base64 of the JSON
    `[event_at, session_id, message_key]` of the last row. The next page
    starts strictly after it. A malformed cursor raises `ValueError`.
    `next_cursor` is NULL on the last page.
  - **Totals.** One aggregate query over the whole scope, independent of
    cursor and limit, runs on every request and describes the scope as of
    that request (the live-traversal rules are in Decision 10). It returns:
    - `rows`, `api_calls` (sum of non-NULL values), and
      `api_calls_complete` (no NULL `api_calls` in scope);
    - the four token sums;
    - `by_source` with the same fields.
  - **Reported spend.**
    - A session scope sums its runs by `cost_unit`, with `api_duration_ms`,
      `wall_duration_ms`, and a count of incomplete runs.
    - A task scope counts a run only when a single interval of the task, in
      the run's session, covers `[started_at, observed_at]` and no other
      interval of that session opens inside that span. A run that overlaps
      the task's intervals but fails that test is reported under
      `unattributed` with its unit and amount. Spend is never split.
  - **Durations.**
    - Session scope: `active_span_ms` from the first to the last event.
    - Task scope: `claimed_ms`, the sum of the task's interval lengths, with
      open intervals measured to now.
    - Both scopes: reported API and wall time.
  - **Coverage.** Coverage lists one descriptor for each source present in
    scope, taken from a static `SOURCE_COVERAGE` table that encodes the
    Provider Coverage table. Each descriptor gives the tokens, `api_calls`,
    and spend modes, each as `exact`, `reported`, or `unknown` with a reason.
    Coverage also carries `unkeyed_rows`, the count of rows with NULL
    `message_id`.
  - **Observed spend.** Each coverage descriptor also carries
    `spend_observed`, derived from the runs the scope reports for that source
    (attributed and `unattributed` together):
    - `unknown` with reason `no reported run` when the source has rows in
      scope but no run (a Claude session without `cost-state`, a Droid
      session without a sidecar);
    - `reported` with `runs` and `incomplete_runs` when at least one run
      exists. A run whose amount is 0 is reported as 0 and is never treated
      as unknown.
    - A source whose spend mode is `unknown` in `SOURCE_COVERAGE` keeps that
      mode and its capability reason.
    `reported` means only that these runs exist. It never claims that the
    session's billing is complete. Amounts stay provider-reported and
    separated by unit.
  - **Attribution.** A task scope returns `attribution_since`, the earliest
    `claimed_at` of the task's intervals, or NULL when the task has none.
- MCP: `create_metrics_registry` registers the read-only
  `get_usage_ledger(session_id=None, task_id=None, cursor=None, limit=200)`.
  - It builds `UsageLedgerStore` from the same database as the token tracker.
  - It resolves a task ref with
    `resolve_task_reference(db, ref, require_calling_project_id())`.
  - It resolves a session ref with `resolve_session_id` and refuses a session
    outside the calling project.
  - On failure it returns `{"success": false, "error": …}`.
- HTTP: `register_usage_routes` adds
  `GET /usage/ledger?session_id=&task_id=&cursor=&limit=`.
  - It takes UUIDs and runs the store call with `asyncio.to_thread`.
  - It returns 400 for a malformed cursor or a scope that is not exactly one.
  - It inherits `/api/admin` operator scope.
- `register_message_tools`: the `get_session_messages` description states
  that it pages rendered message groups, and that per-call usage and totals
  come from `gobby-metrics:get_usage_ledger`.
- `docs/reference-audit/observability.json` gains entries for the MCP tool
  (symbol `create_metrics_registry.get_usage_ledger`) and the HTTP route
  (symbol `register_usage_routes.get_usage_ledger`). Both reference
  `references/observability/usage.md`, which gains a short section on the
  ledger: scopes, the cursor as a live traversal, totals, coverage, and
  `spend_observed`.

Consumers unchanged:
- `src/gobby/mcp_proxy/registries.py` — no-edit-reason: calls `create_metrics_registry` with its unchanged signature.
- `tests/mcp_proxy/test_metrics_events.py` — no-edit-reason: exercises existing metrics tools, which are unchanged.
- `tests/mcp_proxy/tools/test_metrics_tool.py` — no-edit-reason: exercises existing metrics tools, which are unchanged.
- `src/gobby/servers/routes/admin/__init__.py` — no-edit-reason: calls `register_usage_routes(router, server)` unchanged.
- `tests/servers/routes/admin/test_usage.py` — no-edit-reason: covers `GET /usage`, which is unchanged.
- `src/gobby/mcp_proxy/tools/sessions/_factory.py` — no-edit-reason: calls `register_message_tools` with its unchanged signature.
- `tests/mcp_proxy/tools/sessions/test_search_session_messages.py` — no-edit-reason: only the `get_session_messages` description text changes.
- `tests/sessions/bench_transcript_index_append.py` — no-edit-reason: only the `get_session_messages` description text changes.

**Granularity:** one leaf. The store, its MCP tool, and its HTTP route are one
read contract. Splitting the surfaces would ship a store with no reader, or
duplicate the scope and cursor validation across leaves.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_usage_ledger.py tests/mcp_proxy/tools/test_usage_ledger_tool.py tests/skills/test_reference_library.py -q`.

**Acceptance:**

- 3.1.1 - The fixture is ingested through the real parsers and `_persist_usage_events`. It has two Claude calls with distinct usage, the first written as two content-block lines sharing one `message.id`, and three Codex `token_count` lines, one repeating a cumulative total. The ledger has exactly four rows, and its totals equal the hand-computed sums of the four distinct calls. test: `tests/storage/test_usage_ledger.py::test_fixture_calls_are_counted_once`.
- 3.1.2 - Paging that fixture at `limit=1` returns four pages whose rows, unioned, equal the single `limit=1000` page with no duplicates. While the data is unchanged, every page carries identical totals. test: `tests/storage/test_usage_ledger.py::test_paging_neither_loses_nor_double_counts`.
- 3.1.3 - Re-ingesting the same transcripts through the rebuild (`_persist_session_transcript`) leaves the rows and totals unchanged, and a cursor issued before the rebuild resumes at the same row. test: `tests/storage/test_usage_ledger.py::test_rebuild_keeps_rows_totals_and_cursor`.
- 3.1.4 - With two tasks claimed in sequence by one session, and an overlap where the later claim wins, each call is attributed to exactly one task. A reported run that spans both claims is `unattributed` for both tasks. `attribution_since` is the first `claimed_at`. test: `tests/storage/test_usage_ledger.py::test_task_scope_latest_claim_wins`.
- 3.1.5 - Coverage reports Codex spend as `unknown` with its reason, `api_calls_complete` false when a Droid row is in scope, and the count of unkeyed rows. test: `tests/storage/test_usage_ledger.py::test_coverage_reports_missing_data`.
- 3.1.6 - `get_usage_ledger` resolves `#N` task refs in the calling project, refuses a session from another project, and returns an error for a malformed cursor. The HTTP route returns the same page for UUIDs and 400 for a malformed cursor. test: `tests/mcp_proxy/tools/test_usage_ledger_tool.py::test_ledger_tool_and_route_scope_and_errors`.
- 3.1.7 - Four session scopes keep unknown spend separate from reported spend. A Claude session with token rows and no `cost-state` reports spend `unknown` with reason `no reported run`. A Droid session with no sidecar reports the same. A Grok session whose one complete turn has `costUsdTicks` 0 reports `reported` with 1 run, 0 incomplete, and 0 usd. A Grok session whose one turn is flagged `usageIsIncomplete` reports 1 incomplete run. The reported-spend sums contain only what the runs state. test: `tests/storage/test_usage_ledger.py::test_spend_unknown_is_distinct_from_reported_zero`.
- 3.1.8 - Extending the 3.1.2 fixture after page one is read: an appended Claude call raises the totals returned with page two and appears on a later page. A subagent row inserted with a timestamp before the cursor is counted in the totals but absent from the remaining pages. A traversal restarted without a cursor returns every row exactly once. test: `tests/storage/test_usage_ledger.py::test_paging_is_a_live_traversal`.

### 3.2 `gobby tokens ledger` and `gobby tokens quota` [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/tokens.py::*` — scope-reason: two new commands in the tokens group
- `docs/reference-audit/observability.json::*` — scope-reason: inventory entries for the two new CLI commands
- `src/gobby/install/shared/skills/gobby/references/observability/usage.md`
- `tests/cli/test_tokens_ledger_cli.py`

**Research context:**
- The `tokens` click group (cli/tokens.py) holds `audit` and `stats` and reads
  the database directly.
- There is no provider-capacity CLI. `src/gobby/cli/utils_config.py::get_daemon_client`
  returns a `DaemonClient` whose `call_http_api` reaches
  `GET /api/providers/{provider}/usage`.
- `src/gobby/cli/utils_resolution.py::resolve_session_id` and
  `src/gobby/cli/tasks/_utils/resolution.py::resolve_task_id` resolve refs.

**Implementation:**
- `gobby tokens ledger (--session REF | --task REF) [--cursor C] [--limit N] [--json]`:
  - resolves the ref and calls `UsageLedgerStore.page` directly;
  - prints totals, `by_source`, reported spend by unit, durations, coverage,
    and `next_cursor`;
  - prints each source's spend from `spend_observed`:
    `unknown (no reported run)` or the capability reason, or the reported
    amount by unit with the run and incomplete-run counts. Unknown spend never
    prints as `0`;
  - with `--json`, emits the page dict.
- `gobby tokens quota [PROVIDER]`:
  - calls `/api/providers/{provider}/usage` for the given provider, or for
    `claude`, `codex`, `grok`, `droid`, `qwen`, and `agy` in turn;
  - prints state, each window's used percent and reset time, the reason,
    and, from `details` when present, each window's alert level,
    `limit_reached`, `drawing_credits`, and the credit balance;
  - exits 1 with the client error when the daemon is unreachable.
- Two audit entries are added: `gobby tokens ledger` (symbol `token_ledger`)
  and `gobby tokens quota` (symbol `token_quota`). `usage.md` names both
  commands.

**Granularity:** one leaf. Two small commands in one group, sharing one
reference entry and one test file.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_tokens_ledger_cli.py tests/skills/test_reference_library.py -q`.

**Acceptance:**

- 3.2.1 - `gobby tokens ledger --session` and `--task` print the store's totals for the 3.1 fixture, and `--json` round-trips the page dict. Passing both scopes, or neither, is a usage error. test: `tests/cli/test_tokens_ledger_cli.py::test_ledger_command_prints_scope_totals`.
- 3.2.2 - `gobby tokens quota codex` prints each window with its reset time and alert level, and the balance, from a stubbed two-window daemon response, and an unreachable daemon exits 1. test: `tests/cli/test_tokens_ledger_cli.py::test_quota_command_reads_daemon_snapshot`.
- 3.2.3 - For the 3.1.7 scopes, `gobby tokens ledger --session` prints `unknown (no reported run)` for the Claude session without `cost-state`, `0 usd` over 1 run for the zero-cost Grok session, and 1 incomplete run for the incomplete Grok session. test: `tests/cli/test_tokens_ledger_cli.py::test_ledger_command_keeps_unknown_spend_distinct`.

## P4: Provider-neutral usage-limit detection and operator alerts
`kind: framing`

**Goal:** the operator learns promptly when any supported provider approaches or
hits a usage limit, with evidence confidence explicit. Verified structured/
reporter evidence is authoritative even without a percentage. A fresh text-only
pane match is useful as a "possible quota limit" alert, with no exhausted,
recovered or spawned fail-fast inference. All sources share the operator sink.

This is the 2026-10-06 amendment for #23715 (Provider-neutral usage-limit detection
and operator alerts). It supersedes the Codex-only P4 scope and the old unknown-
quota reasons for other providers. It retains the ledger, privacy, statusline,
communications, and spawn-fallback rulings. LM7 gobby#15389 confirmed the six-item
Decision Record on 2026-10-06; source receipt a1b86a7f-d1a9-44f8-8ec6-6f83981a9003.
The 21:58 CT Orchestrator ruling subsequently amended items 1-3 to the two
confidence paths recorded above; no verified pane origin is claimed.

P4 has five independently testable outcomes: normalization and persisted reads
(4.1), alert edges and delivery (4.2), live transcript collection (4.3), and
spawned/reporter collection with run fail-fast (4.4), and interactive/placed
terminal observation (4.5). It needs the
existing 1.1 capacity-details schema change, but not the P2 spend ledger.
No provider process, credential file, or undocumented endpoint is queried for
quota. AGY keeps its already-supported bounded `/usage` reporter.

### 4.1 Provider-neutral quota observations and persisted capacity reads [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/quota_observations.py`
- `src/gobby/providers/quota_signals.py`
- `src/gobby/providers/codex_rate_limits.py`
- `src/gobby/providers/capacity_service.py::*` — scope-reason: snapshot details, observation admission, reporter normalization, freshness, and record conversion implement one persisted observation contract
- `src/gobby/storage/provider_capacity.py::*` — scope-reason: the record, column projection, and conditional upsert must carry details and return write admission together
- `tests/providers/test_provider_quota_observation.py`
- `tests/providers/test_capacity_service.py::*` — scope-reason: reporter and reporterless fixtures exercise the changed persisted-read contract
- `tests/storage/test_provider_capacity.py::*` — scope-reason: storage tests cover component details round trips and row CAS merge admission

**Research context:**
- Current `ProviderCapacityService._refresh_with_fallback` discards persisted
  reporterless observations: `164| if reporter is None:` and
  `165| return _unknown(provider, "no usage reporter")`.
  Excerpt hash `31cd62a4d2049ce8947f06b83d522e82fb5097f37161863b3ffba114b197cfda`.
- The original 4.1 Codex sample has primary/secondary `used_percent`,
  `window_minutes`, epoch `resets_at`, credits, and reached flags. A windowless
  reached flag must now survive normalization instead of being discarded.
- Upstream Claude SDK `RateLimitInfo` distinguishes allowed, warning, and
  rejected, with optional utilization and reset. This is evidence of the
  provider type, not evidence that interactive transcripts always contain it.
  [Primary SDK type source](https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/src/claude_agent_sdk/types.py).
- Installed provider distributions were inspected without executing them:
  Claude 2.1.292 SHA256 `97a01e5bc74a199e67189435d0331ea3a24eac2e07db4b76d9148c5b0386138f`;
  Grok 1.0.46 SHA256 `e8daa302364c9c3b6a5546d511cfbd1ab5e5d407a9b04282f660665ea405f9f3`;
  Droid SHA256 `d086ee371842583a7acab03bcb6176c07598736a42a263c02162aa956ed851e8`.
  The literal signals below come from those distributions; binary presence
  does not prove a particular structured wire envelope or a numeric feed.
- Qwen 0.24.7 installed `chunks/chunk-PVHUAPKM.js`, SHA256
  `1e9e9d65fbdc7b6deaed04d8cfdf99d9bddde5fa374a78e0b290ce9f085a6377`,
  has `QUOTA_EXHAUSTED_PREFIX="Quota exhausted: "`. Its permanent-quota
  classifier requires quota exhausted/exceeded plus reset guidance, separately
  from transient capacity retry. The discontinued free-OAuth guidance is not
  a resettable quota episode. [Primary retry source](https://raw.githubusercontent.com/QwenLM/qwen-code/main/packages/core/src/utils/retry.ts).
- AGY's checked contract fixture has `648| "error": "Individual quota reached.
  Please upgrade your subscription to increase your limits. Resets in
  120h9m43s."` in a provider ERROR result. Excerpt hash
  `6e930b861444a7850d01483f8ef1bf8667519c1d15a4c9c78701e2ec698b6110`,
  `tests/fixtures/provider_contracts/agy/command-captures.json:641-650`.
- Existing `UsageWindow` and `ProviderUsageSnapshot` in providers/usage.py,
  existing AGY reporting, and the machine/provider storage key are reused.
  No account identity is inferred from secrets; unknown source/account scope
  cannot be promoted to a quantitative provider-wide window.

**Provider signal inventory:**

| Provider | Accepted signal | Observation owner | Unknown boundary |
| --- | --- | --- | --- |
| Claude | Conditional actual typed `rate_limit_event` with allowed/allowed_warning/rejected and optional utilization/reset is verified; `You've hit your limit` or `Usage limit reached` pane text is a possible-limit hint only | Live raw-record collector 4.3 for a timestamped typed event; spawned 4.4 and interactive/placed 4.5 pane owners | No local emitted-event fixture established yet; SDK types alone cannot enable a new envelope; no statusline or bare 429 inference |
| Codex | Verified transcript `rate_limits`, windowless reached flags and `task_complete.error.codex_error_info=usage_limit_exceeded`; `You've hit your usage limit` pane text is a hint only | Caught-up live collector 4.3; spawned 4.4 and interactive/placed 4.5 | Credits are not session spend; context-full is separate; pane strings never establish origin |
| Grok | Usage balance exhausted, usage limit reached or out-of-credits pane prefix gives a possible-limit hint | Spawned 4.4 and interactive/placed 4.5 | No verified structured quota envelope admitted; generic rate/global/concurrency limits are excluded; quantitative/exhausted state stays unknown |
| Droid | `Standard Usage limit reached.` pane prefix gives a possible-limit hint | Spawned 4.4 and interactive/placed 4.5 | No verified structured quota envelope admitted; bare 402/429, provider_rate_limited and spent factoryCredits excluded |
| Qwen | `Quota exhausted: ` prefix with exhausted/exceeded and reset guidance gives a possible-limit hint | Spawned 4.4 and interactive/placed 4.5 | No verified structured quota envelope admitted; bare 429, overload and discontinued OAuth guidance excluded; reset text is not an authoritative time |
| AGY | Existing `/usage` reporter windows are verified; recorded top-level status=ERROR/error=Individual quota reached envelope is a verified format, conditional on an existing source supplying it with provenance; same pane text is a hint only | Existing reporter refresh 4.1/4.4; spawned 4.4 and interactive/placed 4.5 panes | No new CLI command or headless output boundary; the recorded ERROR fixture alone does not make pane output structured; timeout stays unknown/stale |

**Implementation:**
- New `QuotaObservation` in quota_observations.py carries provider, observed_at,
  source kind, normalized windows, an explicit warning/reached/recovery signal,
  and allowlisted details. Every component carries its own limit_key, scope_key,
  source_observed_at, occurrence_key and supplied reset epoch. Optional values
  stay absent, never fabricated zero.
  Source identifiers used internally for replay control are opaque; details
  and outbound text never contain prompts, raw provider errors, or credentials.
- Separate PaneQuotaHint carries provider, opaque source generation/occurrence,
  detection time and possible-limit kind only: no windows/flags/reset/recovery.
  Persist dedupe in details["pane_hints"] through the same atomic row path,
  without refreshing quota components. A hint-only row still reads unknown.
- New quota_signals.py separates typed-event normalization and pane-hint
  classification. SnapshotResult supplies text/truncated/dropped_bytes/total_bytes,
  not message origin or source time. Never pass a synthetic trusted flag to turn
  pane text into a QuotaObservation. Typed input must come from the existing raw
  provider envelope/reporter boundary, not JSON quoted inside message content.
  Unknown envelopes/codes produce no observation. Claude's SDK schema is
  conditional evidence, not an emitted fixture; keep it disabled unless an
  actual timestamped provider event in the collector's native format has complete
  sanitized fixture provenance. No new event feed is added.
- Pane hints strip ANSI/decorations within last 15 lines and require the exact
  provider-specific prefix/form in the inventory. Generic keyword hits do nothing.
  After stripping only the existing pane decorations, use these bounded literal
  forms, with a word/punctuation boundary after a prefix: Claude prefixes
  "You've hit your limit" or "Usage limit reached"; Codex prefix
  "You've hit your usage limit"; Grok casefolded prefixes "usage balance
  exhausted", "usage limit reached", "out of credits", "run out of credits"
  or "You hit your free usage limit."; Droid prefix "Standard Usage limit
  reached."; Qwen prefix "Quota exhausted: " with exhausted/exceeded plus
  "will reset"/"reset at" in the same new complete block; AGY prefix
  "Individual quota reached.". No arbitrary Error:/API wrapper is invented.
  Unsupported layout/prefix stays unknown; complete synthetic renderer fixtures
  exercise these static-distribution literals without claiming UI provenance.
  A genuine-looking prefix and an identical user/tool/assistant quote are
  indistinguishable and both yield only possible-limit hints. Malformed/truncated
  ambiguous forms yield nothing. No reset duration, percent, account scope or
  recovery is inferred from pane text. Preserve complete sanitized input fixtures
  with provider/version and evidence classification; synthetic rendered inputs
  are labeled synthetic, never claimed emitted proof.
- Shared pane occurrence contract: hash existing Terminal.id, created_at,
  attempt_generation, host_epoch and session_id/agent_run_id ownership together
  for the opaque generation; no new terminal schema fields. SnapshotResult has
  no message-origin/time field. On first capture after attachment,
  ownership change or daemon restart, baseline retained 15 lines and emit nothing.
  Later captures need exact suffix/prefix overlap of prior/current normalized
  line sequences showing newly appended complete lines; consider hints only in
  those new lines. Identical redraws, no overlap, incomplete matched lines,
  replacement and host failure rebaseline/ignore rather than assert freshness.
  truncated=true for older offscreen history alone does not suppress a complete
  newly appended block with verified snapshot overlap; missing overlap does.
  Occurrence identity
  hashes generation, prior/current snapshot fingerprints and matched new block;
  time is first successful fresh capture, a detection time rather than provider
  event time. Duplicates are recorded before normalization and never refresh age.
  Relative pane reset is never normalized, so static AGY banners cannot move a
  reset forward. Limits present at attachment remain unknown until verified
  evidence or a fresh appended hint; this conservative gap is documented.
- Preserve `observation_from_rate_limits` in codex_rate_limits.py: primary and
  secondary become percent windows, with weekly/five_hour labels for 10080/300
  minutes; other lengths use `<n>m`. Retain limit_id, plan_type and allowlisted
  credits fields. A conclusive reached flag works with no windows; a null or
  windowless value without a signal returns None. Claude utilization becomes
  percent only when actually supplied; its explicit warning can have no window.
- ProviderCapacitySnapshot/Record gain default-empty details; to_dict, unknown,
  `_from_record`, get and the storage protocol carry them. Upsert accepts optional
  details and returns write admission. Row replacement uses compare-and-swap
  against the exact previously read observed_at; the row observed_at is a
  strictly advancing local write revision, not a provider source clock. A CAS
  miss reloads and remerges component state before retrying; it never publishes
  edges from the lost write. Component metadata lives in details, so the
  unchanged UsageWindow input type needs no timestamp fields or new migration.
  No migration is added here; 1.1 owns details.
- `ProviderCapacityService.observe` serializes with a per-provider async lock,
  admits each component independently. A component key is provider plus scope
  plus limit identity: use a supplied limit_id and window duration/type, never
  positional primary/secondary or a display label alone. Model-specific limits
  include the supplied model. A provider-documented account-wide scope uses its
  supplied opaque account scope; never discover account identity from credentials.
  If scope is absent, use an opaque current source-stream/generation key and
  mark scope unknown. Different unknown sources are not asserted to share an
  account. A windowless hard signal uses its verified signal family as limit
  identity; it does not overwrite a numeric window or an unrelated model.
- Store components in details["quota_components"], keyed by that identity,
  with source_observed_at, last_accepted_occurrence, supplied resets_at, state
  and optional measured fields. Reject an older timestamp or earlier supplied
  reset only for that same component. Equal timestamp plus equal occurrence
  is a duplicate; equal timestamp with contradictory values is ambiguous and
  rejected. An unseen five-hour reading at t9 remains admissible after a weekly
  reading at t10. Missing sibling components are preserved with their own clocks;
  row writes and updates to weekly never refresh five-hour freshness.
- Recovery must be a newer positive quota reading for the exact same scope and
  limit, with a later supplied reset or an explicit verified recovery event for
  that episode. Account-wide recovery cannot clear model-specific or unknown
  source components. Unknown-scope recovery can clear only its own source and
  limit, never another seat's unresolved episode. Missing fields, expired data,
  a vanished pane, an ordinary turn and transient errors never imply recovery.
- Reads age every component independently: stale when source_observed_at is
  older than 900s or its supplied reset has passed. Fresh exhausted components
  make the aggregate exhausted; otherwise any retained stale/reset-past component
  makes it stale, even beside a fresh available component. Available requires
  every retained component to be fresh and positively available; no accepted
  component means unknown. A warning remains warning metadata, not exhaustion.
  Return only fresh numeric windows, retaining stale component metadata/reasons
  in allowlisted details so an old hard limit cannot look freshly available.
- Both AGY reporter refresh and pushed observations enter this same admission
  path. Reporter reads/commands remain bounded and outside the provider lock;
  stale reporter failure preserves the last row and does not clear a hard signal.
- Capacity persistence/alerts do not own terminalization.4.4 reuses the existing
  current-source watchdog transcript error reader and recovery path, independently
  of successful storage or delivery; no new hard-evidence queue/cache is added.
- Reporterless reads apply the component-level 900s and supplied-reset rules,
  rather than the row write clock. A verified windowless exhausted
  observation with no reset uses the 900-second freshness rule and remains
  useful despite having no windows. Expiry yields stale, never available.
  With no accepted observation the reason states that provider's supported
  signal has not been observed; it never claims all non-Codex sources absent.

Consumers unchanged:
- `src/gobby/providers/usage.py` — no-edit-reason: existing reporter snapshots and UsageWindow remain the numeric input contract

**Granularity:** ten acceptance items inspect all six provider rows and one
shared persistence/read contract. Normalizers and persistence stay together
because a detector whose signal cannot survive get() is unusable. Collection
and alert lifecycle owners are separate outcomes in 4.2-4.5. Pane hints are
confidence-separated inputs to that contract, not a new poller.

**Focused verification (planned, not run):**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/providers/test_provider_quota_observation.py tests/providers/test_capacity_service.py tests/storage/test_provider_capacity.py -q`.

**Acceptance:**
- 4.1.1 - Claude pane forms yield hints only; typed warning/rejected events require emitted fixture/envelope/time provenance, never SDK types alone. No utilization/reset, statusline or generic 429 inference. test: `tests/providers/test_provider_quota_observation.py::test_claude_quota_signals`.
- 4.1.2 - Codex numeric sample, independent windows, windowless reached flag and typed usage_limit_exceeded normalize as verified; pane banners yield hints only and context-full stays separate. test: `tests/providers/test_provider_quota_observation.py::test_codex_quota_signals`.
- 4.1.3 - Grok inventory forms yield hints only; transient rate/global/concurrency limits and unverified typed codes do not. test: `tests/providers/test_provider_quota_observation.py::test_grok_quota_signals`.
- 4.1.4 - Droid Standard Usage form yields a hint only; bare 402/429, spent credits and unverified typed codes do not. test: `tests/providers/test_provider_quota_observation.py::test_droid_quota_signals`.
- 4.1.5 - Qwen quota/reset-guidance form yields a hint without authoritative reset; retryable 429, discontinued OAuth guidance and unverified typed codes do not. test: `tests/providers/test_provider_quota_observation.py::test_qwen_quota_signals`.
- 4.1.6 - AGY reporter windows normalize as verified; recorded top-level ERROR requires an actual native structured source, while identical pane text yields a hint. Relative reset uses the verified first occurrence time once; duplicate admission precedes normalization. Timeout is not exhaustion. test: `tests/providers/test_provider_quota_observation.py::test_agy_quota_signals`.
- 4.1.7 - Verified windowless hard signals persist exhausted with unknown numeric/reset, then stale after 900s. Every provider's hint-only row stays unknown and never refreshes quota clocks or resets. test: `tests/providers/test_provider_quota_observation.py::test_output_only_freshness`.
- 4.1.8 - Per-component admission accepts disjoint out-of-order five-hour/weekly readings, rejects same-component older/contradictory occurrences and earlier resets, and CAS races remerge without lost components or alerts. Partial weekly refresh leaves five-hour stale after 900s or its reset, duplicate occurrences never refresh age, and mismatched scope/model recovery clears nothing. Details and component clocks round-trip through isolated storage. test: `tests/storage/test_provider_capacity.py::test_observation_write_admission`.
- 4.1.9 - Typed normalization ignores quoted JSON/arbitrary message content, malformed fields, unknown codes, secrets and ambiguous times. Identical genuine/quoted-at-bottom pane forms can yield only the same uncertain hint, never quota/recovery/fail-fast. test: `tests/providers/test_provider_quota_observation.py::test_untrusted_or_ambiguous_signals_are_ignored`.
- 4.1.10 - Real Terminal/SnapshotResult fields drive baseline/new-line admission: retained-before-start/restart, redraw, unknown overlap and incomplete matches emit nothing; a new complete matching line with overlap gives one hint even when older offscreen history is truncated. Static relative-reset replay never refreshes age or moves reset. test: `tests/providers/test_provider_quota_observation.py::test_pane_occurrence_contract`.

### 4.2 Provider-neutral quota edges to the operator channel [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/quota_alerts.py`
- `src/gobby/providers/capacity_service.py::*` — scope-reason: observation admission persists edge state and schedules the single sink after commit
- `src/gobby/config/communications.py::CommunicationsConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated operator_alert_channel contract carrier
- `tests/contracts/http/config_schema.json::*` — scope-reason: regenerated operator_alert_channel schema carrier
- `src/gobby/runner_init/servers.py::init_servers`
- `tests/providers/test_quota_alerts.py`

**Research context:**
Existing #23604 (Quota edge alerts) supplies the reusable design: independent
window state, rising reached/credit edges, persisted alert state, post-upsert
delivery outside the lock, one bounded CommunicationsManager sink and both
config carriers. CommunicationsManager.send_message is async and selects the
channel's default_destination when no session is supplied. No agent fanout is
needed. Reporterless get() currently discards stored rows (capacity excerpt
hash `31cd62a4d2049ce8947f06b83d522e82fb5097f37161863b3ffba114b197cfda`),
so 4.1 must precede alerts. Communications may be disabled or its manager absent.

**Implementation:**
- Retain per-window ok/warn/exhausted, 90% warn and 100% exhausted, monotonic
  level within the same reset epoch, independent reset advances, and unchanged
  absent-window state. Store level/resets_at by 4.1's scope/limit identity, with
  that component's admitted source time; omit no clocks when persisting edges.
- Preserve Codex account-signal predicates: limit_reached is true when
  rate_limit_reached_type is a supplied valid non-null reached type or
  spend_control_reached is explicitly true. It becomes false only when the
  former is explicitly null and the latter explicitly false in an admitted
  same-scope event. Missing fields retain prior values. individual_limit is
  metadata, not a reached boolean. Persist both supplied reached fields,
  their source clock and derived state. The original research's Tier1 predicate
  names these exact fields; do not mistake individual_limit for a reached flag.
- drawing_credits is true only when a fresh window in that same scope is
  exhausted, credits.has_credits is explicitly true, and a supplied valid
  numeric balance is lower than the stored balance. A first balance establishes
  the baseline without a drawing edge. Persist balance and drawing_credits.
  Explicit has_credits=false, an equal/rising valid balance, or a complete
  admitted same-scope reading showing no exhausted window clears drawing_credits
  silently; missing balance/has_credits/windows retains state. Neither flag
  clearing is a quota reset or positive recovery of a verified windowless hard episode.
  Both flags alert once on a false-to-true edge, clear silently, and rearm only
  after that explicit clearing. Credit balances never measure session spend.
- Extend transition(previous_details, observation) for explicit provider warning,
  verified windowless hard exhaustion and limit_reached. State is keyed by
  machine/provider plus 4.1's normalized scope/limit identity;
  verified identical account limits across seats generate one edge. Unknown
  source scope remains separate, never falsely correlated for recovery. A repeated pane,
  reporter result, replay, daemon restart or observation from a second seat
  does not generate another edge. Persist edge state in details with the row.
- Explicit warning emits warn without inventing percent. A new hard-limit
  episode emits exhausted and, if an explicit reached signal is present,
  limit_reached once each; do not emit duplicate text for the same edge kind.
  Newer positive quota evidence for the exact same scope/limit resets that
  episode once; later
  window reset keeps the original per-window reset behavior. Expiry or wall
  clock passing a predicted reset only makes the row stale; it is not proof
  of reset and sends no reset message. Other exhausted windows remain intact.
- Pane hints send only "<provider>: possible quota limit" through the same sink,
  with no percent/reset/credit or exhausted/reached/reset semantics. Persist a
  machine/provider hint edge plus opaque occurrence metadata separately from
  quota alert_state. Suppress duplicates and hints from other seats while the
  prior hint is less than 900s old. Only a genuinely new occurrence after that
  bound can notify again; replay never can. This bound is hint notification
  policy, not quota freshness/recovery. Restart baselines panes again and keeps
  persisted dedupe. Hints/cooldown expiry never rearm/clear verified incidents.
  Dedupe keeps only the last occurrence for each live source generation plus
  the latest provider notification time; retire generation entries when their
  terminal is no longer live. Restart's baseline prevents historical replay,
  so no unbounded event history or retention service is required.
- Observation/admitted reporter writes compute edges under the provider lock,
  persist them atomically, and schedule sink calls only after successful upsert
  and after lock release with create_background_task. A lost write sends nothing.
- Keep build_operator_alert_sink and communications.operator_alert_channel.
  INFO-only fallback when disabled, missing manager, or empty channel; otherwise
  wait_for the single channel send with the existing 10s bound. Delivery errors
  and timeout are logged without propagating. Never await channel delivery in
  a processor/terminal scan or while holding the capacity lock.
- Alert text is provider, edge kind, optional measured window/percent/reset and
  allowlisted credit balance. Unknown fields are explicitly unknown or omitted.
  No session/run ids, raw error text, prompts, model responses or credentials.
  Startup construction uses late getters for manager and active runtime config.

Consumers unchanged:
- `src/gobby/communications/identities.py` — no-edit-reason: reads existing `CommunicationsConfig` fields only.
- `src/gobby/communications/manager.py` — no-edit-reason: reads existing `CommunicationsConfig` fields; the sink calls its unchanged `send_message`.
- `src/gobby/config/app.py` — no-edit-reason: nests `CommunicationsConfig`, whose new field has a default.
- `tests/communications/test_communications_manager.py` — no-edit-reason: builds `CommunicationsConfig` without the new defaulted field.
- `tests/communications/test_communications_storage.py` — no-edit-reason: builds `CommunicationsConfig` without the new defaulted field.
- `tests/communications/test_identities.py` — no-edit-reason: builds `CommunicationsConfig` without the new defaulted field.
- `tests/communications/test_session_bridging.py` — no-edit-reason: builds `CommunicationsConfig` without the new defaulted field.
- `tests/communications/test_telegram_access.py` — no-edit-reason: builds `CommunicationsConfig` without the new defaulted field.
- `tests/communications/test_telegram_decisions.py` — no-edit-reason: builds `CommunicationsConfig` without the new defaulted field.
- `tests/communications/test_threading.py` — no-edit-reason: builds `CommunicationsConfig` without the new defaulted field.
- `tests/config/test_config_communications.py` — no-edit-reason: asserts existing fields and defaults; the new field has a default.
- `tests/config/test_removed_config_fields.py` — no-edit-reason: checks removed fields; the new field is not one.
- `tests/config/test_url_validation.py` — no-edit-reason: covers `webhook_base_url` validation only.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: re-exports `init_servers`, whose signature is unchanged.
- `src/gobby/runner.py` — no-edit-reason: calls `init_servers` with its unchanged signature.
- `tests/config/test_restart_config_consumers.py` — no-edit-reason: drives `init_servers` with unchanged arguments.
- `tests/runner_init/test_config_runtime_startup.py` — no-edit-reason: drives `init_servers` with unchanged arguments.
- `tests/test_runner_lifecycle.py` — no-edit-reason: drives `init_servers` with unchanged arguments.

**Granularity:** eight acceptance items cover one operator-notification outcome,
including retained Codex predicates and the confidence-separated hint edge.
The new configuration field exists only for this sink. Collector wiring is
separate, so transport policy is testable without running provider monitors.

**Focused verification (planned, not run):**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/providers/test_quota_alerts.py -q`.
Run focused existing config carrier tests, scoped Ruff/mypy, suppression ratchet,
and quality/type audits covering all changed Python tests.

**Acceptance:**
- 4.2.1 - Original weekly incident replay sends exactly three alerts: warn at 95%, exhausted at 100%, drawing_credits on the first falling stored balance, then exactly one reset for a later window; repeats send nothing. Independent five_hour 92%/100%/later-reset emits its own warn/exhausted/reset while weekly 20% is unchanged. test: `tests/providers/test_quota_alerts.py::test_windows_alert_independently`.
- 4.2.2 - Verified windowless limits produce one exhausted/reached edge per exact scope/limit; duplicate same-scope seats/restart send nothing, warning has no fabricated percent and exact-scope positive recovery sends one reset. Unknown-scope/model mismatches never recover siblings. test: `tests/providers/test_quota_alerts.py::test_provider_signal_edges`.
- 4.2.3 - Same-epoch lower readings, omitted windows, stale events, expiry and elapsed reset time do not lower/re-arm state or send reset. test: `tests/providers/test_quota_alerts.py::test_no_inferred_recovery`.
- 4.2.4 - Single configured channel delivery is bounded 10s, outside lock and after accepted write; disabled/missing/raising/hung channels never stall observation or propagate failures. test: `tests/providers/test_quota_alerts.py::test_sink_degrades_without_raising`.
- 4.2.5 - Text contains only provider/edge and measured optional fields; unknown quota never reads as zero and no content/id/credential-shaped value escapes. test: `tests/providers/test_quota_alerts.py::test_alert_text_is_minimal`.
- 4.2.6 - Both config carriers expose operator_alert_channel and the startup sink reads current config/manager. test: `tests/providers/test_quota_alerts.py::test_operator_alert_config_carriers`.
- 4.2.7 - Either Codex reached flag produces one rising edge; explicit false clears silently and later true rearms. Missing flags preserve state. Credit fixtures cover has_credits=false, first/equal/rising/falling balance, exhausted versus nonexhausted windows, missing inputs and silent clearing/rearming; flag clearing never resets a hard quota episode. test: `tests/providers/test_quota_alerts.py::test_codex_flag_and_credit_predicates`.
- 4.2.8 - Six-provider hints send only possible-limit text through the bounded sink; cross-seat/cooldown/restart duplicates are suppressed, old occurrence replay never rearms and only a new occurrence after 900s may notify again. No hint changes quota state/freshness/recovery/fail-fast. test: `tests/providers/test_quota_alerts.py::test_possible_limit_hint_edges`.

### 4.3 Caught-up live transcript quota collection [category: code] (depends: 4.2)
`kind: deliverable`

Targets:
- `src/gobby/sessions/processor_quota.py`
- `src/gobby/sessions/processor_transcripts.py::ProcessorTranscriptMixin._process_session_unlocked`
- `src/gobby/sessions/processor_types.py::ProcessorHost`
- `src/gobby/sessions/processor.py::SessionMessageProcessor.__init__`
- `src/gobby/runner_init/servers.py::init_servers`
- `src/gobby/runner_init/services.py::_build_message_processor`
- `tests/sessions/test_live_quota_observation.py`

**Research context:**
ProcessorTranscriptMixin._process_session_unlocked already reads bounded raw
new_lines, records byte offsets, computes caught_up and then parses records.
Its `if not stats_records` return loses any provider event the usage parser
does not retain. Collect quota from typed raw envelopes before that return,
independently of usage ledger records. Claude rate_limit_event support is
conditional on an actual provider event; interactive/placed panes belong to 4.5,
while spawned panes belong to 4.4.
Codex transcripts carry rate_limits and a structured usage_limit_exceeded
terminal error; current watchdog/models already gives the quota terminal reason.
The rebuild writer is TranscriptProcessingMixin._persist_session_transcript,
and the audit is a separate caller; neither should publish live observations.
init_servers builds the shared capacity service. `_build_message_processor`
rebuilds it via activate(); services.py currently 847 lines, so use exactly one
attachment line there. No code is added to the P2 ledger dependency.

**Implementation:**
- New bounded processor_quota helper consumes provider, raw live lines and
  source timestamp/cursor provenance. Recognize the supported structured
  Codex and Claude signals from 4.1; unknown provider/event envelopes do nothing.
  It never scans ordinary user/tool/assistant content for phrases.
- On caught-up passes only, admit the newest valid observation per relevant
  limit/window whose source timestamp is within 900s of now. Call it before
  `not stats_records`; quota-only events must work without token usage. A
  historical catch-up pass, audit, rebuild, malformed/undated event or replay
  never generates a new observation/alert. Include originating session/stream
  generation from the processor host, never from arbitrary payload text, for
  current-source fail-fast linkage. Advance source cursor with the
  existing processing pass; retries use observation idempotence from 4.1.
- Initialize a typed default-None provider_capacity_service member in SessionMessageProcessor.__init__ and
  ProcessorHost, attach in init_servers, and reattach in the existing rebuild
  activation. The helper awaits observe, which schedules delivery and returns
  without waiting on communications. Isolation of a malformed quota record
  must not fail ordinary transcript indexing.
- Preserve per-scope/limit occurrence clocks and reset admission from 4.1;
  newest means newest for each component, never newest for the whole provider.
  A partial batch carries only observed components and never refreshes omitted
  siblings. Use the actual typed-envelope source timestamp and stream cursor
  as occurrence provenance; no processing-time refresh on retry. A
  supported provider without structured events still gets possible-limit pane
  hints from spawned 4.4 or interactive/placed 4.5. This collector neither polls
  nor promises new CLI events. Unverified Claude envelopes remain conditional;
  tests need actual native fixture provenance, never collector-invisible flags.

Consumers unchanged:
- `src/gobby/sessions/liveness_monitor.py` — no-edit-reason: SessionMessageProcessor construction retains its signature and the new service member defaults to None
- `tests/sessions/test_e2e_session_tracking.py` — no-edit-reason: SessionMessageProcessor construction retains its signature and the new service member defaults to None
- `tests/sessions/test_liveness_monitor.py` — no-edit-reason: SessionMessageProcessor construction retains its signature and the new service member defaults to None
- `tests/sessions/test_processor_catchup_occupancy.py` — no-edit-reason: SessionMessageProcessor construction retains its signature and the new service member defaults to None
- `tests/sessions/test_sessions_processor_unit.py` — no-edit-reason: SessionMessageProcessor construction retains its signature and the new service member defaults to None
- `tests/workflows/test_observer_context_usage.py` — no-edit-reason: SessionMessageProcessor construction retains its signature and the new service member defaults to None
- `src/gobby/sessions/processor_stats.py` — no-edit-reason: types against `ProcessorHost`, whose new member is additive.
- `tests/sessions/test_transcript_index_journal.py` — no-edit-reason: types against `ProcessorHost`, whose new member is additive.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: re-exports `init_servers`, whose signature is unchanged.
- `src/gobby/runner.py` — no-edit-reason: calls `init_servers` with its unchanged signature.
- `tests/config/test_restart_config_consumers.py` — no-edit-reason: drives `init_servers` with unchanged arguments.
- `tests/runner_init/test_config_runtime_startup.py` — no-edit-reason: drives `init_servers` with unchanged arguments.
- `tests/test_runner_lifecycle.py` — no-edit-reason: drives `init_servers` and `_build_message_processor` with unchanged arguments.
- `src/gobby/runner_init/config_subscribers.py` — no-edit-reason: existing rebuild callable keeps its signature

**Granularity:** one live transcript lifecycle owner with startup/rebuild wiring.
It is independent of terminal observation and needs no new parser or ledger.
The seven production Targets include a new helper and required host/attachment
carriers; splitting any attach would leave rebuilds silently unobserved.

**Focused verification (planned, not run):**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_live_quota_observation.py -q`.

**Acceptance:**
- 4.3.1 - A caught-up quota-only Codex event without stats reaches observe with actual envelope timestamp/cursor/session-generation provenance; Claude events require a proven emitted native fixture. Newest wins per scope/limit, preserving windowless reached/warning. Weekly t10 followed by unseen five_hour t9 admits both; partial refresh/replay preserve component clocks. test: `tests/sessions/test_live_quota_observation.py::test_quota_only_live_records`.
- 4.3.2 - Historical catch-up, rebuild, audit, malformed/undated records, user/tool quotes, stale timestamps and cursor replay send no new quota edge. test: `tests/sessions/test_live_quota_observation.py::test_history_is_not_live_quota`.
- 4.3.3 - A processor rebuilt through activate retains the exact shared service; quota admission does not stall on a slow operator channel. test: `tests/sessions/test_live_quota_observation.py::test_rebuilt_processor_keeps_capacity_service`.

### 4.4 Prompt spawned-terminal hints and verified-source fail-fast [category: code] (depends: 4.3)
`kind: deliverable`

Targets:
- `src/gobby/agents/provider_quota_monitor.py`
- `src/gobby/agents/watchdog/quota.py::*` — scope-reason: reuse the provider-neutral signal classifier while preserving the existing recovery payload contract
- `src/gobby/agents/idle_check_handler.py::*` — scope-reason: observe active spawned terminal scans before attention/idle gates and preserve fail-fast ordering
- `src/gobby/agents/lifecycle_monitor.py::*` — scope-reason: add only service forwarding to the idle handler; leave the existing periodic lifecycle and idle guards intact
- `src/gobby/servers/_app_lifecycle.py::create_lifespan`
- `tests/agents/test_provider_quota_monitor.py`
- `tests/agents/test_watchdog_quota.py::*` — scope-reason: original text-only fail-fast tests must exercise verified source evidence and preserve parent notification
- `tests/providers/test_capacity_service.py::*` — scope-reason: reporter observations and failures feed the shared admission/alert path

**Research context:**
- Spawned runs already receive an active terminal scan before idle detection.
  IdleCheckHandler.check_attention_agents captures 15 lines but currently stops
  when attention is disabled: `138| if not self._attention_tracker.enabled:`;
  `139| return 0`. Excerpt hash
  `5bce38c1570f4e688302e1a208532d51db6de971ee6efc658bb30d22d2657522`.
  AgentLifecycleMonitor._check_loop calls this before check_idle_agents every
  configured lifecycle interval (default 30s). Recent activity and coordination
  waits must not suppress quota observation.
- Interactive/placed terminal observation has its own lifecycle owner in 4.5;
  this section does not require an AgentRun for those sessions or scan their panes.
- detect_provider_quota currently returns None for any provider other than Codex;
  its stripped prefix cannot prove provider origin. Under the 21:58 ruling
  pane classification is hint-only; only verified evidence admits fail-fast.
  fail_provider_quota_agent already delegates to bounded terminalization, but
  its text-only caller loses origin. Existing verified transcript failures use
  _fail_on_terminal_provider_error/fail_terminal_provider_agent with the same
  provider_quota_exhausted reason and parent notification. Preserve that typed
  recovery path instead of routing text to either failure method.
- Existing IdleCheckHandler.current_provider_error_snapshot uses the run's
  child session/provider and existing WatchdogReaderRegistry, requiring newest
  provider-error output and _provider_error_postdates_run. The Codex reader
  admits only type=event_msg/payload.type=task_complete/error.codex_error_info=
  usage_limit_exceeded for this quota reason. Reuse that verified boundary before
  idle gates; neither aggregate capacity nor printed JSON can substitute for it.
  A window at 100% is not itself a terminal quota error when credits remain.
- _provider_error_postdates_run explicitly uses error.timestamp >= run.created_at:
  the run row exists before launch, while run.started_at is persisted afterward
  and would miss an immediate current-process startup failure. Preserve that
  inclusive pre-launch boundary. The existing resumed-rollout regression is
  tests/agents/test_lifecycle_monitor_watchdog_idle_recovery.py::test_resumed_codex_run_ignores_predecessor_terminal_error;
  it rejects predecessor output then admits a newly appended startup error.
- Literal consumer sweep: `gcode grep -w detect_provider_quota src/ tests/`
  found only its definition and idle_check_handler.py import/call. The banner
  literal sweep additionally found tests/agents/test_watchdog_quota.py's two
  pane-only fail-fast tests; those are owned Targets and are rewritten for
  verified evidence, with a paired pane-only no-fail regression. Existing typed
  Codex quota-error reader/recovery tests remain valid and need no edits.
- AgyUsageReporter.report already runs the supported usage command with a15s
  bound and normalizes windows.4.1 routes successful reporter results to observe;
 4.4 verifies that actual reporter refresh drives the same alerts.

**Implementation:**
- Move quota scanning behavior out of lifecycle_monitor.py into the new
  provider_quota_monitor.py; lifecycle_monitor.py retains only a small service
  forwarding setter to its existing idle handler. Its current 948 lines must
  remain below 1000; no quota parser or state machine goes in that file.
- Wire the shared service during HTTP lifespan startup to the spawned idle
  handler. Reuse its existing polling task/captures,
  intervals, startup readiness and shutdown; add no independently scheduled
  quota poller. Observation occurs on the next normal pass even if attention
  is disabled, a session was recently active, has a standing lifetime, or holds
  a coordination wait. Ordinary idle reprompt/completion rules are unchanged.
- Pane input is SnapshotResult.text/truncated/byte counters only. Hash existing
  Terminal.id/created_at/attempt_generation/host_epoch/current ownership for
  generation. First capture after attach/restart/generation change is baseline,
  with no hint. Later captures need prior-suffix/current-prefix line overlap and
  newly appended complete matching lines. No overlap, incomplete match, outage
  or replacement rebaselines without emission; unchanged text never refreshes
  time. Older clipped history alone does not reject a complete new block with
  overlap. Hash generation/previous/current fingerprints/new block for occurrence;
  time is first detection, not provider event time. Never normalize pane-relative
  reset. Genuine and quoted-at-bottom identical forms yield the same uncertain
  hint; neither is trusted quota. Shared persisted provider hint cooldown 900s
  suppresses cross-seat noise; it never changes quota clocks/recovery.
- Spawned scan uses current run/session provider and current owned terminal.
  Apply 4.1's generation/baseline/overlap occurrence contract and submit only a
  PaneQuotaHint from text. Remove text-only fail-fast admission from watchdog/
  quota.py and its idle-handler call site; identical quoted text never fails a
  run. Reuse current_provider_error_snapshot and the existing reader registry
  for fresh verified current-run terminal quota errors: require source event
  timestamp within 900s, error.timestamp >= run.created_at through the existing
  _provider_error_postdates_run helper, and error.timestamp >= the current
  Terminal.attempt_started_at. Both lower bounds are inclusive; never substitute
  run.started_at, which is written after launch. Retain
  current ownership, newest provider-error output, and terminal quota reason.
  Only that evidence calls _fail_on_terminal_provider_error once for the current run;
  preserve _complete_if_work_finished semantics from the existing recovery.
  Numeric window exhaustion, provider-wide capacity state, unknown reporter
  scope and unrelated sessions cannot terminate a seat. Capacity observation
  and current-source watchdog read are independent; persistence/sink failure
  never gates verified fail-fast. Reuse current-reader errors/cleanup rather
  than a new source-evidence queue, poller or provider error feed.
- Missing/gone terminals, host outage, timeout, ownership generation change,
  historical retained pane and empty output do not generate quota or recovery.
  Pane text has no trusted region and is a hint only. Disappearance of the
  banner is not a reset. Positive structured quota evidence from 4.3 or
  a later valid reporter reading is the recovery source for known state;
  verified windowless hard components with no recovery signal become stale
  after freshness, with unknown reset rather than invented recovery. Hint-only
  providers remain unknown; pane hints never establish or age quota components.
- Verify actual AGY refresh populates/advises the same service and sink, including
  a timeout/unsupported reporter returning unknown/stale without an exhaustion
  or reset edge. No additional AGY command or quota endpoint is introduced.

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: create_lifespan retains its signature and lifecycle ownership
- `tests/providers/test_version_gate.py` — no-edit-reason: existing lifespan fixture requires no quota observation and default attachment remains optional
- `src/gobby/agents/watchdog/recovery.py` — no-edit-reason: existing fail_provider_quota_agent accepts the same ProviderQuotaExhaustion contract and already terminalizes with the classified reason

**Granularity:** one spawned-run observation integration outcome. Interactive
panes have different lifecycle ownership and actions and are split into 4.5.
The shared admission/helper is reused, while each monitor has its own evidence.
AGY normalization/edge state remains owned by 4.1/4.2; this leaf tests real
reporter-to-sink wiring, not a second reporter state machine.

**Focused verification (planned, not run):**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_provider_quota_monitor.py tests/providers/test_capacity_service.py -q`.
Also run the focused existing test_watchdog_quota.py, watchdog/test_codex_reader.py,
test_lifecycle_monitor_watchdog_idle_recovery.py and
watchdog/test_interactive_lifecycle_cleanup.py consumer regressions.

**Acceptance:**
- 4.4.1 - All six providers' newly appended pane forms reach possible-limit hints on the next normal pass using actual snapshot fields and the shared sink; AGY reporter supplies verified windows. test: `tests/agents/test_provider_quota_monitor.py::test_all_provider_observation_owners`.
- 4.4.2 - Recent/standing/coordination-waiting spawned runs observe hints with attention disabled; only fresh verified current-source quota fails once with provider_quota_exhausted/parent notification. Quoted text and unrelated account/source exhaustion never fail the run; ordinary idle rules remain intact. test: `tests/agents/test_provider_quota_monitor.py::test_quota_precedes_idle_eligibility`.
- 4.4.3 - Actual snapshot fields enforce first-capture/restart baseline, generation/overlap/new-line guards; retained history, redraw and static relative reset never re-alert or refresh quota. Genuine/quoted matching lines produce the same hint-only classification. Shutdown creates no orphan quota task. test: `tests/agents/test_provider_quota_monitor.py::test_terminal_observation_lifecycle`.
- 4.4.4 - AGY refresh drives verified windows/edges; transient/unsupported refresh never invents exhausted/reset. Numeric window exhaustion/unknown reporter scope cannot fail a run. The existing current-source watchdog error path still fails verified quota independently of storage/sink failure; pane-only hints never fail. test: `tests/agents/test_provider_quota_monitor.py::test_reporter_and_failure_boundaries`.
- 4.4.5 - A verified startup quota event with run.created_at and Terminal.attempt_started_at below its timestamp, but timestamp below run.started_at, fails exactly once on the next normal scan; equality at either inclusive lower bound is accepted. Predecessor/pre-attempt errors never fail the successor, and completed work still wins. test: `tests/agents/test_provider_quota_monitor.py::test_startup_quota_uses_prelaunch_identity_bounds`.

### 4.5 Interactive and placed-seat quota observation [category: code] (depends: 4.4)
`kind: deliverable`

Targets:
- `src/gobby/agents/interactive_attention_monitor.py::*` — scope-reason: current interactive terminal captures observe quota independently of attention settings while preserving ownership and shutdown guards
- `src/gobby/servers/_app_lifecycle.py::create_lifespan`
- `tests/agents/test_interactive_attention_monitor.py::*` — scope-reason: exercise the service callback with absent attention state and current terminal ownership
- `tests/agents/test_interactive_quota_observation.py`

**Research context:**
InteractiveAttentionMonitor._check_attention_panes lists interactive sessions,
skips active agent sessions, resolves the current live terminal and captures
15 lines. `134| if manager is None or session_manager is None:` currently stops
the scan; `166| if session.id in active_agent_sessions:` separates it from
spawned scans. Excerpt hash
`356c6400a7527d9a071c8d333658f81ef4f8f1c8df14c31c585a3aadcd7955fe`.
HTTP create_lifespan creates/stops this existing monitor with startup/recovery
readiness. Placed seats without an AgentRun already belong to this boundary.
4.1 provides the normalizer/admission contract, 4.2 the single sink, and 4.4
the shared observation helper; do not create another polling service.
SnapshotResult exposes text/truncated/byte counters, no origin/event time.
Existing Terminal carries id/created_at/attempt_generation/host_epoch and
session_id/agent_run_id ownership; use those fields, never invented provenance.

**Implementation:**
- Attach the shared capacity service during the existing lifespan's monitor
  construction. Use the same owned 15-line snapshot with 4.1's source-generation,
  first-capture baseline, overlap and newly appended complete-line contract
  before optional attention syncing. Text can submit only a PaneQuotaHint;
  no snapshot-origin/timestamp fields or trusted flags are invented.
  A missing/disabled attention manager does not disable quota observation.
- Hash that current Terminal identity/ownership into an opaque generation.
  Baseline first capture after attach/restart/generation change; emit nothing
  from retained lines. On later captures require prior-suffix/current-prefix
  line overlap plus new complete matching lines. No overlap, incomplete match,
  outage or replacement rebaselines; identical redraw never refreshes time.
  Older clipped history alone permits a complete new block with overlap. Hash
  generation/previous/current fingerprints/new block for occurrence; detection
  time is not provider event time. No pane-relative reset normalization occurs.
  First-sighting limits present at attachment remain unknown until a new hint
  or verified observation. Genuine and identical quoted bottom lines are equally
  uncertain; submit possible-limit only. Shared provider cooldown 900s and opaque
  last-source occurrence suppress repeats across seats/restart without refreshing
  quota components or asserting shared accounts.
- Keep interactive/placed observation independent of AgentRun, recent session
  activity, standing lifetime, and coordination hold. It sends no keys and
  never fails/completes the human or placed session. Active spawned terminals
  remain owned by 4.4 and are not scanned twice here.
- Preserve startup readiness, current live-terminal ownership/generation,
  host-unavailable/timeout/gone-terminal handling and existing stop cancellation.
  Historical retained output, missing captures and disappeared banners cannot
  create a hint or reset. Genuine/quoted newly appended matching text can raise
  only an uncertain possible-limit hint, never quota/recovery or session failure.
  Without verified quota evidence capacity stays unknown; existing stale quota
  components stay stale. No recovery probe is added.
- Both scan owners use the same persisted hint dedupe and sink: several seats
  yield at most one possible-limit notification per provider within 900s. Verified
  quota edges retain 4.1's exact scope/limit admission; hints do not assert accounts.

Consumers unchanged:
- `src/gobby/servers/app_factory.py` — no-edit-reason: create_lifespan retains its signature and lifecycle ownership
- `tests/providers/test_version_gate.py` — no-edit-reason: existing lifespan fixture requires no quota observation and default attachment remains optional

**Granularity:** one independent interactive-terminal lifecycle owner. It shares
the existing service but has no spawned cleanup or reporter state machine.

**Focused verification (planned, not run):**
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_interactive_quota_observation.py tests/agents/test_interactive_attention_monitor.py -q`.

**Acceptance:**
- 4.5.1 - All six providers' placed/interactive sessions without AgentRun observe newly appended pane hints with attention disabled/recent activity/coordination hold; no keys, quota-state mutation or session failure occurs. test: `tests/agents/test_interactive_quota_observation.py::test_all_provider_interactive_signals`.
- 4.5.2 - Real snapshot fields enforce first-capture/restart/history/ownership/overlap/timeout guards; paired genuine/quoted-at-bottom forms yield hints only. Spawned terminals are excluded and shutdown adds no orphan poller. test: `tests/agents/test_interactive_quota_observation.py::test_interactive_observation_guards`.
- 4.5.3 - Simultaneous spawned/placed new hints produce one possible-limit notification within 900s; replay, disappearance and cooldown expiry do not create quota/reset or refresh component age. test: `tests/agents/test_interactive_quota_observation.py::test_shared_provider_edge_across_seats`.

## P5: Three surfaces, documented
`kind: framing`

**Goal**: an operator or agent can tell account quota, context occupancy, and
spend apart, and knows which tool answers which question.

### 5.1 Observability guide and skill references [category: docs] (depends: 3.2, 4.5)
`kind: deliverable`

Targets:
- `docs/guides/observability.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`
- `src/gobby/install/shared/skills/gobby/references/observability/capacity.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/transcripts.md`

**Research context:**
- `docs/reference-audit/observability.json` audits these anchors:
  - `docs/guides/observability.md`: `token-usage-and-savings`,
    `provider-capacity`, `token-ledger-audit`, `cli`, `http`, and `mcp`;
  - `docs/guides/cli-commands.md`: `gobby-tokens`;
  - `docs/guides/http-endpoints.md`: `admin`.
  Headings that carry them keep their text.

**Implementation:**
- `observability.md` §Token Usage And Savings (line 232) is rewritten around
  three surfaces:
  - **Account quota:** `get_provider_capacity` and `gobby tokens quota`, with
    the per-provider sources and freshness rules from 4.1.
  - **Context occupancy:** the existing occupancy snapshots, which are not
    accounting.
  - **Spend:** `get_usage_ledger`, `/api/admin/usage/ledger`, and
    `gobby tokens ledger`. Paging is a live traversal (Decision 10), and
    `spend_observed` separates unknown spend from reported zero.
- §Provider Capacity gains the six-provider signal/owner inventory, unknown
  numeric and recovery boundaries, per-component scope/freshness, `details`, the per-window
  alert levels with the account-wide `limit_reached` and `drawing_credits`
  alerts, confidence-separated possible-limit pane alerts, first-capture/restart
  baseline limitations, and `communications.operator_alert_channel`.
- §Token Ledger Audit gains the post-deploy `gobby tokens audit --all --fix`
  and the unkeyed-row drift rule.
- The guide states the attribution rules: latest claim wins, intervals start
  at migration 460, and database-clock edges.
- `cli-commands.md#gobby-tokens` lists `ledger` and `quota`.
  `http-endpoints.md` adds the `/api/admin/usage/ledger` row.
- `capacity.md` documents six-provider observations, source freshness,
  verified windowless exhausted versus pane-hint-only unknown quota, `details`, and the alert
  setting. `transcripts.md` notes that `get_session_messages` pages rendered
  groups and that the ledger answers per-call questions.

**Granularity:** one leaf. One guide section and its references describe one
model, and they land after every surface exists.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_reference_library.py -q`.

**Acceptance:**

- 5.1.1 - The reference audit passes with every audited anchor present, and the guide names one tool for each of quota, occupancy, and spend. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.

## V1 Plan Changelog
`kind: verification`

**2026-10-08 whitespace repair and M1 withdrawal:** The #23715 amendment
dropped the space between a word and a following number in 88 places,
including acceptance-item text that M1 copied into task criteria.
4b4918b5d45fbfed11e977cf6301483e0379b3c4 restores those spaces and makes no
other change. This commit withdraws the M1 block under the PD's stale-manifest
route. Adv4 gobby#15471 derives a fresh M1, and Adv4 and gobby#15414 verify
that the repair is whitespace-only. LM7 gobby#15389 directed this order on
2026-10-08: repair first, then the P4 re-expansion that the Orchestrator
approved as option A.

**2026-10-06 consensus:** Adv2 gobby#15414 reached pre-audit consensus on
these bytes (receipt 221ca410-a06e-412f-9a12-2140da67ffd3). The
Orchestrator's 22:39 CT ruling made W4 the canonical writer; Adv4 derives a
fresh M1 from the canonical narrative. P4 re-expansion and the #23603/#23604
rescope wait for the new-work freeze to lift.

**2026-10-06 amendment in progress:** #23715 extends P4 to all six providers.
The original P1-P3 deliverable bodies are preserved byte-for-byte; section 2.2's
heading gains an ordering dependency on 4.3 for their shared processor paths.
None of this
plan's implementation leaves was closed when checked. Existing 1.1 schema work
is actively owned and remains unchanged. Old P4 leaves #23603/#23604 remain
authoritative until the Orchestrator performs their approved rescope/retirement.
This scratch is narrative-only: old M1 was removed, not edited or reused.
Adversary consensus, fresh server-derived M1, expansion-mode validation, canonical
GO, exact-byte commit and P1 leaf transition are not yet done.

**2026-10-06 pre-audit repair in progress:** Adv2 PUSN-F1-F3 and N1 accepted.
F1 follows the 21:58 Orchestrator ruling: actual structured/reporter sources drive
state/fail-fast, fresh pane text raises possible-limit hints only; first-capture/
restart baseline and snapshot overlap bound hint freshness. F2 separates scope/
limit clocks and recovery identity from row CAS. F3 restores both Codex reached
flags, credit-drawing predicate, persisted balance and silent clearing/rearming,
with exact incident counts. N1 assigns spawned panes 4.4 and interactive/placed 4.5.
New review consensus is pending; no M1 is reused.

**2026-10-06 follow-up pre-audit repair:** Adv2 resolved PUSN-F1-F3/N1.
PUSN-F4 accepted: specify inclusive source timestamp >= run.created_at and
>= current Terminal.attempt_started_at, never run.started_at; add immediate
startup/successor/equality/completed-work regression in 4.4. PUSN-N2 accepted:
current Provider Coverage/Reuse framing and windowless-signal wording now match
the two confidence paths. Protected P1-P3 bodies remain unchanged. Consensus
is pending on these revised bytes.

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15469 under the
  Orchestrator rulings of about 14:50 CT. Targets and consumers were swept
  read-only on `0.5.0` after `4858476f43`. The draft is narrative only, with
  no M1.
- **2026-10-05, enhancement round 1 of 1.** `kind: enhancement`,
  `enhancer_run` 935c1967-daae-4ac5-8453-e4bee0013645
  (`plan-enhancer-taskless-old`), `suggestions_presented` 3, not converged.
  The Orchestrator gobby#14972 accepted all three:
  - PUS-E01 accepted. `_process_session_unlocked` returns before any ledger
    work when the parent has no new lines, so subagent appends and Droid
    sidecar changes had no ingestion path while the parent was idle. 2.2 now
    runs `_persist_ledger_batch` on that pass and adds 2.2.5. 2.3 adds 2.3.5
    and makes the upsert strict, so the idle pass that rereads the sidecar
    writes no row version when nothing changed.
  - PUS-E02 accepted. Run capability alone could not tell a missing record
    from a reported zero. 3.1 adds `spend_observed` to coverage with 3.1.7,
    and 3.2 renders it with 3.2.3.
  - PUS-E03 accepted. Totals are recomputed on every request, so "paging
    never changes totals" needed an unchanged-data condition. Decision 10
    now defines paging as a live traversal, 3.1.2 states the condition, and
    3.1.8 proves an append and a late earlier-timestamp row. No snapshot
    table, export job, or cursor lease is added.
- 2026-10-05: Consensus between the Plan Writer gobby#15469 and the Plan
  Adversary gobby#15471 at 904a22a18c, after one dialogue with four
  blocking findings, all resolved in the plan. No disagreement was left for
  the Orchestrator.
  - PUS-F1: the `if not stats_records` exit advanced the transcript offset
    without ledger work, so a `cost-state`-only append was lost. Every exit
    that consumes input now feeds `_persist_ledger_batch` before the offset
    advances, with parser-state restore on failure (2.2, 2.3.6).
  - PUS-F2: the audit repaired only sessions with different token totals,
    which left pre-460 identities and NULL `api_calls` in place. Drift now
    compares rows and reported runs, and the activation repair is the gate
    before the ledger counts as authoritative (2.1.6, 2.3.4, Constraints,
    V2).
  - PUS-F3: a fresh parser on each subagent tail reused fallback indexes.
    Per-file cursors now carry offset, message index, file identity, and
    parser state, with resets on truncation or replacement (2.2.6).
  - PUS-F4: alert levels followed only the longest window, so an exhausted
    five-hour window went unannounced. Alert state and stale rejection are
    per window, with account-wide `limit_reached` and `drawing_credits`
    (4.1.3, 4.2.5).
  - The Adversary found no over-engineering, and the project-scoped task
    resolution needed no change.
- 2026-10-05: Renewed consensus between the Plan Writer gobby#15469 and the
  Plan Adversary gobby#15471 after CR7 gobby#15396 bounced the plan at
  0738b01b80. The repair is aac743f4d2. M1 from 0738b01b80 is withdrawn
  because the repair added and changed acceptance items (memory f5577ae0
  route), and the Adversary derives it again. No disagreement was left for
  the Orchestrator.
  - CR7 B1: the activation repair read the transcript outside its
    transaction and then deleted every session row, so rows the live
    processor ingested in between were lost. The audit now reads stored
    rows before the transcript and reports `stale` and `missing` separately.
    `--fix` is a diff repair: it deletes only pre-read rows that are
    unkeyed or absent from the derived set, then upserts the derived set,
    so a same-key row is corrected in place. The gate is `stale=0` for
    every session and `missing=0` for every session that is not `active`
    (2.1, 2.1.7, 2.3, Constraints, V2). The Adversary caught a
    delete-after-upsert ordering bug in the first draft, and it was fixed
    before commit.
  - CR7 LOW-1: Claude cost runs keep the record with the largest
    `observed_at` (2.3, 2.3.4).
  - CR7 LOW-2: alert delivery is scheduled after the provider lock is
    released, with a 10 s send timeout (4.2, 4.2.3).
  - The Adversary's timing correction at 48000bc411: a line appended
    between the stored-row read and the transcript read is `missing`, and a
    line appended after the transcript read is reported by the next audit,
    since each audit is a snapshot bounded by its transcript read (2.1,
    2.1.7).

## V2: Verification
`kind: verification`

After every leaf has passed:

1. Run the focused suites of 1.1 to 5.1 together against the test hub, plus
   `cargo nextest run -p gobby-core --test schema_contract` and
   `cargo nextest run -p gobby-daemon --test cli_contract` (heavy work).
2. Run `uv run gobby plans validate .gobby/plans/provider-usage-spend.md -p <root>`.
3. Confirm the activation repair (Constraints, Repair at activation). Before
   it runs, existing Codex rows keep index ids while reread lines get
   cumulative keys, so those sessions can double-count, and pre-460 Claude
   and Grok rows lack `api_calls`. The repair compares rows, not only
   totals (2.1). It upserts every derived row, deletes only stale pre-read
   rows, recomputes `sessions.usage_*` from `get_session_totals`, ingests
   subagent rows, and upserts reported runs. `gobby tokens audit --all` must
   report `stale=0` for every session and `missing=0` for every session that
   is not `active`.
4. Planned after implementation: read quota for every provider against the
   six-provider fixtures and current supported signals. Compare Codex/AGY
   measured windows with their source; confirm verified windowless exhausted
   reports have unknown numeric fields, pane-only providers stay unknown and
   notify possible-limit only, and idle/coordination holds do not hide fresh
   appended hints or verified observations. Replay all verified edges and pane
   hints through one isolated sink; verify scope/limit clocks, quoted-text
   uncertainty, restart baseline and separate persisted dedupe.
   This is a future integration check, not evidence it has passed.
5. Run `gobby tokens ledger --task <a task closed after deploy>` and confirm
   that `attribution_since` falls inside the deploy window and that coverage
   lists every source present.
