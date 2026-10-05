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
- Codex quota observations from live transcripts feed
  `ProviderCapacityService`, so `get_provider_capacity` and
  `gobby tokens quota` show Codex windows, reset times and the credit balance
  (4.1). Upward level edges and window resets alert the operator through the
  communications channel (4.2).
- The observability guide separates three surfaces: account quota, context
  occupancy, and spend (5.1).

## Decision Record
`kind: framing`

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
  - `src/gobby/runner_init/services.py` is 847 lines. 4.1 adds exactly one line
    there (848), under the 850-line growth threshold.
  - `src/gobby/sessions/transcripts/codex.py` (825) gains at most 12 lines in
    2.1.
  - `src/gobby/sessions/transcripts/claude.py` (823) is not edited.
  - New logic goes in new modules: `usage_calls.py`, `subagent_usage.py`,
    `reported_usage.py` (sessions and storage), `processor_ledger.py`,
    `usage_ledger.py`, `codex_rate_limits.py`, and `quota_alerts.py`.
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
| #21700 (Detect provider usage-limit exhaustion in agent output and fail runs fast instead of reporting them as stuck) | Output-based fail-fast for exhausted runs | Kept. Quota observation (4.1) reports state before exhaustion and complements it. |
| #22075 (Detect provider usage outages across all providers and fall back to the next candidate) | Escalated, needs-decision; owns spawn fallback and gating | Owns any spawn gate. It can read Codex state from `get_provider_capacity` once 4.1 lands. |
| #19364 (Add AGY usage-capacity reporting when CLI becomes compatible) | `AgyUsageReporter` and `ProviderCapacityService` | Reused. 4.1 adds observed providers beside the AGY reporter. |
| #19319 (Remove statusline usage ingestion) | The transcript is the sole Claude usage authority | Respected (Decision 2). |

## Provider Coverage
`kind: framing`

| Source | Ledger rows | `api_calls` | Reported spend | Quota |
| --- | --- | --- | --- | --- |
| Claude | One per API `message.id`, main and subagent transcripts | `usage.iterations` of type `message`, else 1 | `cost-state` USD per process run, with API and wall time | `unknown`: no local source (statusline only, #19319) |
| Codex | One per increase of `total_token_usage` | 1 | None. Codex reports an account credit balance only, shown in quota details. | `rate_limits` windows and credits, pushed from live transcripts |
| Grok | One per `turn_completed` | `modelCalls`; NULL when `usageIsIncomplete` | `costUsdTicks` / 1e10 USD per turn, with API time | `unknown`: no local source |
| Droid | One per parse-pass delta | NULL | `factoryCredits` per session (cumulative sidecar), with active time | `unknown`: no local source |
| Qwen | One per usage record | NULL | None | `unknown`: no local source |
| AGY | None (no usage in AGY transcripts) | None | None | `agy -p /usage` reporter (#19364) |

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

### 2.2 Claude subagent calls enter the parent ledger [category: code] (depends: 2.1)
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

## P4: Codex quota and operator alerts
`kind: framing`

**Goal**: the operator can see Codex quota remaining and is told when it
crosses a level, without any agent's context carrying the alert.

### 4.1 Codex `rate_limits` observations in `ProviderCapacityService` [category: code] (depends: 1.1, 2.3)
`kind: deliverable`

Targets:
- `src/gobby/providers/codex_rate_limits.py`
- `src/gobby/providers/capacity_service.py::*` — scope-reason: snapshot details, an observe path, and the observed-provider read path touch the snapshot, storage protocol, service, and record helpers
- `src/gobby/storage/provider_capacity.py::ProviderCapacityRecord`
- `src/gobby/storage/provider_capacity.py::ProviderCapacityStorage.upsert`
- `src/gobby/storage/provider_capacity.py::ProviderCapacityStorage.get`
- `src/gobby/sessions/processor_ledger.py`
- `src/gobby/sessions/processor_types.py::ProcessorHost`
- `src/gobby/runner_init/servers.py::init_servers`
- `src/gobby/runner_init/services.py::_build_message_processor`
- `tests/providers/test_codex_quota_observation.py`

**Research context:**
- A live Codex `rate_limits` sample:
  `{limit_id: "codex", primary: {used_percent: 80.0, window_minutes: 10080, resets_at: <epoch s>}, secondary: null, credits: {has_credits, unlimited, balance: "62111.98"}, individual_limit, spend_control_reached, plan_type: "pro", rate_limit_reached_type}`.
  - Every scanned row had `limit_id` `codex`.
  - Some rows are stale readings from older sessions, and some carry null
    (`usage-monitor-2026-09.md:198`).
- `ProviderCapacityService` (capacity_service.py):
  - `_get_once` returns a persisted row younger than 60 s. Otherwise it calls
    `_refresh_with_fallback`, which returns `unknown` "no usage reporter" for
    every provider except AGY and ignores the persisted row.
  - `ProviderCapacityStorage.upsert` replaces its row unconditionally.
- The service is built in `init_servers` (servers.py:135) and passed to the
  service container. `init_servers` already attaches `websocket_server` and
  `session_manager` to `runner.message_processor` (servers.py:357-359).
- A config change can rebuild the processor through
  `_build_message_processor` (services.py:518), whose `activate()` re-attaches
  live refs, and it already reads `runner.http_server`. Without a matching
  attach there, a rebuilt processor would stop observing.
- The HTTP server's service container holds the capacity service
  (`services.provider_capacity_service`, `servers/http.py:329`).
- `tests/providers/test_capacity_service.py` has a fake storage whose `upsert`
  takes no `details`, and it builds `ProviderCapacityRecord` without one.

**Implementation:**
- New `src/gobby/providers/codex_rate_limits.py::observation_from_rate_limits(raw, observed_at) -> QuotaObservation | None`:
  - `QuotaObservation` holds `observed_at`, `windows` (a tuple of
    `UsageWindow`), and `details`.
  - Each non-null `primary` or `secondary` becomes a window: label `weekly`
    for 10080 minutes, `five_hour` for 300, else `<n>m`;
    `used=used_percent`, `limit=100`, `unit="percent"`, and `resets_at` as
    ISO UTC.
  - `details` holds `limit_id`, `plan_type`, the `credits` object, and the
    two reached flags.
  - It returns None when `raw` is not a dict or has no window.
- `ProviderCapacitySnapshot` gains `details: Mapping[str, object]` (default
  empty), carried by `to_dict`, `_from_record`, and `unknown`.
  `ProviderCapacityRecord` gains a trailing `details` field that defaults to
  an empty dict. `ProviderCapacityStorage.get` selects it.
- `ProviderCapacityStorage.upsert` takes a keyword
  `details: Mapping[str, object] | None = None`, stored as `{}` when None. It
  adds `WHERE provider_capacity_snapshots.observed_at <= excluded.observed_at`
  and returns whether the row was written. The AGY reporter path keeps its
  call unchanged, so existing storage fakes stay valid; only `observe` passes
  `details`.
- `ProviderCapacityService.observe(provider, observation) -> bool` runs under
  a per-provider `asyncio.Lock`:
  1. It reads the stored row.
  2. It drops the observation when any window's `resets_at` is earlier than
     the stored `resets_at` of the window with the same label. That rejects a
     stale reading on a newer line for each independently resetting window.
  3. It sets the state to `exhausted` when any window is at or above its
     limit or either reached flag is set. Otherwise the state is `available`.
  4. It upserts with `source_version="transcript"` and returns whether the row
     was written.
- The read path for providers without a reporter:
  - `_get_once` takes its 60-second shortcut only for providers with a
    reporter.
  - For a provider without one, `_refresh_with_fallback` returns the persisted
    row. The row keeps its state when it is younger than
    `OBSERVED_FRESHNESS_SECONDS = 900` and before every window's `resets_at`.
    Otherwise it is `stale`, with the reason "last observation older than
    900 s" or "window reset at <time> has passed".
  - Without a row, Codex returns `unknown` "no Codex rate_limits observed
    yet". Claude returns "no local quota source (statusline only, #19319)".
    Grok, Droid, and Qwen return "no local quota source".
- Live wiring:
  - `_persist_ledger_batch` observes only on caught-up passes, and only when
    `self.provider_capacity_service` is set.
  - It observes the newest Codex `usage` record in the batch whose
    `raw_json.payload.rate_limits` is non-null and whose timestamp is within
    900 s of now.
  - The rebuild and the audit never observe.
- Attachment:
  - `ProcessorLedgerMixin` declares the class attribute
    `provider_capacity_service: ProviderCapacityService | None = None`, and
    `ProcessorHost` declares it.
  - `init_servers` attaches the service to `runner.message_processor` beside
    `websocket_server`.
  - `_build_message_processor.activate()` adds the single line
    `processor.provider_capacity_service = getattr(getattr(http_server, "services", None), "provider_capacity_service", None)`.

Consumers unchanged:
- `tests/providers/test_capacity_service.py` — no-edit-reason: its fake `upsert` and record construction omit `details`, which stays optional, and the reporter path it covers keeps its call.
- `tests/storage/test_provider_capacity.py` — no-edit-reason: calls `upsert` and `get` without `details`, which defaults to `{}`.
- `src/gobby/sessions/processor_stats.py` — no-edit-reason: types against `ProcessorHost`, whose new member is additive.
- `tests/sessions/test_transcript_index_journal.py` — no-edit-reason: types against `ProcessorHost`, whose new member is additive.
- `src/gobby/runner_init/__init__.py` — no-edit-reason: re-exports `init_servers`, whose signature is unchanged.
- `src/gobby/runner.py` — no-edit-reason: calls `init_servers` with its unchanged signature.
- `tests/config/test_restart_config_consumers.py` — no-edit-reason: drives `init_servers` with unchanged arguments.
- `tests/runner_init/test_config_runtime_startup.py` — no-edit-reason: drives `init_servers` with unchanged arguments.
- `tests/test_runner_lifecycle.py` — no-edit-reason: drives `init_servers` and `_build_message_processor` with unchanged arguments.

**Granularity:** one leaf. Observation, persistence, and the read path are one
contract: an observed provider's snapshot. Shipping observe without the read
path would persist rows that `get_provider_capacity` still reports as
`unknown`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/providers/test_codex_quota_observation.py tests/providers/test_capacity_service.py -q`.

**Acceptance:**

- 4.1.1 - `observation_from_rate_limits` maps the live sample to one `weekly` window (80/100 percent, ISO reset) with credits and plan in `details`, and returns None for a null or windowless value. test: `tests/providers/test_codex_quota_observation.py::test_rate_limits_map_to_windows_and_details`.
- 4.1.2 - A caught-up live Codex batch writes the newest reading, and `get("codex")` returns it as `available` with `details`. A catch-up pass and a rebuild write nothing. test: `tests/providers/test_codex_quota_observation.py::test_live_caught_up_batch_observes_newest_reading`.
- 4.1.3 - An older line timestamp, an earlier weekly `resets_at`, or an earlier `five_hour` `resets_at` with the weekly unchanged does not replace the stored row. A two-window reading with only the five-hour window at 100% stores state `exhausted`. test: `tests/providers/test_codex_quota_observation.py::test_stale_readings_are_rejected`.
- 4.1.4 - A stored row older than 900 s, or past a window's `resets_at`, reads as `stale` with its reason. Codex without a row reads as `unknown` "no Codex rate_limits observed yet". Claude reads as `unknown` with the statusline reason. test: `tests/providers/test_codex_quota_observation.py::test_observed_provider_freshness_and_reasons`.
- 4.1.5 - A processor rebuilt through `_build_message_processor` keeps `provider_capacity_service`. test: `tests/providers/test_codex_quota_observation.py::test_rebuilt_processor_keeps_capacity_service`.

### 4.2 Quota edge alerts to the operator channel [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/providers/quota_alerts.py`
- `src/gobby/providers/capacity_service.py::*` — scope-reason: observe() computes the level transition and calls the sink
- `src/gobby/config/communications.py::CommunicationsConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated contract entry for communications.operator_alert_channel
- `tests/contracts/http/config_schema.json::*` — scope-reason: regenerated schema entry for communications.operator_alert_channel
- `src/gobby/runner_init/servers.py::init_servers`
- `tests/providers/test_quota_alerts.py`

**Research context:**
- `src/gobby/communications/manager.py::CommunicationsManager.send_message(channel_name, content, session_id=None)`
  is async. With no session, outbound delivery uses the channel's
  `config_json["default_destination"]`.
- `communications.enabled` defaults to False. No daemon-side operator alert
  setting exists.
- `runner.communications_manager` is built during orchestration init
  (`runner_init/orchestration.py:766`), before `init_servers` runs, and can be
  None.
- In the 09-22 incident the window passed 90% at 12:38, reached 100% at
  15:59, and drew credits from 23:59. A purchased reset arrived at 09-24
  18:53.

**Implementation:**
- New `src/gobby/providers/quota_alerts.py`:
  - Alert state is kept per window, because Codex windows reset
    independently, and `observe` already marks the provider `exhausted` when
    any window is exhausted (capacity_service.py:227-228).
    `WINDOW_LEVELS = ("ok", "warn", "exhausted")`.
    `window_level(window)` is `exhausted` at or above its limit, `warn` at or
    above 90% of it, and `ok` otherwise.
  - Account-wide signals are kept apart from the windows:
    - `limit_reached` is true when either reached flag is set;
    - `drawing_credits` is true when any window is exhausted,
      `credits.has_credits` is true, and the balance is lower than the
      stored balance.
  - `transition(previous_details, observation) -> tuple[dict, list[str]]`:
    - Window state lives in `details["alert_state"]["windows"]`, keyed by
      window label, holding each window's `level` and `resets_at`.
    - For each observed window, with the same `resets_at` the stored level is
      the maximum of the previous and computed levels, so it is monotonic
      within that window's period. An upward change returns an alert naming
      that window.
    - With a later `resets_at`, the window's level is the computed one. When
      the previous level was above `ok`, a reset alert naming that window is
      returned. Other windows are untouched.
    - A window absent from the observation keeps its stored state. (`observe`
      has already dropped any observation with an earlier `resets_at` for a
      known window.)
    - `limit_reached` and `drawing_credits` each alert once on their rising
      edge and clear silently. `alert_state` also stores them and the
      balance.
  - Alert text is one line per alert. It names the provider, the alert, and,
    for a window alert, that window's label, used percent, and reset time,
    plus the credit balance. Example: "Codex quota warn: five_hour 92% used,
    resets 2026-10-05T23:00Z, credit balance 1265.60".
  - `build_operator_alert_sink(get_manager, get_config) -> Callable[[str], Awaitable[None]]`
    logs at INFO and returns without sending when comms is disabled, the
    manager is None, or `operator_alert_channel` is empty. Otherwise it awaits
    `asyncio.wait_for(manager.send_message(channel, text), ALERT_SEND_TIMEOUT_SECONDS)`
    with `ALERT_SEND_TIMEOUT_SECONDS = 10`. It catches and logs every
    exception, including the timeout, and never raises.
- `ProviderCapacityService`:
  - gains `alert_sink: Callable[[str], Awaitable[None]] | None = None`;
  - in `observe`, under the per-provider lock, applies `transition` before
    writing and stores the new details with the upsert. After a successful
    upsert, and after the lock is released, it schedules one sink call per
    alert with the existing
    `src/gobby/hooks/background_tasks.py::create_background_task`. `observe`
    never awaits delivery, so a slow channel cannot stall a processor pass
    or hold the lock.
- `CommunicationsConfig.operator_alert_channel: str = ""` names a configured
  channel. Regenerate both config carriers.
- `init_servers` sets
  `provider_capacity_service.alert_sink = build_operator_alert_sink(lambda: runner.communications_manager, lambda: runner.config_runtime.capture().snapshot.active.communications)`.

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

**Granularity:** one leaf. The level ladder, edge detection, and delivery are
one behavior. The config key exists only for this sink.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/providers/test_quota_alerts.py tests/contracts -q -k "config_schema or runtime_config"`.

**Acceptance:**

- 4.2.1 - Replaying the incident readings (a weekly window only) sends exactly three alerts: weekly warn at 95%, weekly exhausted at 100%, and drawing_credits when the balance first falls. A later window then sends one reset alert. Repeated readings at the same level send nothing. test: `tests/providers/test_quota_alerts.py::test_incident_replay_alerts_on_edges_only`.
- 4.2.2 - A stale lower reading inside a window does not lower the stored level or re-alert. test: `tests/providers/test_quota_alerts.py::test_level_is_monotonic_within_window`.
- 4.2.3 - The sink sends to `operator_alert_channel` through `CommunicationsManager.send_message`. It only logs when comms is disabled, the channel is empty, or the manager is None. A raising send is logged and does not fail `observe`. A send that never completes is cut off at `ALERT_SEND_TIMEOUT_SECONDS` and logged. `observe` returns, and the provider lock is free, before the send finishes. test: `tests/providers/test_quota_alerts.py::test_sink_degrades_without_raising`.
- 4.2.4 - Alert text contains no session id, message content, or credential-shaped value. test: `tests/providers/test_quota_alerts.py::test_alert_text_is_minimal`.
- 4.2.5 - In a two-window fixture the weekly window stays at 20% with an unchanged `resets_at`. The five-hour window rising to 92% and then 100% sends a warn alert and an exhausted alert, both naming `five_hour`, while the provider reads `exhausted` and the weekly sends nothing. When the five-hour `resets_at` advances and its use falls, one `five_hour` reset alert is sent and the weekly state is unchanged. A reached flag turning on sends one `limit_reached` alert, and repeating it sends nothing. test: `tests/providers/test_quota_alerts.py::test_windows_alert_independently`.

## P5: Three surfaces, documented
`kind: framing`

**Goal**: an operator or agent can tell account quota, context occupancy, and
spend apart, and knows which tool answers which question.

### 5.1 Observability guide and skill references [category: docs] (depends: 3.2, 4.2)
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
- §Provider Capacity gains Codex observation, `details`, the per-window
  alert levels with the account-wide `limit_reached` and `drawing_credits`
  alerts, and `communications.operator_alert_channel`.
- §Token Ledger Audit gains the post-deploy `gobby tokens audit --all --fix`
  and the unkeyed-row drift rule.
- The guide states the attribution rules: latest claim wins, intervals start
  at migration 460, and database-clock edges.
- `cli-commands.md#gobby-tokens` lists `ledger` and `quota`.
  `http-endpoints.md` adds the `/api/admin/usage/ledger` row.
- `capacity.md` documents Codex observation, `details`, and the alert
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
4. Read `gobby tokens quota codex` while a Codex session is active, and check
   the weekly percent and reset time against the newest rollout
   `rate_limits` line.
5. Run `gobby tokens ledger --task <a task closed after deploy>` and confirm
   that `attribution_since` falls inside the deploy window and that coverage
   lists every source present.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Usage ledger schema and the claim-interval trigger
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Claiming a task, re-claiming it from another session,
    and clearing `claimed_by_session_id` produce, in order, an interval for the first
    session, then one for the second, then no open interval. Each release time is
    greater than or equal to its claim time. test: `tests/storage/test_schema_usage_ledger.py::test_trigger_tracks_holder_changes`.

    1.1.2: Closing a task that keeps `claimed_by_session_id` closes its open interval.
    Reopening it opens a new one. A second open interval for one task is rejected
    by the partial unique index. test: `tests/storage/test_schema_usage_ledger.py::test_close_and_reopen_follow_holder`.

    1.1.3: `sync_task_claim_interval` opens an interval for an open, held task that
    has none (the migration seed), and a second call is a no-op. test: `tests/storage/test_schema_usage_ledger.py::test_sync_seeds_current_holder_idempotently`.

    1.1.4: `token_events.api_calls` rejects a negative value. `session_reported_usage`
    rejects an unknown `cost_unit`, a unit without an amount, and `observed_at` before
    `started_at`. Its rows and intervals cascade when their session is deleted. test:
    `tests/storage/test_schema_usage_ledger.py::test_ledger_columns_constrain_values`.

    1.1.5: `provider_capacity_snapshots.details` defaults to `{}`, and the packaged
    schema identity reports version 460. test: `tests/storage/test_schema_usage_ledger.py::test_capacity_details_default_and_identity`.'
  labels:
  - covers:provider-usage-spend:1.1:1.1.1
  - covers:provider-usage-spend:1.1:1.1.2
  - covers:provider-usage-spend:1.1:1.1.3
  - covers:provider-usage-spend:1.1:1.1.4
  - covers:provider-usage-spend:1.1:1.1.5
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Per-call identity and `api_calls`
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '2.1.1: A Codex rollout fixture with three `token_count` lines,
    one of which repeats the previous cumulative total, yields two ledger rows whose
    ids are the cumulative key. Their summed tokens equal the final `total_token_usage`
    (input minus cached, cached, and output). test: `tests/sessions/test_usage_call_identity.py::test_codex_cumulative_id_dedupes_repeats`.

    2.1.2: A Codex `token_count` without `total_token_usage` keeps the index id. test:
    `tests/sessions/test_usage_call_identity.py::test_codex_without_cumulative_keeps_index_id`.

    2.1.3: `api_call_count` returns 2 for a Claude message with two `message` iterations
    and one `advisor_message`, 1 without iterations, `modelCalls` for a Grok turn,
    None for a Grok turn with `usageIsIncomplete`, and None for Droid and Qwen. test:
    `tests/sessions/test_usage_call_identity.py::test_api_call_count_per_source`.

    2.1.4: The live writer and the rebuild writer both persist `api_calls`, and `list_session_events`
    returns it. test: `tests/sessions/test_usage_call_identity.py::test_writers_persist_api_calls`.

    2.1.5: The audit deduplicates parsed events by `message_id`, so a Codex session
    with repeated totals shows no drift. A stored row with NULL `message_id` is drift,
    and `--fix` replaces it with keyed rows. test: `tests/sessions/test_usage_call_identity.py::test_audit_dedupes_and_flags_unkeyed_rows`.

    2.1.6: Stored rows whose token totals equal the transcript''s still drift when
    they keep a pre-460 identity. This covers a Claude row and a Grok row with valid
    ids and NULL `api_calls`, and a Codex rollout without repeated totals whose rows
    keep index ids. They are reported `stale`. `--fix` rewrites them to keyed rows
    with `api_calls`, and a second audit reports `stale=0` and `missing=0`. test:
    `tests/sessions/test_usage_call_identity.py::test_audit_flags_equal_total_identity_drift`.

    2.1.7: `--fix` is safe against live ingestion. In a fixture that interleaves the
    audit with processor inserts, a row inserted after the audit''s stored-row read
    survives `--fix`, and `sessions.usage_*` afterwards equals `get_session_totals`
    including it. A line appended after the stored-row read but before the transcript
    read is reported `missing`, never `stale`, and nothing deletes it. A line appended
    after the transcript read is outside that pass: that audit does not report it,
    and a following audit reports it `missing` while it is still not ingested. Only
    pre-read rows that are unkeyed or absent from the derived set are deleted. A pre-read
    row with the same key but NULL `api_calls` is corrected in place to `api_calls`
    1, keeps its `id`, and is still present after `--fix`. test: `tests/sessions/test_usage_call_identity.py::test_fix_never_deletes_rows_ingested_during_audit`.'
  labels:
  - covers:provider-usage-spend:2.1:2.1.1
  - covers:provider-usage-spend:2.1:2.1.2
  - covers:provider-usage-spend:2.1:2.1.3
  - covers:provider-usage-spend:2.1:2.1.4
  - covers:provider-usage-spend:2.1:2.1.5
  - covers:provider-usage-spend:2.1:2.1.6
  - covers:provider-usage-spend:2.1:2.1.7
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Claude subagent calls enter the parent ledger
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: A parent transcript with one subagent file that holds
    two API calls yields two parent-session rows tagged with `agent_id`, through both
    the live path and the rebuild. `sessions.usage_*` includes them. test: `tests/sessions/test_claude_subagent_usage.py::test_subagent_calls_join_parent_ledger`.

    2.2.2: Lines appended to the subagent file between two live passes are read once.
    Rereading from offset 0 after a simulated restart inserts nothing new. test: `tests/sessions/test_claude_subagent_usage.py::test_live_offsets_read_each_line_once`.

    2.2.6: Two usage-bearing messages without an API id, appended to one subagent
    file in separate live passes, stay two rows with distinct ids. A simulated restart
    and the rebuild assign the same two ids and the same totals. Truncating the file
    below its cursor, or replacing it with a new inode, resets that file to offset
    0 and index 0, so the next pass reads the new content and the cursor is never
    stranded. test: `tests/sessions/test_claude_subagent_usage.py::test_subagent_identity_is_stable_across_passes`.

    2.2.3: The parent''s context occupancy snapshot and published tail occupancy are
    unchanged by subagent rows. test: `tests/sessions/test_claude_subagent_usage.py::test_subagent_rows_never_touch_occupancy`.

    2.2.4: Two subagents whose messages lack an API id get distinct agent-scoped fallback
    ids, and the audit includes subagent rows without reporting drift. test: `tests/sessions/test_claude_subagent_usage.py::test_fallback_ids_are_agent_scoped_and_audited`.

    2.2.5: With the parent transcript unchanged, two complete usage lines appended
    to a subagent file are ingested by the next normal live pass. The parent gains
    exactly two rows, `sessions.usage_*` includes them, and the parent''s occupancy
    is unchanged. A further idle pass inserts nothing and leaves `sessions.usage_*`
    unchanged. test: `tests/sessions/test_claude_subagent_usage.py::test_idle_parent_pass_ingests_subagent_appends`.'
  labels:
  - covers:provider-usage-spend:2.2:2.2.1
  - covers:provider-usage-spend:2.2:2.2.2
  - covers:provider-usage-spend:2.2:2.2.6
  - covers:provider-usage-spend:2.2:2.2.3
  - covers:provider-usage-spend:2.2:2.2.4
  - covers:provider-usage-spend:2.2:2.2.5
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Provider-reported run totals
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.3.1: Claude `cost-state` lines from two process runs, one
    repeated with a larger cumulative cost, yield two runs. Each run keeps its largest
    reading and its own `startTime`, and a resumed run does not carry the earlier
    run''s cost. test: `tests/sessions/test_reported_usage.py::test_claude_cost_state_runs_are_per_start_time`.

    2.3.2: A Grok turn converts `costUsdTicks` to USD exactly, and a turn flagged
    `usageIsIncomplete` stores `cost_complete` false. test: `tests/sessions/test_reported_usage.py::test_grok_turn_runs_convert_ticks`.

    2.3.3: A Droid sidecar yields one `factory_credits` run, and a missing sidecar
    yields none. test: `tests/sessions/test_reported_usage.py::test_droid_sidecar_run`.

    2.3.4: Upserting an older reading after a newer one leaves the newer one, and
    the live path, rebuild, and `audit --fix` produce identical rows. A plain audit
    reports a session whose run is missing as `missing` and writes nothing. `--fix`
    upserts the run, and a second audit reports `stale=0` and `missing=0`. Two cost-state
    records that tie on `totalCostUSD` resolve to the one with the larger `observed_at`
    on both the live path and the rebuild. test: `tests/sessions/test_reported_usage.py::test_upsert_keeps_newest_and_paths_agree`.

    2.3.5: With the Droid transcript unchanged, rewriting its sidecar with a larger
    `factoryCredits` and a later mtime refreshes the `droid:session` run on the next
    normal live pass. A further pass with the sidecar unchanged leaves the row untouched,
    with the same `xmin`. test: `tests/sessions/test_reported_usage.py::test_idle_pass_refreshes_droid_sidecar`.

    2.3.6: Appending only a Claude `cost-state` line to an otherwise idle parent transcript:
    the next live pass leaves through the `if not stats_records` exit, upserts the
    run, and advances the offset. Replaying that pass from the earlier offset changes
    nothing. When `_persist_ledger_batch` raises on that exit, the byte offset and
    parser state stay unchanged, and the next pass ingests the line. test: `tests/sessions/test_reported_usage.py::test_cost_state_only_append_reaches_the_ledger`.'
  labels:
  - covers:provider-usage-spend:2.3:2.3.1
  - covers:provider-usage-spend:2.3:2.3.2
  - covers:provider-usage-spend:2.3:2.3.3
  - covers:provider-usage-spend:2.3:2.3.4
  - covers:provider-usage-spend:2.3:2.3.5
  - covers:provider-usage-spend:2.3:2.3.6
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: '`UsageLedgerStore`, `get_usage_ledger`, and `/api/admin/usage/ledger`'
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '2.3'
  validation_criteria: '3.1.1: The fixture is ingested through the real parsers and
    `_persist_usage_events`. It has two Claude calls with distinct usage, the first
    written as two content-block lines sharing one `message.id`, and three Codex `token_count`
    lines, one repeating a cumulative total. The ledger has exactly four rows, and
    its totals equal the hand-computed sums of the four distinct calls. test: `tests/storage/test_usage_ledger.py::test_fixture_calls_are_counted_once`.

    3.1.2: Paging that fixture at `limit=1` returns four pages whose rows, unioned,
    equal the single `limit=1000` page with no duplicates. While the data is unchanged,
    every page carries identical totals. test: `tests/storage/test_usage_ledger.py::test_paging_neither_loses_nor_double_counts`.

    3.1.3: Re-ingesting the same transcripts through the rebuild (`_persist_session_transcript`)
    leaves the rows and totals unchanged, and a cursor issued before the rebuild resumes
    at the same row. test: `tests/storage/test_usage_ledger.py::test_rebuild_keeps_rows_totals_and_cursor`.

    3.1.4: With two tasks claimed in sequence by one session, and an overlap where
    the later claim wins, each call is attributed to exactly one task. A reported
    run that spans both claims is `unattributed` for both tasks. `attribution_since`
    is the first `claimed_at`. test: `tests/storage/test_usage_ledger.py::test_task_scope_latest_claim_wins`.

    3.1.5: Coverage reports Codex spend as `unknown` with its reason, `api_calls_complete`
    false when a Droid row is in scope, and the count of unkeyed rows. test: `tests/storage/test_usage_ledger.py::test_coverage_reports_missing_data`.

    3.1.6: `get_usage_ledger` resolves `#N` task refs in the calling project, refuses
    a session from another project, and returns an error for a malformed cursor. The
    HTTP route returns the same page for UUIDs and 400 for a malformed cursor. test:
    `tests/mcp_proxy/tools/test_usage_ledger_tool.py::test_ledger_tool_and_route_scope_and_errors`.

    3.1.7: Four session scopes keep unknown spend separate from reported spend. A
    Claude session with token rows and no `cost-state` reports spend `unknown` with
    reason `no reported run`. A Droid session with no sidecar reports the same. A
    Grok session whose one complete turn has `costUsdTicks` 0 reports `reported` with
    1 run, 0 incomplete, and 0 usd. A Grok session whose one turn is flagged `usageIsIncomplete`
    reports 1 incomplete run. The reported-spend sums contain only what the runs state.
    test: `tests/storage/test_usage_ledger.py::test_spend_unknown_is_distinct_from_reported_zero`.

    3.1.8: Extending the 3.1.2 fixture after page one is read: an appended Claude
    call raises the totals returned with page two and appears on a later page. A subagent
    row inserted with a timestamp before the cursor is counted in the totals but absent
    from the remaining pages. A traversal restarted without a cursor returns every
    row exactly once. test: `tests/storage/test_usage_ledger.py::test_paging_is_a_live_traversal`.'
  labels:
  - covers:provider-usage-spend:3.1:3.1.1
  - covers:provider-usage-spend:3.1:3.1.2
  - covers:provider-usage-spend:3.1:3.1.3
  - covers:provider-usage-spend:3.1:3.1.4
  - covers:provider-usage-spend:3.1:3.1.5
  - covers:provider-usage-spend:3.1:3.1.6
  - covers:provider-usage-spend:3.1:3.1.7
  - covers:provider-usage-spend:3.1:3.1.8
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: '`gobby tokens ledger` and `gobby tokens quota`'
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: `gobby tokens ledger --session` and `--task` print
    the store''s totals for the 3.1 fixture, and `--json` round-trips the page dict.
    Passing both scopes, or neither, is a usage error. test: `tests/cli/test_tokens_ledger_cli.py::test_ledger_command_prints_scope_totals`.

    3.2.2: `gobby tokens quota codex` prints each window with its reset time and alert
    level, and the balance, from a stubbed two-window daemon response, and an unreachable
    daemon exits 1. test: `tests/cli/test_tokens_ledger_cli.py::test_quota_command_reads_daemon_snapshot`.

    3.2.3: For the 3.1.7 scopes, `gobby tokens ledger --session` prints `unknown (no
    reported run)` for the Claude session without `cost-state`, `0 usd` over 1 run
    for the zero-cost Grok session, and 1 incomplete run for the incomplete Grok session.
    test: `tests/cli/test_tokens_ledger_cli.py::test_ledger_command_keeps_unknown_spend_distinct`.'
  labels:
  - covers:provider-usage-spend:3.2:3.2.1
  - covers:provider-usage-spend:3.2:3.2.2
  - covers:provider-usage-spend:3.2:3.2.3
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Codex `rate_limits` observations in `ProviderCapacityService`
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '2.3'
  validation_criteria: '4.1.1: `observation_from_rate_limits` maps the live sample
    to one `weekly` window (80/100 percent, ISO reset) with credits and plan in `details`,
    and returns None for a null or windowless value. test: `tests/providers/test_codex_quota_observation.py::test_rate_limits_map_to_windows_and_details`.

    4.1.2: A caught-up live Codex batch writes the newest reading, and `get("codex")`
    returns it as `available` with `details`. A catch-up pass and a rebuild write
    nothing. test: `tests/providers/test_codex_quota_observation.py::test_live_caught_up_batch_observes_newest_reading`.

    4.1.3: An older line timestamp, an earlier weekly `resets_at`, or an earlier `five_hour`
    `resets_at` with the weekly unchanged does not replace the stored row. A two-window
    reading with only the five-hour window at 100% stores state `exhausted`. test:
    `tests/providers/test_codex_quota_observation.py::test_stale_readings_are_rejected`.

    4.1.4: A stored row older than 900 s, or past a window''s `resets_at`, reads as
    `stale` with its reason. Codex without a row reads as `unknown` "no Codex rate_limits
    observed yet". Claude reads as `unknown` with the statusline reason. test: `tests/providers/test_codex_quota_observation.py::test_observed_provider_freshness_and_reasons`.

    4.1.5: A processor rebuilt through `_build_message_processor` keeps `provider_capacity_service`.
    test: `tests/providers/test_codex_quota_observation.py::test_rebuilt_processor_keeps_capacity_service`.'
  labels:
  - covers:provider-usage-spend:4.1:4.1.1
  - covers:provider-usage-spend:4.1:4.1.2
  - covers:provider-usage-spend:4.1:4.1.3
  - covers:provider-usage-spend:4.1:4.1.4
  - covers:provider-usage-spend:4.1:4.1.5
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Quota edge alerts to the operator channel
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: Replaying the incident readings (a weekly window only)
    sends exactly three alerts: weekly warn at 95%, weekly exhausted at 100%, and
    drawing_credits when the balance first falls. A later window then sends one reset
    alert. Repeated readings at the same level send nothing. test: `tests/providers/test_quota_alerts.py::test_incident_replay_alerts_on_edges_only`.

    4.2.2: A stale lower reading inside a window does not lower the stored level or
    re-alert. test: `tests/providers/test_quota_alerts.py::test_level_is_monotonic_within_window`.

    4.2.3: The sink sends to `operator_alert_channel` through `CommunicationsManager.send_message`.
    It only logs when comms is disabled, the channel is empty, or the manager is None.
    A raising send is logged and does not fail `observe`. A send that never completes
    is cut off at `ALERT_SEND_TIMEOUT_SECONDS` and logged. `observe` returns, and
    the provider lock is free, before the send finishes. test: `tests/providers/test_quota_alerts.py::test_sink_degrades_without_raising`.

    4.2.4: Alert text contains no session id, message content, or credential-shaped
    value. test: `tests/providers/test_quota_alerts.py::test_alert_text_is_minimal`.

    4.2.5: In a two-window fixture the weekly window stays at 20% with an unchanged
    `resets_at`. The five-hour window rising to 92% and then 100% sends a warn alert
    and an exhausted alert, both naming `five_hour`, while the provider reads `exhausted`
    and the weekly sends nothing. When the five-hour `resets_at` advances and its
    use falls, one `five_hour` reset alert is sent and the weekly state is unchanged.
    A reached flag turning on sends one `limit_reached` alert, and repeating it sends
    nothing. test: `tests/providers/test_quota_alerts.py::test_windows_alert_independently`.'
  labels:
  - covers:provider-usage-spend:4.2:4.2.1
  - covers:provider-usage-spend:4.2:4.2.2
  - covers:provider-usage-spend:4.2:4.2.3
  - covers:provider-usage-spend:4.2:4.2.4
  - covers:provider-usage-spend:4.2:4.2.5
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Observability guide and skill references
  category: docs
  task_type: chore
  depends_on:
  - '3.2'
  - '4.2'
  validation_criteria: '5.1.1: The reference audit passes with every audited anchor
    present, and the guide names one tool for each of quota, occupancy, and spend.
    test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.'
  labels:
  - covers:provider-usage-spend:5.1:5.1.1
  tdd: false
  source_section: '5.1'
  assigned_agent: tech-writer
```
