# Two-Daemon Hub — Remote Data Stacks

**Plan ID:** two-daemon-hub
**Plan kind:** strategy (execution decision record + deltas; the implementation
plan is `.gobby/plans/m0-shared-datastores-bridge.md`, adopted below)
**Root:** epic #17488 — shared daemon with machine-local execution

Plan artifact: `.gobby/plans/two-daemon-hub.md` (canonical — supersedes the
plan-mode copy at `~/.claude/plans/can-you-do-an-refactored-sphinx.md`)

## Overview
`kind: framing`

Josh is putting the three Gobby data stacks (PostgreSQL, Qdrant, FalkorDB) in Docker on his Linux desktop (AMD 395+, 128 GB) **now (approved 2026-07-30)**, so work continues while the laptop is closed during next week's travel. The laptop's daemon joins the same hub remotely over Tailscale; a co-located daemon runs on the desktop. This surfaced from (and builds on) the DB/codebase audit in the Appendix. The larger foundation program (cleanup → gcore schema authority → Rust crates) is plan #2, separate artifact.

## Decision Record (locked — review and amend HERE)
`kind: framing`

1. **Topology** — Desktop: Postgres + Qdrant + FalkorDB in Docker + co-located gobby daemon. Laptop: keeps its daemon, joins the SAME hub remotely. Two machine identities, one user store.
2. **Network** — Tailscale only; stacks bind to the tailnet interface. No public exposure, no TLS certs.
3. **Migration** — `gobby pack` on laptop → volume restore on desktop; copy `~/.gobby/.secret_kek` (KEK is a portable file; DEK travels in DB as `secret_key_material.wrapped_dek`). No re-encryption.
4. **Automation on BOTH daemons** — cron due-job claims become atomic leases (`FOR UPDATE SKIP LOCKED`); dispatch claims filtered by machine; template sync disciplined; machine-local loops (tmux, agent lifecycle, local worktree reaping) run on both. Includes fixing the mutex lease TypeError (`dispatch/workspace_merge.py:668`) and wiring the unreachable lease sweeps (`startup=True` never passed — `dispatch/dispatcher.py:201`).
5. **Machine identity (minimum today)** — machines registry row at daemon boot; `machine_id` stamped on `worktrees`/`clones` (sessions already carry it); dispatch gate: only claim tasks whose artifacts live on this machine; fresh unclaimed tasks fair game. Full ownership filtering → foundation (#17436/#17437).
6. **Path affinity** — repo-touching jobs (wiki, code index) skip when the project root doesn't exist locally. Path-independent projects → #17435.
7. **Remote lifecycle** — daemon skips local container management when endpoints are non-loopback.
8. **Foundation constraints recorded for plan #2** — sequence: cleanup (audit below = evidence; **recall tables exempt — research data**) → remote-model deltas (identity + path-independence) → gcore schema authority + filename-aware migrations → crate build-out (`gobby` multicall CLI/launcher, `gdaemon` supervisor, feature crates; session/spawn split last). Surfaces: CLI-first + thin rmcp MCP wrappers over shared Rust cores. **Hosted-path constraint (load-bearing):** feature crates target storage traits backable by direct-PG or remote-API; gdaemon is the future hosted backend; client DSN access is explicitly temporary.

## Implementation Plan — adopt the existing M0 design + 5 deltas
`kind: framing`

**Base plan: `.gobby/plans/m0-shared-datastores-bridge.md`** (design draft for exactly this milestone under epic #17488 / `shared-remote-stack`; never reviewed or built — V1 changelog empty, no task manifest). Its P1–P4 stand as written:

- **P1 Remote datastore topology**: `datastore_mode: local|remote` bootstrap key (loopback validator `config/bootstrap.py:190-205` runs only in `local`); compose bind-address knob + `databases.bind_address`/`databases.published_host` config keys + `gobby datastores expose --bind <ts-ip> --host <name>` (wildcard binds refused); remote-mode `gobby install` with per-service reachability preflight and copy-from-hub KEK/token guidance.
- **P2 Machine scoping**: migration adding `machine_id NOT NULL` to `worktrees`, `clones`, `agent_runs`, `cron_runs` (backfill via authoritative `sessions.machine_id` join, fail-closed; per-machine unique indexes); machine-scoped agent lifecycle/workspace readers and claim surfaces; machine-scoped transcript/summarizer/watchdog consumers; cron sweeps scoped (`_fail_remaining_active_runs`, `fail_stale_running_runs`, `count_running` — occurrence claiming is already cross-daemon safe via `CronRunAdmission` advisory lock + CAS, `scheduler/scheduler.py:155-181`).
- **P3 Guards + runbook**: migration lockstep guard (`MAX(version) > latest_known_version()` → fatal, in `PostgresHubDatabase.apply_migrations`); `docs/guides/shared-stack.md` rewrite.
- **P4 Validation**: two-daemon e2e (`tests/e2e/test_shared_datastores_m0.py`) + manual checklist.

**Correction to the base doc**: its migration number note ("next free after 342") is stale — 343–350 exist; use the next free number at implementation time.

### Deltas (this session's findings, not in the base doc)
`kind: framing`

- **D1 Hub data migration via `gobby pack`** — the doc assumes a fresh hub; Josh moves the laptop's existing data. `unpack` works daemon-free on a Docker host, but: the Postgres leg hard-requires the local managed layout (fine on the desktop — it IS local there), and **`PACK_FILES` ships `machine_id` (`cli/pack.py:41-45`), which would clone the laptop's identity onto the desktop**. Add an unpack step/flag that removes `~/.gobby/machine_id` on restore (regenerates on first start) while keeping `.secret_kek` + `local_cli_token` (wanted — shared user store). Cross-OS (macOS→Linux) volume restore is Docker-portable.
- **D2 Path affinity replaces "identical checkout paths"** — the doc's constraint (two Macs, same paths) is dead: hub is Linux. Repo-touching jobs (wiki crons, code-index) skip when the project root path doesn't exist locally (cheap existence guard in the job executors). `projects.repo_path` stays untouched (#17435 owns path independence).
- **D3 Template-sync discipline** — dev-mode bundled sync (`runner_init/storage.py:204-217`) runs from each daemon's checkout; two dev-mode daemons on different commits flap registry rows (~260 restarts/17d observed). Guard: sync only when the local build version ≥ the registry row's recorded version (newest-wins), or desktop runs a packaged (non-dev) install so only the laptop syncs. Decide at implementation; smallest mechanism wins.
- **D4 Dispatch machine gate** — explicit dispatcher eligibility filter: skip tasks whose artifacts (worktree/clone rows post-P2) belong to another machine; fresh unclaimed tasks are fair game. Complements P2's workspace-claim scoping at the dispatch decision point.
- **D5 Lease-infra fixes (swarm findings)** — `dispatch/workspace_merge.py:668` compares a `datetime` lease against `now.isoformat()` string (TypeError on first real contention — which starts today); `sweep_expired_leases`/`sweep_expired_integration_workspace_leases` are unreachable (no caller passes `startup=True`, `dispatch/dispatcher.py:201-206`) — wire into daemon startup.

### Endpoint facts (verified, for implementers)
`kind: framing`

- Qdrant/Falkor are already remote-capable via config_store: `databases.qdrant.url|api_key|port` (`config/persistence.py:47-65`, URL validator has no loopback restriction), `databases.falkordb.host|port|password` (`:99-124`). Clients: `runner_init/services.py:138-142,158-171`, `memory/vectorstore_client.py:154-164`, `memory/manager.py:215-230`. Postgres is the only hard endpoint blocker (loopback validator, enforced on every load path).
- Readiness gate `runner_service_readiness.py:26-108` is endpoint-based — remote-safe unchanged.
- CLI lifecycle sites needing the remote-mode skip: `cli/daemon.py:82-218,446-450,648-665`, installers (`postgres.py:43-99,306-339`, `qdrant.py:41-159`, `falkor.py:118-315`, `container_restart.py:37-99`), backup (`postgres_backup.py:305-327` loopback+port assertion), pack (`pack.py:562,616-699`). Cleanest seam: the `managed_services` flag in `utils/dependency_requirements.py:214-270`.
- Cron claim mechanics: `storage/cron.py:823-875` (CAS), `scheduler/scheduler.py:155-181` (advisory lock), one-active-per-job partial unique index (`postgres_baseline_schema.sql:1228`). No SKIP LOCKED anywhere; none needed — sweeps are the fix (P2 §2.4).
- `machines` table already has `tailscale_name` + `owner_user_id` (`postgres_baseline_schema.sql:160-174`); upsert exists (`storage/machines.py:90-134`) — P4's checklist notes adding a daemon-boot upsert (today it fires on session create/hook ingress).

### Execution notes
`kind: framing`

- Order: P1 → P2 (2.1 and 3.1 before the laptop points at the shared DB — the doc's own invariant) → D-items alongside their phase → P3 → P4. Fan out with the agent fleet per phase; Josh wants this done today.
- Full-planning gates (enhancement/adversary rounds on the artifact) are available post-approval; Josh may skip them for the deadline — his call.
- Plan #2 (foundation: cleanup → gcore schema authority → crates) remains separate; Decision Record item 8 carries its constraints. Task #19355 (skill-convention edits) sits ready with validated uncommitted edits — commit+close first, before this work begins.

## Verification
`kind: framing`

P4 as specified in the base doc (two-daemon e2e + manual checklist), plus:
- Pack/restore rehearsal on the desktop with row-count spot-checks vs the laptop source; fresh desktop `machine_id` confirmed distinct in `machines`.
- Two-daemon soak: no double-fired cron occurrences; dispatch claims machine-correct; template registry stable across both daemons' restarts.
- Remote functional pass from the laptop over the tailnet: task CRUD, memory recall (vector + graph), gcode search, wiki search, local agent spawn.
- Focused pytest per touched module; `uv run ruff check src/`; `mypy src/`.

---

# Appendix — Database & Codebase Cleanup Audit (evidence base for plan #2)
`kind: framing`

**Analysis only — nothing was modified.** Evidence: read-only psql against the hub DB, gcode/import-graph sweeps by Explore agents, DB-registry verification of every template candidate. Hub DB at time of audit: **8 GB (233 tables, 3.9 GB indexes)**. Pre-0.5.0, so no backward-compatibility obligations.

---

## Part 1 — Database (hub at localhost:60891/gobby)
`kind: framing`

### The 8 GB breaks down as self-inflicted telemetry
`kind: framing`

| Finding | Size | Detail |
|---|---|---|
| `rule_eval` firehose | **2.75 GB** | 9.92M of 10.0M `metrics_events` rows are `rule_eval`; ~368K/day; **99.8% are `result='allow'` no-ops** (720 blocks/day). 30-day retention works — this is steady-state churn. Fix: don't persist (or 1%-sample) allow outcomes. |
| `session_variables` | **648 MB payload** | 9,067 rows, max **29 MB/row** — the `mcp_results` key caches full MCP tool outputs. **100% of payload belongs to expired/deleted sessions** (8,978 expired + 164 deleted vs 2 active). No maintenance loop covers it (spans/comms/attachments have loops in `runner_maintenance.py`; this doesn't). Fix: clear variables on session expiry + one-time purge. |
| `spans` | 984 MB | 7-day retention working (`delete_old_spans`); ~200K rows/day is just high volume. Tuning question, not a bug. |
| `token_events` | 352 MB | Growing since Jan 13, no age retention (per-session delete only). Possibly intentional (usage history) — needs a policy. Its 3 secondary indexes (~84 MB) have **zero scans**. |
| `loop_progress` | 240 MB | 782K rows since Jun 12, no age retention. Old rows look valueless. |
| `step_executions` | **143 MB for 32 rows** | 209 dead tuples, never autovacuumed; ~2.5 MB input/output JSON per old pipeline run (May). Needs payload retention + `VACUUM FULL`. |
| `savings_ledger` | 19 MB, 0 rows | **Dead table** — zero references in src/ or crates/ (only docs/CHANGELOG). Savings moved to daemon/API reporting. Drop it. |
| Leaked pytest schema | 21 MB | `gobby_test_1785317499_29776_master_86b552` — 111 empty tables from a Jul 29 test run. Teardown leaked; add a startup sweep for `gobby_test_*` schemas. |
| BM25 indexes, idx_scan=0 | ~705 MB | `code_content_search_bm25` 387 MB etc. **Verify before acting** — ParadeDB custom scans may not increment `idx_scan`. |
| Recall-experiment tables | ~72 MB | `recall_signal_requests/hits`, `recall_usefulness`, `recall_injection_outcomes`, shadow snapshots — same unbounded pattern, small today. |

Also: code index contains `/private/tmp/gobby-grok-edit-probe.olxVdb` (91 files, still on disk — throwaway probe in the shared index); open review findings on `cleanup_old_metrics` non-atomic aggregate/delete (docs/reviews/mcp_proxy-core.md:200) and unfiltered `reset_metrics`.

### Registry/operational (verified against DB — DB is source of truth)
`kind: framing`

- **7 of 11 registered pipelines never executed:** `dev`, `merge-clone`, `merge-worktree`, `qa`, `spawn-developer`, `spawn-qa`, `wiki-research`. `nightly-fixes`: 3 runs, last 2026-03-19 — abandoned with its `nightly-linter`/`nightly-test-fixer` agents. Only `expand-task` and `review` ran recently.
- **`monolith-enforcement` rules are ENABLED in the DB** despite templates shipping `enabled: false` — the group is live; the template/doc claim is drift, not chaff.
- All 29 `workflow_type='workflow'` rows (`*-steps`) are disabled — likely per-session activation semantics; verify before touching.
- Disabled rules to retire or document: `block-and-teach-context7`, `block-writes-outside-plan-artifact`, `no-destructive-git-interactive`, `no-npx`, `require-memory-review-before-status`.
- **Failing crons (operational, investigate separately):** `gobby:wiki-recap` failed on all 3 projects; `gobby:codewiki-nightly` and `gobby:wiki-sync-sessions` failed for the main project.

---

## Part 2 — Codebase
`kind: framing`

### Functional breakage found during the audit (fix first, all single-file)
`kind: framing`

1. **Red CI gate:** `schemas/diagnose-output.v2.schema.json` drifted from `crates/ghook/schemas/` (missing `local_token_file_present`, `auth_401_remediation`); `schema-mirror-check.yml` sha256-fails any PR touching `schemas/**`.
2. **Secret scanning silently disabled:** `.gitleaks.toml` is 0 bytes (zero rules) while pre-commit still runs gitleaks v8.30.0. Delete the file (restores defaults) or drop the hook.
3. **Dead CodeRabbit config:** `.github/coderabbit.yaml` — unread duplicate whose 7 path rules match zero files (pre-`src/gobby/` layout).

### Dead Python (import-graph verified, zero production references)
`kind: framing`

- **10 whole files, ~700 lines (high confidence):** `utils/mathutil2.py`, `cli/pipelines_runtime.py` (abandoned extraction, duplicates 4 helpers), `code_index/prune_storage.py` + `servers/routes/stage_routes.py` (compat re-export shims), `servers/routes/stages.py` (router with zero routes, never mounted), `workflows/task_actions.py`, `workflows/summary_actions.py` (tested only by a test of the shim itself), `postgres_pgsearch_assets.py`, `skills/injector.py` (~260-line skill-selection engine, never invoked), `plans/convergence_regression.py` (test harness living in shipped package).
- **Unreachable memory-dream merge subsystem (~500 lines):** sole producer passes `duplicate_groups=[]` (`memory/dream/orchestrator.py:433`) → `duplicates.py`, planner merge actions, `apply.py` `_merge`/`_supersede` handlers, `DuplicateGroup` model all dead. Plus ~250 lines of `_legacy` fallbacks in `apply.py` gated on `hasattr(db, "transaction")` — always true in production; kept alive only by test fakes.
- **SQLite-removal residue:** `gobby postgres activate` cutover ritual (~230 lines incl. pgaudit probes, cutover tickets, flags that only raise; references nonexistent `docs/runbooks/`); `hub_backend: Literal["postgres"]` single-valued selector; **`bootstrap.yaml` `database_path` key read by nothing** (Rust reads only `hub_backend` + `database_url`); FTS5 alias indirection in `search/` (`tasks_fts` → real table mapping); `db: HubDatabase | HubDatabase` degenerate unions (3 sites). Note: the `migrate-from-sqlite` command CLAUDE.md mentions **does not exist** — CLAUDE.md itself is stale there.
- **No-op CLI/API surface:** unregistered `build stop`/`build resume` click commands (`cli/build.py:422-435`); dead `export_import` group; `tasks close --skip-validation/--force` flags discarded (`_ = (skip_validation, force)`); MCP handoff `full` param ignored; HTTP `rebuild_graph?limit=` deprecated-and-ignored; `claude_code.py` docstring documents a `set_legacy_mode` that doesn't exist.
- **Dead config (~40 fields):** entire `ContextInjectionConfig`; 7 of 8 `BuildConfig` fields parsed-validated-discarded (making `build.yaml` near-inert); `TaskExpansionConfig` research knob set; `CommunicationsConfig.inbound_enabled/outbound_enabled` gating nothing; 8 `DaemonConfig.get_*_config()` accessors with zero production callers (alive only via their own tests). Code-index config fields flagged low-confidence — **check `crates/gcode/src/config.rs` before removing** (Rust may consume them).
- **~15 legacy-migration paths for pre-0.5.0 states no user can be in** (`_migrate_legacy_config`, `wiki_migration.py`, droid/qwen legacy hook cleanup, machine-id secret migration, …). Product call: removable pre-release.
- **Duplicate utilities:** `truncate_tool_brief` byte-identical ×2 (+shim); `_daemon_error_message` ×4; `get_project_path` ×3; **`_sanitize_url` ×2 with different redaction semantics — security-relevant, consolidate first**; 3 pairs of retry/Retry-After helpers; two full webhook stacks (httpx `WebhookDispatcher` vs aiohttp `WebhookExecutor`), both wired — consolidation debt.

### Dead Rust & web
`kind: framing`

- `crates/gwiki/src/ingest/mediawiki.rs` (157 lines) — zero callers. `wayback.rs` (550 lines) — reachable only from a test; 23 `#[allow(dead_code)]`.
- Unused deps — Rust: `ignore`+`dirs` (gcode), `dirs` (ghook — the size-optimized binary), `postgres-types` (gcore), `linked-hash-map` (gwiki); `gobby-core` declared twice in gwiki; `ureq` in gcode serves one file. Python: `tomli-w`, `opentelemetry-instrumentation-logging`, `opentelemetry-semantic-conventions`, `pygments` (dev-only CVE pin shipped to prod), `anthropic` (instrumentor patches an SDK nothing calls). Web: `recharts`, `react-arborist`, `@dagrejs/dagre` + 2 misplaced `@types/*`.
- **`chatterbox-tts` non-optional for one file** (`voice/tts_chatterbox.py`) — drags the Torch stack and causes 9 `[tool.uv]` pin overrides; `voice = []` extra is a no-op.
- 6 orphan web modules (per-export verified, ~21 KB): `useAgentSpawn.ts`, `useSessionTokenEvents.ts`, `useAgentRuns.ts`, `isolationColors.ts`, `Textarea.tsx`, `useVoiceCapabilities.ts`.

### File-size ceiling (rule 2)
`kind: framing`

- **3 hard Rust violations** (>1,000 non-test lines): `gcode/commands/status/prune.rs` (1,066), `gcode/codewiki/build_parts/curated_content.rs` (1,221), `gcode/codewiki/types.rs` (1,029). 7 more over 1,000 raw but under after stripping inline `#[cfg(test)]` — the rule needs a policy decision + an actual linter (none implements the check today).
- Python: zero violations, but 9 files at 970–999 (`config/validation_detection.py` = 999) are forced-decomposition landmines on next edit.

### Templates / docs
`kind: framing`

- `tag:sync` dead selector in 23 of 28 bundled agents (no rule carries the tag).
- `workflows/rules/CLAUDE.md` materially wrong: nonexistent `messaging` group + `deprecated/` dir, 4 real groups omitted, 12/22 counts wrong, cites tombstoned `pipeline-worker.yaml`.
- `skills/gcode/SKILL.md` — stale generated copy describing retired SQLite FTS5; source asset is current.
- `detection/gemini.toml` uses retired slug `gemini` (installers/adapters are `agy`); `grok`/`agy` lack detection profiles.
- Stale/orphan files: `survey.json` (committed review scratch), empty `.gitattributes`, diverged older `scripts/setup-firewall.sh` fork, completed one-shot `scripts/migrate_index_to_plans_table.py`, `docs/guides/web-ui.md` → removed `artifacts.md`, `release-guide.md` pins ghook 0.7.2 (crate is 0.7.3), `docs/architecture/*` stamped 2026-06-11 claiming Python 3.13+ vs pre-commit's 3.14, guides index missing 3 guides, 5 two-line "waived" review stubs, 39 raw dumps in `docs/evidence/wiki-parity-2026-06/` (keep summaries).
- `memory/digest_update` prompt: enabled in DB (auto-synced) but zero code consumers.

### Test suite & suppressed debt (informational)
`kind: framing`

784K test lines; 13 `_extra`/`_coverage`/`_v2` sibling clusters; `lifecycle_monitor` at 9:1 test:prod (8,145 lines for 903). TODO debt is a non-finding (3 markers in 2,689 files); real debt: 165 Rust `#[allow]` (gwiki ingest concentrated), 40 `pragma: no cover`, 41 `as any` in one web test file.

### Verified clean (don't re-litigate)
`kind: framing`

Bundled manifest 100% in sync (453 entries); all 51 template tool references and 72 skills resolve; no commented-out Rust; no orphan bin targets; CI scripts all resolve; `docs/contracts/plan-coverage.md` load-bearing; comms adapters/voice providers/SSE transport are string-loaded and alive; `web/dist-setup/cli.mjs` intentional.

---

## Part 3 — Data-model usage audit (tables × fields × code × runtime)
`kind: framing`

Method: every public table/column cross-referenced against tokens in src/gobby (1,689 py), crates (705 rs), web/src (742 ts/tsx), scripts, tests, migrations; plus `pg_stat_user_tables` write/read counters (see stats-window correction in Part 4).

### Active bug found: migration slot 346 was hijacked — cron display_name is broken NOW
`kind: framing`

- Abandoned tmux-input-arbiter WIP (wiki sessions ~Jul 25–28) created `tmux_input_requests` + `tmux_input_pane_states` and consumed `schema_migrations` version **346**. The code (`storage/tmux_input.py`, `agents/tmux/input_arbiter.py`, its migration) no longer exists in the repo.
- Repo's real `346_cron_display_name.sql` (`ALTER TABLE cron_jobs ADD COLUMN display_name`) is therefore marked applied but **never ran** — live `cron_jobs` has no `display_name` column, while `storage/cron_display.py`, `/api/cron/jobs*`, MCP `update_cron_job`, and CronTab (contract #19160, commit 5f8450504) all use it.
- Fixes: apply the missing ALTER manually or re-run 346; drop the 2 orphan tmux tables; **run a full schema diff (fresh DB from migrations vs live) to catch any other WIP contamination**; consider filename-aware migration bookkeeping to prevent slot hijacking.

### Table-level census (121 public tables)
`kind: framing`

- **Migration-only (no code/test references):** `savings_ledger` (also 0 rows), `tmux_input_requests`, `tmux_input_pane_states` (orphaned WIP, above). 3 drop candidates.
- **Never written in the DB's lifetime (0 ins/upd/del) but code-referenced — 27 tables.** Groups:
  - *Features built, never used on this install:* `build_profiles`, `chat_attachments`, `checkpoints`, `clones`, `comms_routing_rules`, `gh_issues_triaged`, `gh_triage_build_dispatches`, `gh_triage_deliveries`, `merge_conflicts`, `merge_resolutions`, `pending_interactions`, `task_comments`, `task_delivery_campaigns`, `task_delivery_units`, `tool_embeddings`, `tool_schema_hashes`, `session_stop_signals`, `project_lifecycle_events`, `integration_workspace_mutex`, `recall_gate_runs`, `recall_holdout_consumed`, `recall_shadow_audit_verdicts`, `workflow_states`, `session_memories`. Product call per table: dead feature vs not-yet-used.
  - *Hot-path polls of forever-empty tables:* `rule_overrides` (665K reads, 0 writes ever), `code_index_projection_cleanup_pending` (301K reads), `gh_triage_deliveries` (40K reads), `build_profiles` (3K), `clones` (7K). Wasted daemon queries; also the strongest "dead feature" signals.
  - *Infra sentinel:* `_pgaudit_probe` — Docker pgAudit seed from the postgres cutover ritual (pairs with dead-code finding: `gobby postgres activate`).
- **Everything else is code-referenced and runtime-active.** No test-only tables.

### Field-level census — clean; 15 dead columns across 6 tables
`kind: framing`

Strict pass (column token absent from every file that references its table):
- `tasks.assignee` — dead in the 47-column core table (441 referencing files, zero mention).
- `task_artifacts`: `last_reviewed_plan_hash`, `plan_review_attempts`, `qa_attempts`, `epic_qa_attempts`, `merge_attempts`.
- `workflow_states`: `workflow_name`, `step`, `step_entered_at`, `step_action_count`, `total_action_count`, `context_injected` — 6 of its columns dead + table never written → drop the whole table.
- `session_memories`: `memory_id`, `action` (+ table never written) → drop candidate.
- `tool_embeddings.text_hash`; `inter_session_messages.read_at`.

### Ownership map for the gcore-authoritative move
`kind: framing`

- **Rust already SQL-owns (5):** `gwiki_chunks`, `gwiki_documents`, `gwiki_ingestions`, `gwiki_links`, `gwiki_sources`.
- **Shared Python+Rust SQL (10):** `code_calls`, `code_content_chunks`, `code_imports`, `code_indexed_files`, `code_indexed_projects`, `code_symbols`, `config_store`, `projects`, `secret_key_material`, `secrets`.
- **Rust touches without direct SQL (token only):** `checkpoints`, `clones`, `memories`, `plans`, `prompts`, `schema_migrations`, `session_memories`, `sessions`, `skills`, `spans`, `tasks`, `tools`, `worktrees`.
- **Python-only: the remaining ~90 tables.** Migration ordering suggestion: gcore first formalizes what Rust already touches (code index + gwiki + config/projects/secrets), then the core domain (tasks/sessions/memories), then telemetry last — after the retention redesign in Part 1, so the noisy tables aren't ported as-is.
- Caveat: token/SQL-proximity matching; dynamic SQL could hide usage (the tmux tables proved runtime stats catch that — all flagged tables were cross-checked against write counters).

## Part 4 — Inflow investigation: why the never-written tables are never written
`kind: framing`

Verdict taxonomy: (a) inflow broken · (b) trigger never fires (healthy, unused) · (c) writer unreachable/dead · (d) no writer exists.

### Memory/recall domain (agent complete)
`kind: framing`

- **`session_memories` → (d) NO WRITER EXISTS.** Junction table from the original Dec-2025 memory design; migration shipped, writer never did. Superseded by `memories.source_session_id` (1,525 rows use it; `summary_context.py:331` queries it directly). The 580 "reads" are FK `ON DELETE CASCADE` scans (578 parent deletes ≈ 579 idx_scans). **Confirmed drop: table + both FKs.**
- **`recall_shadow_audit_verdicts` → (b).** Writer healthy (`storage/recall_shadow_signals.py:403`); only caller is the interactive CLI `gobby memory recall-signals audit-labels --record-agreement` — a 50-prompt human review loop never run. Cohort is ready (1,397 eligible requests).
- **`recall_gate_runs` → (b), blocked two levels up.** Only caller is the manual `gate` CLI; no production/daemon caller (drift monitor never calls it, and it idles anyway — static constants active for all rows). Even if run, `audit.ok` hard-fails closed because the audit table above is empty, returning before the INSERT.
- **`recall_holdout_consumed` → (b).** Same chain + FK RESTRICT on `recall_gate_runs` — structurally can't be non-empty first.
- **Root cause in one line:** recall's *collection* arm is fully live; the *ship-gate* arm has never been exercised because it requires a human 50-verdict audit nobody has run.
- **RECLASSIFIED per Josh (2026-07-30): the recall tables implement an academic-paper hypothesis test and data collection is ongoing.** They are research infrastructure — exempt from chaff cleanup and from any retention policy that would destroy experiment data. The empty gate tables mean the evaluation step hasn't run yet; the unblock is the 50-verdict audit (cohort ready: 1,397 eligible). The synthetic-verdict fallback at `recall_refit.py:817-826` matters more in this light — it would substitute a machine label into the experiment's human-oversight step; fix before running the audit.
- **Incidental findings:** (1) stale `config_store` row `memory.digest_memory_usefulness=true` — field removed from MemoryConfig, nothing reads it; (2) latent hazard at `memory/recall_refit.py:817-826` — on stale prompt_hash the audit fallback fabricates a synthetic "human" verdict from the judge's own label (fails closed today, wrong in principle); (3) **two MORE leaked pytest schemas** (`gobby_test_1785399531_17365_master_7fa4d9`, `gobby_test_1785406025_75102_master_b23f3f`) — 3 total now, strengthening the startup-sweep recommendation.
- Log evidence: zero errors/tracebacks for any of the four tables — no broken-writer signature.

### GitHub triage domain (agent complete)
`kind: framing`

- **All 3 tables → (b) TRIGGER NEVER FIRES.** Config row for project gobby: `sync_enabled=t, triage_enabled=f, webhook_enabled=f, repositories=[], webhook_secret_ref=NULL`; other 8 projects have no row (defaults all-false). GitHub token/MCP server present and working — this is config-off, no broken writer. Turn-on: `gobby github setup --project gobby --repo GobbyAI/gobby --triage` (+ webhook secret + public URL for deliveries).
- **Secondary (c): delivery retry sweeper is orphaned** — `register_github_triage_cron` (`github_triage/cron.py:71`) has zero production callers; `cron_jobs` has no triage row. Superseded by `ExternalIssueSyncCoordinator`, left dead. The recovery path stays dead even if triage is enabled — genuine wiring gap.
- **The 39.6K reads explained:** `ExternalIssueSyncCoordinator` (started because the github MCP server is enabled; 5s refresh, 30s github cadence) runs `COUNT(*)` twice per cycle against the permanently empty deliveries table, and hammers `project_github_triage_configs` to **986K scans** polling all 9 projects every 5s.
- **LIVE BUG (broken outflow, zero log output):** `external_issue_sync_status` for gobby: `state=degraded, consecutive_failures=34, last_error="GitHub reconciliation reported 4 failures"` — `push_linked_tasks` fails every cycle silently (outbound errors:4, pushed:0). Needs a task.
- **Missing index:** `gh_triage_build_dispatches.task_id` FK (ON DELETE CASCADE) has no index → full seq scan per task delete (117 observed). Free while empty; O(n) the moment it isn't.
- The tables' other "reads" are FK-cascade probes from task/project deletes — same pattern as session_memories.
- Log window 2026-07-17→07-30: zero triage/github errors (nothing executes). One unrelated: github MCP health-check failure Jul 28.

### ⚠ Stats-window correction (task/session agent)
`kind: framing`

**The "lifetime counters" premise was wrong.** PostgreSQL 18 discards cumulative stats on unclean shutdown WITHOUT setting `stats_reset`; postmaster started 2026-07-26 — the counters cover ~4 days. "Never written" in Parts 3–4 means "not written in the stats window." Verdicts based on code paths, config state, or actual row counts stand (session_memories no-writer, savings_ledger zero-refs, recall gates empty-by-row-count, GH triage config-off). Any verdict resting purely on zero tuple-writes needs the row-count check. Five of the six task/session tables actually have historical rows.

### Task/session domain (agent complete)
`kind: framing`

- **`task_delivery_campaigns` → (a) INFLOW BYPASSED — real bug.** 64 rows exist (May–Jun). `record_campaign` (`storage/delivery.py:111`) fires on every branch of MCP `record_merge_result` (`_stage_ops.py:604`), but `complete_stage` (`_stage_ops.py:274`) is separately agent-callable, accepts `stage_name='merge'`, and touches no delivery state. Merge-stage completions with campaign coverage: 42% (wk of May 18) → 4% (May 25) → **0% since Jun 8 (33 merges, incl. #18600–#18603 on Jul 23, no failed-call log lines)**. Fix: single choke point for merge completion, or make `complete_stage('merge')` write the campaign. Next diagnostic: `git log -S record_merge_result` over the May 18–25 window + merge-worker/orchestrator template diffs.
- **`pending_interactions` → (b) design drift.** Built for durable restart-safe approvals; the live web-chat path now uses in-memory `asyncio.Event`s (`chat_session_permissions.py:708-745`) — approvals are no longer restart-safe. All 40 rows are one Apr-16 codex CLI-bridge session. Re-wire or delete table + startup/rebroadcast machinery.
- **`session_stop_signals` → (b) + stale docs.** 0 rows ever. Wiring intact (HTTP POST + WS `stop_request`), but `stop_registry.py:43-47` advertises a CLI command and MCP tool that don't exist and misnames the WS type. Also `docs/reviews/orchestration-infra.md:200`: nothing calls `acknowledge` — any row would stick forever.
- **`task_comments`, `task_delivery_units`, `project_lifecycle_events` → (b), healthy.** Comments: 269 system rows from epic-QA path (HTTP route unexercised; Holistic→Epic rename reset the exact-body dedupe key — note). Units: hard-gated on `delivery_mode='pull_request'`; all campaigns are `auto`. Lifecycle events: only records `build_stop`/`build_resume`; correctly idle since Jun 11.

### Proxy/infra domain (agent complete)
`kind: framing`

- Window start independently pinned to **~2026-07-13** (task/session walk-back). Method rule going forward: check `count(*)`, `max(updated_at)`, and identity-sequence `last_value` before trusting `n_tup_ins`.
- **`rule_overrides` → (d) NO WRITER EXISTS.** Session-scoped overrides were specced (`workflows-v2.md:993-1016`: `POST /api/rules/:name/overrides`, `toggle_rule(session_id=)`), table + read path shipped, writer never built — `toggle_rule` everywhere mutates the global definition row. Costs **666K wasted probes** (`engine/core.py:292,502-511`, one SELECT per rule eval). Drop table + read step, or build the writer.
- **`workflow_states` → (d) frozen relic with a correctness issue.** 138 rows, writes stopped 2026-03-02; superseded by `workflow_instances` + `session_variables`; `DROP TABLE` already planned (`split-workflow-definition-storage.md:1754`). But `get_claimed_task_owners` (`cli/tasks/_utils/claims.py:13-40`) still resolves claims by JOINing March data — returns nothing only because no March session is still active. Rewrite onto session_variables, then drop.
- **`comms_routing_rules` → (c) WRITER UNREACHABLE — highest-value (c).** Full CRUD storage layer exists (`storage/communications.py:443-475`); no HTTP route, CLI command, MCP tool, or UI calls it. Comms IS configured (2 channels, 135 messages) → **event routing to Slack/Telegram is a permanent silent no-op** while `docs/guides/comm-integrations.md:385` documents it as working. Expose CRUD or delete the rule path.
- **`tool_embeddings` → (d), provably never held a row EVER** (identity sequence `last_value` NULL — reset-proof). The live feature uses the Qdrant collection of the same name; the PG table is pure FK-cascade tax (624 probes). Drop with `text_hash`.
- **`code_index_projection_cleanup_pending` → (b) + latent bug.** Only written when `gcode graph_clear` fails during explicit invalidate (never has). Bug: vector branch (`context.py:188,204`) reports `pending_retry=True` but never enqueues the marker (graph branch does) → failed vector clears would never retry. Plus 300K wasted unconditional DELETEs (`sync_worker.py:412-433,501-511`).
- **`chat_attachments`, `tool_schema_hashes` → (b) healthy** (2 real uploads May 27, fully wired; 451 hash rows from manual `POST /api/mcp/refresh`).

### Build/merge domain (agent complete)
`kind: framing`

- All six → **(b) TRIGGER NEVER FIRES**, writers wired and healthy. `build_profiles`: 5 bundled rows, hash-skip working (reads = startup sync × **~260 daemon restarts in 17 days** — ops signal in itself). `checkpoints`: 1 row (Jun 11); trigger requires provider-stall/doom-loop kill + dirty worktree. `clones`: **0 of 14,423 tasks ever chose `isolation='clone'`**; reads are the web UI polling `/api/source-control/clones` every 5s + hourly reaper. `merge_resolutions`/`merge_conflicts`: the gobby-merge AI-conflict subsystem (separate from `merge_worktree`/`merge_clone` tools, which by design don't touch these tables); unused since Mar 27. `integration_workspace_mutex`: gate needs integration workspace/clone artifacts that essentially never exist (1 ever).
- **Attached defects:** (1) `sweep_expired_leases` + `sweep_expired_integration_workspace_leases` are unreachable — no production caller passes `startup=True` (`dispatcher.py:201`); a crashed merge lease would never be swept. (2) `LocalCheckpointManager.delete_old` has zero callers and nothing ever reads checkpoints back — write-only feature, no retention. (3) Latent TypeError at `dispatch/workspace_merge.py:668`: compares `datetime` lease against `now.isoformat()` string — fires the first time a live lease is contended.

### Swarm synthesis (all 5 domains)
`kind: framing`

Of 27 investigated tables: **1 × (a) broken inflow** (`task_delivery_campaigns` merge bypass — real bug), **2 × (c)** (comms routing rules unreachable; gh-triage retry cron orphaned), **4 × (d) no writer** (`session_memories`, `rule_overrides`, `workflow_states`, `tool_embeddings` — all confirmed drops), rest **(b)** healthy-but-unused (config-off, narrow triggers, or genuinely idle features). Cross-cutting: ~967K wasted hot-path reads/window on empty tables (rule_overrides 666K + projection_cleanup 300K), 4 latent bugs (vector-cleanup enqueue, lease sweep unreachable, lease TypeError, delivery bypass), and the pg_stat method caveat.

## Prioritized roadmap (if/when cleanup is approved — nothing executed now)
`kind: framing`

1. **Breakage (same day):** fix schema mirror; resolve gitleaks config; delete `.github/coderabbit.yaml`.
2. **DB quick wins (~1.2 GB + hygiene):** purge `session_variables` for expired/deleted sessions (648 MB); drop leaked `gobby_test_*` schemas (3 known); drop `savings_ledger`; `VACUUM FULL` `step_executions` + add payload retention.
3. **DB policy (biggest ongoing win, ~2.5 GB steady-state):** stop persisting `allow` rule_evals (or sample); clear session variables on expiry; add retention for `loop_progress`; decide `token_events` policy; drop its 3 unused indexes; add `gobby_test_*` schema sweep to startup.
4. **Dead code:** Tier-1 Python files (~700 lines), memory-dream merge chain (~500), `postgres activate` ritual (~230), mediawiki/wayback Rust (~700), 6 web orphans, no-op CLI flags/params.
5. **Deps:** remove 5 Python + 5 Rust + 3 web dead deps; move `pygments`/`@types/*` to dev; make `chatterbox-tts` a real optional extra.
6. **Consolidation:** `_sanitize_url` first (divergent redaction = leak risk), then `_daemon_error_message`/`get_project_path`/`truncate_tool_brief`, webhook stacks.
7. **Templates/docs:** retire never-run pipelines + nightly agents (DB-verified safe); fix rules CLAUDE.md; regenerate `skills/gcode/SKILL.md`; `gemini`→`agy` detection; doc fixes; delete scratch files.
8. **Policy decisions needed from Josh:** Rust inline-test line-count reading (3 hard violations either way); token_events retention; pre-0.5.0 migration-path removal; BM25 idx_scan verification before touching indexes.

## Verification approach for any cleanup
`kind: framing`

Row-count snapshots before/after purges (transactional); `EXPLAIN` FTS queries before index drops; `uv run ruff check` + targeted pytest per touched module; `cargo check` per crate after dep removal; `npm run build` for web; re-run `schema-mirror-check` and pre-commit after config fixes; watch `pg_stat_user_tables` for a week after retention changes.
