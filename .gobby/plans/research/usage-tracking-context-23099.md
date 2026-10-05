# #23099 working context (W4 gobby#15469): research for the usage/spend plan

These are session-scoped notes. Once the plan file is written, it is the authority.
Plan path (to write): `.gobby/plans/provider-usage-spend.md`.

## Inputs

- Task #23099 "Plan provider usage remaining and per-session/task spend tracking":
  - Claimed by W4 on 2026-10-05.
  - Its validation criteria items 1-6 include the `get_session_messages` per-API-call boundary.
  - The evaluation fixture must have at least two provider calls with distinct usage, and must prove that paging neither loses nor double-counts calls or tokens.
- Prior research: `.gobby/plans/research/usage-monitor-2026-09.md` (Researcher gobby#14435, 09-24, HEAD befc623971). It is untracked and not mine. Its findings:
  - Codex rollout `token_count.rate_limits` carries the weekly %, credits and flags.
  - Gobby ignores that field.
  - The $700 credit-burn incident would have been caught by edge alerts at 90% and at 100% while drawing credits.
  - Never read CLI credentials or call `wham/usage` or `api/oauth/usage`.
- Memories:
  - 37fcc94c: accounting totals are not context occupancy.
  - ba7b75e5: Codex `output_tokens` already includes reasoning; never add `reasoning_output_tokens`.
  - 55b8c14e: the plan flow.
- Adv4 gobby#15471 prep:
  - `get_session_messages` pages rendered groups. It is not a call ledger.
  - `GET /api/sessions/{id}/token-events` returns at most 2000 rows, ordered by `event_at` DESC then `id` DESC. `since` is inclusive and there is no continuation token.

## Verified facts (2026-10-05, 0.5.0 after 14c6b62381)

- **Rate-limit fields:** `rate_limits`, `used_percent` and `window_minutes` still have no match in `src`, `crates` or `web/src`. The latest migration is 459 (`459_session_usage_bigint.sql`, #23439).
- **`token_events` table** (baseline.sql:3970):
  - Columns: id (identity), session_id, project_id, message_id (nullable), source, origin, model, model_family, input/output/cache_creation/cache_read (int), context_window, event_at, created_at, metadata.
  - The unique index `idx_token_events_dedup` is on (session_id, message_id) WHERE message_id IS NOT NULL. `idx_token_events_session` is on (session_id, event_at).
  - The session FK cascades.
- **Writers:**
  - Live: `sessions/processor_usage.py::ProcessorUsageMixin._persist_usage_events`.
  - Expiry rebuild: `sessions/transcript_processing.py::_persist_session_transcript`. It deletes `origin` backfill and transcript rows, then reinserts through `record_batch` ON CONFLICT DO NOTHING. Row ids change on every rebuild, so an id cursor is unstable.
  - Others: `cli/tokens.py:81` and `routes/admin/_testing.py:335`.
  - Metadata keeps only `content_type`.
- **Message ids per parser:**
  - Claude `_message_id_for` uses the API `message.id`, which dedupes repeated content blocks. The fallback is `<session>:claude:<index>`.
  - Codex `_message_id_for` uses the payload id, else `<session>:codex:<line index>`.
  - Grok `_message_id` and Qwen `_stable_message_id` are stable. Droid passes `message_id`.
  - **AGY sets no message_id**, so it is None and AGY events are never deduped.
- **Claude server-tool turns:** `usage.iterations` holds N model calls in one message, and `message.usage` sums them. Call count is therefore greater than message count.
  - `_reported_occupancy` uses the last iteration (claude.py:708-735).
- **Codex parsing** (codex.py `_parse_event_msg`): prefers `info.last_token_usage`, else `total_token_usage`. It computes input = input − cached, takes output as reported, and maps cache_read to cached.
  - Measured on one 10-05 rollout with 1331 token_count rows (all carrying rate_limits): 19 rows repeat the previous cumulative total.
  - Summing `last_token_usage.total_tokens` overcounts the final cumulative total by 41,344 of 68.8M.
  - Decision candidate: count a call only when `total_token_usage` increases, and take per-field deltas of the cumulative total, so the sum equals the provider total exactly.
- **Claude `cost-state` transcript record** (the parser skips it at claude.py:123):
  - Fields: `{totalCostUSD, totalAPIDuration, totalAPIDurationWithoutRetries, totalToolDuration, totalLinesAdded, totalLinesRemoved, totalDuration, startTime, modelUsage{model:{inputTokens, outputTokens, thinkingTokens, cacheRead/CreationInputTokens, webSearchRequests, costUSD}}, hasUnknownModelCost}`.
  - This matches the /usage session block Josh saw. It is cumulative per process run, keyed by `startTime`, and monotonic.
  - Over the last 7 days, 282 of 426 transcripts have it. Most have one row; up to 14 have been seen.
  - A resumed run with a new startTime started at cost 0, so there is no carry-over: take the max per (transcript, startTime) and sum across startTimes.
  - modelUsage includes Haiku background calls that are absent from the assistant messages.
- **Claude subagent transcripts** (`<transcript>/subagents/agent-*.jsonl`): found by `transcript_paths.py::_claude_subagent_transcripts`, but only `tasks/transcript_evidence.py` reads them. Token events skip them, so Claude per-session tokens undercount the process.
- **Claude statusline:** #19319 (commit 64a5b4144d, closed 07-30) removed statusline usage ingestion. Its criterion: "No production code parses Claude statusline usage fields"; the transcript is the sole authority.
  - ghook `statusline.rs` only forwards stdin to the downstream command. `POST /api/sessions/statusline` returns a retired 404.
  - Reading the statusline `rate_limits` for quota would need a ruling: does quota count as "usage"?
- **#19323** "Evaluate Claude OpenTelemetry as optional usage telemetry" closed as obsolete with no recommendation.
- **Claude rate limits:** a key scan of the 40 newest Claude transcripts found no structured rate-limit field. Statusline stdin (or stream-json `rate_limit_event` in print mode) is the only local Claude quota signal.
- **Capacity:**
  - `providers/capacity_service.py::ProviderCapacityService` keeps a four-state snapshot (available/exhausted/stale/unknown) with a 60 s freshness, per-provider reporters and support resolvers. Only AGY is wired, via `providers/usage.py::AgyUsageReporter` (`agy -p /usage`).
  - Storage `storage/provider_capacity.py` → `provider_capacity_snapshots`.
  - `UsageWindow` is {label, used, limit, unit, resets_at}.
  - MCP `gobby-metrics:get_provider_capacity(provider)` (metrics.py:86) and HTTP `routes/providers.py:408` expose it. It is constructed in `runner_init/servers.py:135`.
- **Usage report:** `gobby-metrics:get_usage_report(days)` → `SessionTokenTracker.get_usage_summary` → `TokenEventStore.get_breakdown`, which returns tokens only.
- **Session usage columns:** `sessions.usage_*` are cumulative. They are written in `storage/sessions/_usage.py` and read by the CLI (`cli/sessions.py`, `cli/tokens.py`), `routes/agents.py:225` (agent-run usage) and web `SessionsTab.entries.ts`.
- **Per-task usage:** no live per-task usage surface was found. #7580's TaskDetail display appears gone, since web is Chat-only (memory ac77469d).
- **Task attribution data:**
  - `session_tasks` (session_id, task_id, action, created_at) has a UNIQUE (session, task, action) key and keeps only the first claim, with no release row.
  - `task_lifecycle_events` has no session column.
  - `tasks.claimed_by_session_id` holds the current owner only.
  - Seats are long-lived and work many tasks, so per-task attribution needs claim intervals.
  - Candidate: a schema trigger on `tasks.claimed_by_session_id` writes `task_claim_intervals`. A token event then belongs to the interval covering its event_at; on overlap, the latest claim wins. No backfill.
- **Related tasks:**

| Task | Title | State |
| --- | --- | --- |
| #22075 | Detect provider usage outages across all providers and fall back to the next candidate | escalated, needs-decision; owns spawn fallback/gating |
| #21700 | Detect provider usage-limit exhaustion in agent output and fail runs fast instead of reporting them as stuck | closed |
| #12000 | Implement per-event token tracking end-to-end | closed; built token_events |
| #7580 | Add cost and token tracking display per task | closed |
| #19364 | Add AGY usage-capacity reporting when CLI becomes compatible | closed |

## Questions for the Orchestrator before drafting (W4 recommendations)

1. **Cost:** use provider-reported amounts only, tagged by unit: Claude `cost-state` USD, Grok `costUsdTicks`, Droid `factoryCredits`, Codex credit balance (account-level). Gobby keeps no price table, and there is no ccusage dependency.
2. **Claude quota:** statusline `rate_limits` via ghook is a crate change that reverses part of #19319, and there is no transcript source. Alternative: Claude quota is unsupported, state unknown, with a reason.
3. **Alerts:** global send_message alerts on edges (ok→warn at 90%, drawing credits at 100%, reset) are in scope. The spawn gate stays with #22075.
4. **Surfaces:** MCP and HTTP plus the CLI, extending `get_provider_capacity`, `get_usage_report` and the `gobby tokens` CLI with a session/task ledger. The web Activity display is deferred or in scope; that is the question.
5. **Task attribution:** claim intervals via trigger, with no backfill.

## Design sketch (pending rulings)

- **Ledger:** `token_events` becomes the per-call ledger.
  - message_id NOT NULL from every parser; AGY gets `<session>:agy:<index>`.
  - Add an `api_calls` int column, default 1; for a Claude iteration it is the count of type=message iterations.
  - Codex counts by cumulative delta.
  - Claude subagent transcripts are ingested into the parent session with metadata.agent_id.
  - Cost-state is stored per (session, startTime) in a new table or session columns.
- **Interface:** `gobby-metrics:get_usage_ledger(session_id | task_id, after=<cursor>, limit)` and `GET /api/usage/ledger`.
  - Totals are computed in SQL over the full set: calls, input, output, cache_read, cache_creation, and reported cost by unit.
  - The keyset cursor is (event_at, message_id) ascending and opaque. It is stable across rebuilds because message_id is stable.
  - Fixture: two calls with distinct usage, paged at limit=1; the totals equal the sum.
- **Quota:**
  - Codex `rate_limits` from transcript lines are pushed into `ProviderCapacityService` as an observation path, persisted in `provider_capacity_snapshots`.
  - The window is chosen by `window_minutes` (10080 weekly, 300 five-hour). Keep the newest non-null per `limit_id`; a window past `resets_at` becomes stale. Credits go in raw.
  - States: warn at ≥90; drawing_credits at ≥100 with credits falling; critical when the flags are non-null.

## PD rulings (gobby#14972, 2026-10-05 ~14:50 CT)

1. Cost: ACCEPT provider-reported only, tagged by unit, no price table.
2. Claude quota: ACCEPT `unknown`, reason "no local source". Do not reverse #19319. List the ghook statusline quota forward as a Josh decision, unscoped.
3. Alerts: in scope, but NOT global send_message (worker context noise, stacked wakes; Josh's #23125 concern). Use the existing operator notification path, cited. Spawn gate stays with #22075.
4. Surfaces: MCP, HTTP and CLI; web out.
5. Task attribution: claim intervals, latest claim wins, no backfill. Rung 2 first: derive from existing events if they suffice; otherwise trigger, citing the gap.
LM7 standing rule: commit every cited `.gobby/plans/research` file (this note and usage-monitor-2026-09.md) path-only before review; reply to LM7 with the list.

## Added verified facts (post-compaction)

- Operator path: `communications/manager.py::CommunicationsManager.send_message(channel_name, content, session_id=None)` goes to `outbound.py::OutboundCommunications.send_message`. `_require_session(None)` passes, and `enrich_metadata` falls back to `channel.config_json["default_destination"]` (set by `gobby comms channels add` Chat ID; docs/guides/telegram.md:52; routing.md precedence). No daemon-side operator alert config exists, so add `config/communications.py::CommunicationsConfig.operator_alert_channel: str = ""` (carriers: crates/gcore/assets/config/runtime_config_contract.json and tests/contracts/http/config_schema.json, both list communications.* keys). `communications.enabled` defaults False: delivery degrades to a log line and never raises.
- Rung-2 gap for ruling 5:
  - The UNIQUE (session_id,task_id,action) key at `baseline.sql:4576` drops re-claims.
  - `storage/tasks/_automation.py` clears claimed_by_session_id with no link_task row.
  - task_lifecycle_events has no session column.
  - About 50 src files reference claimed_by_session_id.
  So the trigger on `tasks` is the single interception point. Holder = claimed_by_session_id when closed_at IS NULL, else NULL; the trigger fires on INSERT or UPDATE OF claimed_by_session_id, closed_at. Closed tasks can keep claimed_by (guards like `task["closed_at"] is None or task["claimed_by_session_id"]`).
- Codex dedupe test (scratchpad codex_dedupe.py), 8 largest Oct rollouts:
  - Deduping on the key (total_tokens, input, cached, output) of `total_token_usage` and summing `last_token_usage.total_tokens` gives exactly the final cumulative total (error 0).
  - Without dedupe it overcounts by 101k to 2.92M per rollout.
  - Resets observed: 0.
  - Stateless id `<session>:codex:<total>:<input>:<cached>:<output>`. The existing unique index then dedupes in both live and rebuild.
  - Collision case: the same 4-tuple after a decrease (none observed).
  - Live rate_limits sample: `{limit_id:"codex", primary:{used_percent:80.0, window_minutes:10080, resets_at:<epoch s>}, secondary:null, credits:{has_credits, unlimited, balance:"62111.98"}, individual_limit, spend_control_reached, plan_type:"pro", rate_limit_reached_type}`.
- Grok `turn_completed` `params.update.usage`: inputTokens, outputTokens, totalTokens, cachedReadTokens, cacheCreationTokens, reasoningTokens, **modelCalls**, **apiDurationMs**, **costUsdTicks** (1e10 ticks = $1), modelUsage, numTurns. Parser grok.py `_turn_usage`/`_extract_usage`; id `_message_id("grok", session, index, prompt_id)`.
- Droid `.settings.json`:
  - Sidecar fields: `tokenUsage{inputTokens, outputTokens, cacheCreationTokens, cacheReadTokens, thinkingTokens, factoryCredits}` (cumulative) and `assistantActiveTimeMs`.
  - Parser behavior: droid.py `_load_sidecar` and `_usage_delta` emit one delta per parse pass on the last assistant message (ParsedAdjustment), so a row spans many calls and api_calls is NULL.
- AGY parser sets `usage=None` (agy.py `_message`), so there are no AGY ledger rows and no AGY id change is needed. AGY coverage: "no usage in AGY transcripts". Qwen: per-record usage with `_stable_message_id`. api_calls is NULL because one call per record is unverified.
- Claude:
  - `ClaudeTranscriptParser._usage_payload(raw)` is a static helper.
  - api_calls = number of `usage.iterations` entries of type "message", or 1.
  - The advisor_message iteration is excluded from Claude's own totals (memory d51df55b), so api_calls excludes it too. State this in the plan.
  - Memory a827e1da: cost-state must never become message deltas.
  - Subagents: 19 of 69 transcripts in 7 days have `subagents/`, 115 files in total.
  - Async Task tool results carry no usage (keys agentId, status, outputFile...).
  - `transcript_paths.find_supplemental_transcripts_on_disk("claude", path)` exists; only tasks/transcript_evidence.py uses it.
  - The subagent parser fallback id must be scoped by agent: construct the parser with session_id `<session>:<agent_id>`.
- Live path:
  - `processor_transcripts.py::ProcessorTranscriptMixin._process_session_unlocked` has the raw lines and calls `_process_parsed_batch`, which calls `processor_usage.py::ProcessorUsageMixin._persist_usage_events(session_id, messages)` (ParsedMessage only; raw_json available).
  - `session.usage_*` is set from `get_session_totals` after the inserts, so it stays dedupe-consistent.
  - `ProcessorHost` Protocol is in processor_types.py.
  - init_services (the processor, services.py:804) runs before init_servers (the capacity service, servers.py:135). Use `runner_init/servers.py::_resolve_message_processor` to attach the service.
- Rebuild path:
  - `transcript_processing.py::TranscriptProcessingMixin._persist_session_transcript` deletes origin backfill and transcript rows, then reinserts.
  - Live rows also use origin "transcript", so changed ids are replaced at rebuild.
  - Audit trap: `cli/tokens.py::_messages_to_events` sums before `store.record` dedupes, so Codex would drift forever. Dedupe by message_id first, and also count rows with NULL message_id as drift so `gobby tokens audit --all --fix` is the single post-deploy repair.
- Capacity:
  - `_refresh_with_fallback` returns unknown "no usage reporter" and ignores the persisted row; Codex needs the pushed row.
  - The snapshot state CHECK allows only available/exhausted, so the level and credits go in a new `details jsonb`.
  - `ProviderCapacityStorage.upsert` replaces unconditionally, so add the guard `observed_at > existing`. Rebuild never observes; only live does.
  - HTTP is `/api/providers/{provider}/usage` (providers.py::create_providers_router.get_provider_usage).
  - No capacity CLI exists. The CLI quota command uses `cli/utils_config.get_daemon_client` and `utils/daemon_client.py::DaemonClient.call_http_api`.
- Admin HTTP: `servers/routes/admin/_usage.py::register_usage_routes` (`/api/admin/usage`). Add `/usage/ledger` there.
- get_session_messages qualified name: `register_message_tools.get_session_messages` (docs/reference-audit/sessions.json). Skill refs: references/observability/usage.md, capacity.md; references/sessions/transcripts.md. Guide: docs/guides/observability.md §Token Usage And Savings (line 232).
- Consumer-coverage lint checks exact symbols only (plans/symbol_targets.py:600); `::*` is skipped.
- Line counts:
  - claude.py 823, codex.py 825 (keep edits minimal; Claude needs no edit).
  - token_events.py 627, processor_usage.py 436, processor_transcripts.py 433, transcript_processing.py 615, capacity_service.py 320, metrics.py 495, cli/tokens.py 249.
- Schema carrier set (copy gobby-messaging.md lines 276-289): migration file, catalog.manifest.json::*, assets.rs::MIGRATIONS, bundle.rs::GOLDEN_LATEST_CHECKSUM + ::expected_schema_identity, gcore tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity, gdaemon tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract, schema_expected_identity.json::*, tests/contracts/http/runtime_handshake.json::*, and tests/runtime_grants/golden/{brokered_datastores,direct_datastores,old_client_new_grant,payload_skew_unknown_field,unavailable_datastores}.json::*. Migration 460 at drafting; gobby-messaging (parked) also names 460-462, so whichever lands second renumbers.

## Final design (write the plan from this)

Plan `.gobby/plans/provider-usage-spend.md`, Plan ID `provider-usage-spend`. Framing sections:
- Overview
- Decision Record
- Constraints
- Requirement Mapping (criteria 1-6)
- Reuse (#12000 token_events reused as the ledger; #7580 web TaskDetail gone, replaced by CLI/MCP task scope; #19323 OTel closed obsolete, not reused; #21700 output fail-fast stays and quota complements it; #22075 owns the spawn gate and consumes get_provider_capacity)
- Provider coverage table
- Not in scope / Josh decision (ghook statusline quota forward; web; spawn gate; ccusage; price table)
- V1 Plan Changelog
- Verification

Leaves:
- **1.1 Schema [code]**, one migration:
  - `token_events.api_calls integer NULL CHECK >= 0`.
  - `session_reported_usage(session_id FK cascade, run_key, source, cost_unit CHECK in (usd, factory_credits) NULL, cost_amount numeric NULL, api_duration_ms bigint, wall_duration_ms bigint, started_at, observed_at NOT NULL, model_usage jsonb default {}, PK(session_id, run_key))`.
  - `task_claim_intervals(id identity, task_id FK cascade, session_id FK cascade, claimed_at NOT NULL, released_at NULL, CHECK released >= claimed)`, with a partial unique index on (task_id) WHERE released_at IS NULL, an index on (session_id, claimed_at), and an AFTER trigger on tasks.
  - `provider_capacity_snapshots.details jsonb NOT NULL DEFAULT '{}'`.
  - Full carrier set.
  - Test file tests/storage/test_schema_usage_ledger.py (new, test hub).
- **2.1 Call identity and counts (depends 1.1):**
  - Codex `_parse_event_msg` gets the cumulative-key id.
  - New module sessions/usage_calls.py `api_call_count(source, message) -> int | None`: claude uses iterations, codex 1, grok modelCalls; droid, qwen and agy give None.
  - TokenEvent.api_calls, _TOKEN_EVENT_COLUMNS, _record_params, _row_to_event_dict.
  - _persist_usage_events and _persist_session_transcript set api_calls.
  - cli/tokens `_messages_to_events` dedupes by id; audit treats NULL ids as drift.
  - Test file tests/sessions/test_usage_call_identity.py. Granularity note.
- **2.2 Claude subagent transcripts enter the parent ledger (depends 2.1):**
  - New sessions/subagent_usage.py `read_subagent_events(session, project, path, offsets)`.
  - Live: the processor keeps in-memory per-file byte offsets, and each pass in `_process_session_unlocked` reads complete new lines (restart rereads idempotently).
  - The rebuild and the audit read the full files.
  - metadata.agent_id is set; the fallback id is agent-scoped.
  - Test file tests/sessions/test_claude_subagent_usage.py.
- **2.3 Provider-reported run totals (depends 2.2):**
  - New sessions/reported_usage.py with `claude_cost_runs(lines)` (run_key startTime, usd, totalAPIDuration, totalDuration, observed_at = startTime + totalDuration), `droid_sidecar_run(path)` (run_key "session", factory_credits, assistantActiveTimeMs) and `grok_turn_runs(messages)` (run_key message_id, usd = ticks/1e10, apiDurationMs).
  - New storage/reported_usage.py `ReportedUsageStore.upsert_runs` with GREATEST on amount and observed_at.
  - Live: claude from raw lines in `_process_session_unlocked`; droid and grok in `_persist_usage_events`. Rebuild and audit also write.
  - Test file tests/sessions/test_reported_usage.py.
- **3.1 Ledger interface (depends 1.1, 2.3):**
  - New storage/usage_ledger.py `UsageLedgerStore.page(scope=session|task, cursor, limit)`.
  - Totals are SQL over the full scope: rows, api_calls, api_calls_complete, tokens; by_source; reported by unit (task scope: a run is attributed only when one interval of that task covers [started_at, observed_at], otherwise counted as unattributed).
  - Coverage per source (exact|reported|unknown + reason), unkeyed_rows, attribution_since.
  - Duration: task wall time is the sum of interval lengths; session active span is first to last event; api_ms comes from reported runs.
  - Keyset cursor is opaque base64 of (event_at, session_id, message_key) where message_key = message_id or `#<id>`. Limit default 200, max 1000.
  - Task attribution uses a lateral join to the latest interval of the row's session with claimed_at <= event_at < COALESCE(released_at, inf).
  - MCP `gobby-metrics:get_usage_ledger(session_id|task_id, cursor, limit)` in `metrics.py::create_metrics_registry`.
  - HTTP `GET /api/admin/usage/ledger` in register_usage_routes.
  - get_session_messages description points to the ledger.
  - Fixture test: two Claude calls with distinct usage, one Claude message.id repeated across content-block lines, and one repeated Codex cumulative token_count. Paging at limit=1 gives a page union equal to the totals and to the distinct calls; regrouping by get_session_messages does not change the ledger.
  - Tests: tests/storage/test_usage_ledger.py, plus an MCP test.
- **3.2 CLI (depends 3.1):** `gobby tokens ledger --session|--task [--json]` (direct DB) and `gobby tokens quota [provider]` (daemon HTTP). Test file tests/cli/test_tokens_ledger_cli.py.
- **4.1 Codex quota observations (depends 2.3, 1.1):**
  - New providers/codex_rate_limits.py `snapshot_from_rate_limits(raw, observed_at)`. Windows are labeled by window_minutes (10080 weekly, 300 five_hour, else `<n>m`): used = used_percent, limit 100, unit percent, resets_at ISO. Details carry credits, plan_type, limit_id and the reached flags.
  - ProviderCapacityService (capacity_service.py::*):
    - `observe(snapshot)` under a per-provider asyncio.Lock.
    - Guarded upsert.
    - Details on the snapshot.
    - No-reporter path returns the persisted row: available/exhausted when fresh (constant 900 s) and before every resets_at, else stale with a reason.
    - With no row: codex gives "no Codex rate_limits observed yet"; claude, grok, droid and qwen give "no local quota source" (Claude adds statusline-only, #19319).
  - `ProviderCapacityStorage.upsert` gains details and the guard; ProviderCapacityRecord gains details.
  - The processor calls observe for the newest Codex rate_limits in a batch (live path only).
  - ProcessorHost and SessionMessageProcessor get a provider_capacity_service attribute, attached in init_servers.
  - Test file tests/providers/test_codex_quota_observation.py. Granularity note.
- **4.2 Quota edge alerts (depends 4.1):**
  - New providers/quota_alerts.py: the level is monotone within a window (ok < warn >= 90 < exhausted >= 100 < drawing_credits once the balance falls while at 100). Persist level and window resets_at in details. Alert on upward edges, and send one reset alert when resets_at passes or a new window starts with the level back to ok.
  - Text carries provider, window %, resets_at and balance only.
  - The sink in init_servers uses `communications_manager.send_message(cfg.communications.operator_alert_channel, text)` and only logs when the channel is empty, comms is off, or the send fails.
  - CommunicationsConfig.operator_alert_channel plus its 2 carriers.
  - Test file tests/providers/test_quota_alerts.py.
- **5.1 Docs [docs] (depends 3.2, 4.2):** observability.md section rewrite (three distinct surfaces: quota, occupancy, spend), skill refs usage.md, capacity.md, transcripts.md.
- Post-deploy step: run `gobby tokens audit --all --fix` once (it also upserts reported runs).
