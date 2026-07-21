# Orphaned Derived-State Reconciliation (gcode, gwiki, project purge)

**Plan ID:** orphaned-derived-state-reconciliation

## Overview

`kind: framing`

Derived vector/graph state leaks across Gobby's per-project stores. Verified live (2026-07-18): 81 orphaned `code_symbols_<uuid>` Qdrant collections (~2.38 GB allocated) against 15 registered `code_indexed_projects` rows; the leak's root cause is prune's stale-project phase calling `indexer::invalidate` (`crates/gcode/src/index/indexer/lifecycle.rs:86`), which deletes all SQL rows but never the Qdrant collection — and once the parent row is gone, child-row-driven orphan discovery (`collect_orphan_project_ids`, `crates/gcode/src/commands/status/prune.rs:203`) can never find it. Additionally: the hourly cron records failures as successes (plain-string result coerced to `completed`); gwiki has per-scope collections with no registry, no reconciler, and no deletion hook (3 of 7 project scopes are dead); and project soft-delete is terminal — nothing ever reclaims a soft-deleted project's state.

This epic adds global Qdrant reconciliation to `gcode prune --force`, honest cron result semantics, a `gwiki prune` reconciler with its own hourly system job, and a full-hard-delete project purge lifecycle (30-day retention, no tombstone) with a manual `gobby projects purge` command, then performs one-time rollout cleanup.

## Constraints

`kind: framing`

- Preserve the existing `gobby:code-index-prune` job identity. Registration is name-keyed (`register_code_index_prune_cron`, `src/gobby/code_index/prune.py:288`) and `reconcile_system_job_definition` (`src/gobby/storage/cron.py:550`) updates descriptions in place — ID `b049d115-c932-5c31-9d0c-8f3f89f67117`, cadence, and ownership are preserved. **Known defect in scope (2.1)**: the current registration re-enables operator-disabled jobs (`toggle_job` on any disabled reconciled row); the fix preserves the operator's enabled toggle and only wakes enabled rows whose `next_run_at` is null.
- Protected projects are defined by `SYSTEM_PROJECT_NAMES` (`src/gobby/storage/projects.py:22`): `_orphaned`, `_migrated`, `_personal`, `_global`, **and `gobby`**. Protection is currently enforced only in CLI/HTTP callers; the purge service must enforce `LocalProjectManager.is_protected` internally and the retention query must exclude these names as defense in depth.
- The hub `projects` table is **not** an authority for the code index: 12 of the 15 active `code_indexed_projects` rows are worktree/clone indexes with no `projects` row (verified live). Root-path staleness stays unchanged; purged projects' code indexes are cleaned explicitly by the purge service. The `projects` table **is** the authority for `gwiki_project_*` scopes (wiki ingest is always daemon-mediated).
- Automated deletion uses a strict `code_symbols_<canonical-UUID>` filter. Non-matching `code_symbols_*` names are reported, never deleted. `memories`, `tool_embeddings`, `gobby_github_issues`, and `gwiki_*` families are untouched by the gcode reconciler.
- Cron failure semantics: `FAILURE_RESULT_STATUSES` (`src/gobby/scheduler/executor.py:28`) lacks `timed_out`, so handler mappings must set `success: false` explicitly; do not rely on `status` alone.
- No database migration. FK graph verified: 23 tables referencing `projects` are already `ON DELETE CASCADE`; only `tasks`, `plans`, `sessions`, `memories` are `NO ACTION` (purge deletes them explicitly); `comms_identities` is `SET NULL`.
- Wiki writes are **not** daemon-only, so an in-process Python fence alone cannot close the purge/writer race: the standalone gwiki CLI exposes project-scoped mutation commands (`index`, `collect`, `ingest-file`, `ingest-url`, `sync-sessions`, and every other persistent-writer arm of the `commands::run` dispatch; `crates/gwiki/src/cli.rs:118`) that resolve a project ID straight from the on-disk project root with no liveness admission (`resolve_project_from_root`, `crates/gwiki/src/scope.rs:131`) and write SQL, Qdrant, and Falkor directly (`crates/gwiki/src/commands/index.rs:112`; `session_sync::execute` likewise writes the Postgres store then syncs Qdrant and Falkor, `crates/gwiki/src/commands/session_sync.rs:42`). This epic therefore adds a **cross-process per-project gwiki writer lock** (3.2, Postgres advisory lock in its own keyspace): every project-scoped gwiki mutation, the prune reconciler's per-scope removal, and the Python purge service acquire it, with project liveness rechecked under the lock. **Project purge additionally carries the writer-quiescence contract (4.2)** — it targets a soft-deleted row that still exists, and soft deletion does not quiesce already-registered per-project wiki crons (nor does `LocalProjectManager.get` filter deleted rows, `src/gobby/storage/projects.py:198`): disable the project's writer crons (the wiki family **and** codewiki-nightly), drain in-flight runs while their run records still exist, delete the jobs only after the drain, guard handlers against deleted projects, and fence all in-daemon derived writers (memory vector/graph, tool embeddings, GitHub issue index, embedding-switch rebuilds, daemon-mediated gwiki ingest) before projection cleanup. The embedding switch is **not currently an in-daemon writer** — `gobby embeddings switch start`/`resume` run the switch runner in the standalone CLI process (`src/gobby/cli/embeddings.py:152,184`) — and `--abort` mutates the switch journal standalone too (`src/gobby/cli/embeddings.py:136`) — so 4.2 moves the **complete mutating switch lifecycle** (start, resume, abort) into the daemon (the CLI becomes a thin client of daemon endpoints, every standalone mutation path deleted). **Known defect in scope (4.2)**: the switch journal key `ai.embeddings.switch_run` is rejected by the real ConfigStore's key validation (`src/gobby/storage/config_store.py:66`), so the standalone switch cannot persist its journal in production at all — 4.2 defines the internal lifecycle-key contract (write-side admission **and** read-side omission: `load_config` feeds the full config dump through the same rejecting key mapper at `src/gobby/config/app.py:623`, so an admitted-but-visible journal would crash runtime config loading and leak into export/templates) that fixes this. `gobby install` also writes canonical embedding config standalone (`src/gobby/cli/installers/embedding.py:426`); 4.2 confines that to proven first bootstrap. After all of this, the only out-of-daemon derived writers are gwiki (3.2 lock) and gcode (1.1/1.3 lock).
- Full hard delete on purge — no tombstone row remains. Pre-0.5.0: no backward compatibility.
- Topic lifecycle is fully manual (`gwiki purge --topic`). Automated topic-collection deletion was considered and **rejected**: a live topic ingest can create the collection between the zero-row check and the delete, and the 3.2 writer lock is project-scoped — topic scopes remain unlocked, so that race stands. Automation touches `gwiki_project_*` scopes only.
- Out of scope: Qdrant optimizer/segment tuning; topic TTL; `gobby_test_*` schema cleanup (already self-healing via `_cleanup_orphaned_schemas`, `tests/fixtures/postgres.py:84`, 24h threshold, runs each pytest session).

## P1: gcode Global Qdrant Reconciliation

`kind: framing`

**Goal**: `gcode prune --force` reconciles every `code_symbols_<uuid>` Qdrant collection against `code_indexed_projects`, safely and observably, with zero registered projects supported.

### 1.1 Add project-identity advisory lock entry point [category: code]

`kind: deliverable`

Target: `crates/gcode/src/index_lock.rs`

Add a crate-visible entry point that acquires the project advisory lock from `(database_url, project_id)` without constructing a full project `Context`. Match the module's existing visibility and error model — `IndexLockPolicy` is `pub(crate)` (`index_lock.rs:25`), `ProjectIndexLock` is a private guard (`:208`), and there is no dedicated error type:

```rust
pub(crate) fn lock_project_by_id(
    database_url: &str,
    project_id: &str,
    policy: IndexLockPolicy,
) -> anyhow::Result<Option<ProjectIndexLock>>
```

- Raise `ProjectIndexLock` to `pub(crate)` so `prune.rs` (same crate, different module) can hold the guard. Do not invent a new error enum; use the module's existing `anyhow`-style error flow.
- Reuse the existing key derivation `project_lock_key` (`index_lock.rs:186`, `SHA-256("gcode:index:" || project_id)` → first 8 bytes as big-endian `i64`) and `pg_try_advisory_lock` polling (`try_advisory_lock_until`, `:146`). Do not add a second locking scheme.
- Return `Ok(None)` when the lock is busy after the policy's total wait (caller counts it as deferred), `Ok(Some(lock))` on acquisition (released on `Drop` as today, `:215`).
- Add a maintenance policy constant reusing the `BriefTry` shape: 150 ms total wait, 25 ms poll — identical to `brief_freshness_try` (`:36`); either reuse it directly or add a named `maintenance_try` alias.
- The test module's `context_for(database_url, project_id)` helper (`:252`) shows the minimal-context pattern; the new entry point should make that pattern production-legal rather than duplicating `Context` construction.

**Acceptance:**

- 1.1.1 - A public lock entry point taking `(database_url, project_id, policy)` exists and reuses `project_lock_key` and the existing polling acquire path. file: `crates/gcode/src/index_lock.rs`.
- 1.1.2 - Busy locks return a deferred signal (`Ok(None)`) after a 150 ms maintenance attempt instead of erroring. behavior: "busy advisory lock defers, does not fail" in `crates/gcode/src/index_lock.rs`.

### 1.2 Global Qdrant collection reconciliation phase in prune [category: code] (depends: 1.1, 1.3)

`kind: deliverable`

Targets: `crates/gcode/src/commands/status/prune.rs`, `crates/gcode/src/vector/code_symbols/qdrant.rs`, `crates/gcode/src/graph/code_graph/write/deletion.rs`

Restructure `prune()` (`prune.rs:108`) into **discovery → authorization → mutation** so one combined prompt precedes every destructive action. Today `prune_stale_projects` (`prune.rs:139`) prompts and immediately invalidates — same-run collection orphans only become knowable *after* that SQL deletion, so a combined pre-mutation prompt is impossible in the current order. The collection phase is global-scope only (skipped under `--project`; project-scoped prune stays limited to its selected project):

1. **Discovery (zero mutations)**: collect projects and staleness exactly as today; resolve Qdrant **and Falkor** configuration independently of any project context (`resolve_qdrant_config` / `resolve_falkordb_config`, pattern: `crates/gcode/src/commands/setup.rs:163` and `Context::resolve_for_project_id_with_services`, `crates/gcode/src/config/context.rs:305`) so reconciliation works with zero registered projects. Missing Qdrant config → the collection phase reports a successful skip while stale-project pruning still runs; missing Falkor config → the graph-scope phase reports a successful skip symmetrically. Enumerate collections once via the existing listing path used by `delete_code_symbol_collections_with_prefix` / `parse_collection_names` (`crates/gcode/src/vector/code_symbols/qdrant.rs:165,242`); accept only `code_symbols_<canonical-UUID>` (lowercase hex, 8-4-4-4-12), counting other `code_symbols_*` names as `invalid` (reported, never deleted) and ignoring every other family. **When Falkor is configured, also enumerate graph scopes here, in discovery**: the code graph is one shared graph whose nodes carry a `project` property (`clear_project_query`, `crates/gcode/src/graph/code_graph/write/deletion.rs:191`), so read the distinct `project` values from the graph itself — discovery must not depend on SQL anchors, because the shared invalidation path (1.3) deletes SQL while Falkor is unconfigured, and a scope leaked that way must become reconcilable as soon as configuration returns. Authority set: `SELECT id FROM code_indexed_projects`. Compute **existing orphans** (UUID collections with no row), **would-be orphans** (collections belonging to the stale projects about to be removed — knowable pre-mutation because their rows still exist), and — Falkor configured — **existing graph orphans** (graph scopes with no row) and **would-be graph orphans** (scopes of the stale projects). PostgreSQL or Qdrant enumeration failure, or **configured-but-unreachable Falkor** (connection or scope-enumeration failure) → fail **before** any mutation, stale phase included — never discover a backend's unreachability after another backend has already mutated.
2. **Authorization**: when not `--force` and anything destructive is pending (stale projects, orphan collections, and/or orphan graph scopes), present **one** combined prompt listing all of them; declining aborts the run with zero mutations — no stale rows deleted, no collections deleted, no graph scopes cleared. A graph-only run (orphan graph scopes with no stale projects and no orphan collections) prompts the same way. `--force` skips the prompt entirely.
3. **Mutation — stale phase is a full per-project guarded sequence, not a bare SQL invalidate**: today `prune_stale_projects` (`prune.rs:174`) calls `indexer::invalidate` with no lock — while indexing holds the project advisory lock around its SQL and projection writes (`crates/gcode/src/commands/index.rs:62`), so an unlocked stale delete can interleave with an active indexer — and it strands the project's Falkor projection: once the `code_indexed_projects` row is gone, `prune_all_project_projections` (`prune.rs:390`) iterates only remaining registered projects and never visits that project again. Per stale project: acquire the 1.1 advisory lock (150 ms maintenance policy); busy → count `busy` and defer the **entire** project — SQL, Falkor projection, and collection all untouched until the next hourly run. Under the lock, run 1.3's shared projection-first sequence (clear the Falkor scope, delete the Qdrant collection, then `indexer::invalidate` SQL) — factor 1.3's internals so both callers share one implementation. The collection sweep then handles pre-existing orphans: per collection, acquire the 1.1 lock, re-query `code_indexed_projects` under it (guards the drift window while the prompt waited); row present → retain (`active`); absent → delete via `delete_project_collection` (`qdrant.rs:41`) — its 404 → `Ok(0)` handling maps to `already_missing`, which also makes overlap with the stale phase idempotent. **Graph-side Falkor scope sweep** (when Falkor is configured): for each graph orphan computed in discovery, acquire the 1.1 lock, re-check the `code_indexed_projects` row under it (row appeared → retain), and clear the scope via `clear_project`. Mutation only executes scopes that discovery enumerated and the prompt authorized — Falkor reachability was already proven in discovery, before any mutation anywhere.
4. **Observability**: print counts `scanned, active, orphaned, deleted, already_missing, busy, invalid, failed` plus at most 10 affected IDs (reuse `bounded_project_id_summary`, `prune.rs:560` test pins the cap). Continue through individual deletion failures; return nonzero exit status when any deletion failed.

Concurrency contract (unchanged from the indexer side): the indexer seeds its `code_indexed_projects` row before per-file writes, so indexer-first means the re-check retains the collection; cleanup-first means the indexer recreates it.

**Acceptance:**

- 1.2.1 - Global `gcode prune --force` deletes `code_symbols_<uuid>` collections absent from `code_indexed_projects` and retains all registered ones. file: `crates/gcode/src/commands/status/prune.rs`.
- 1.2.2 - Reconciliation runs with zero registered projects; missing Qdrant config skips only the collection phase while stale-project pruning still runs. behavior: "zero-project and no-Qdrant runs succeed" in `crates/gcode/src/commands/status/prune.rs`.
- 1.2.3 - Non-UUID `code_symbols_*` names and non-code collection families are never deleted; invalid names are counted and reported. behavior: "strict UUID filter" in `crates/gcode/src/commands/status/prune.rs`.
- 1.2.4 - A row inserted between enumeration and lock acquisition retains the collection; a busy lock counts as `busy` and exits zero. behavior: "lock-recheck concurrency safety" in `crates/gcode/src/commands/status/prune.rs`.
- 1.2.5 - Qdrant 404 on deletion counts as `already_missing`; any hard deletion failure yields a nonzero exit after processing all collections; enumeration failure aborts before any deletion, stale phase included. behavior: "idempotent, fail-visible cleanup" in `crates/gcode/src/commands/status/prune.rs`.
- 1.2.6 - All discovery precedes any mutation: plain `gcode prune` presents one combined prompt covering stale projects and orphan collections; declining leaves stale rows and collections untouched; `--force` mutates without prompting. behavior: "single pre-mutation confirmation gate" in `crates/gcode/src/commands/status/prune.rs`.
- 1.2.7 - The prompt/mutation matrix is pinned: stale+orphan, stale-only, orphan-only, and **graph-only** runs each prompt once and mutate on accept; a declined run performs zero mutations of any kind. behavior: "discovery-authorization-mutation matrix" in `crates/gcode/src/commands/status/prune.rs`.
- 1.2.8 - The stale phase mutates only under the per-project advisory lock via 1.3's shared projection-first sequence: the stale project's Falkor scope and Qdrant collection are removed in the same guarded pass as its SQL rows, a busy lock defers the whole project untouched, and an active indexer is never interleaved with. behavior: "lock-guarded stale removal with same-run Falkor cleanup" in `crates/gcode/src/commands/status/prune.rs`.
- 1.2.9 - With Falkor configured, prune enumerates project scopes from the code graph **during discovery**, includes graph orphans in the combined prompt, and clears those with no `code_indexed_projects` row even when SQL rows are already gone (lock + under-lock re-check per scope); configured-but-unreachable Falkor aborts the **entire run** before any mutation — stale, collection, and graph phases alike; missing Falkor config is a Falkor-only skip. behavior: "graph-side Falkor scope reconciliation" in `crates/gcode/src/commands/status/prune.rs`.

### 1.3 ID-native, projection-first, lock-guarded project invalidation [category: code] (depends: 1.1)

`kind: deliverable`

Targets: `crates/gcode/src/commands/status/invalidate.rs`, `crates/gcode/src/cli.rs`, `crates/gcode/src/dispatch.rs`

The purge service (4.2) must clean a project's code index by **ID** (a soft-deleted project's root may be gone) with an ordering that preserves retry inputs. Today `invalidate` (`invalidate.rs:8`) requires a root-resolved `Context`, runs `indexer::invalidate` (SQL, including the `code_indexed_projects` discovery row) **before** `cleanup_project_projections` (`:35`, Falkor clear + Qdrant collection delete) — a projection failure after SQL deletion loses the discovery anchor — and acquires **no advisory lock**, so an overlapping indexer can recreate projection or SQL state mid-cleanup.

1. Add a `--project-id <uuid>` mode to `gcode invalidate` that builds its context via `Context::resolve_for_project_id_with_services(..., ServiceConfigSelection::projection_cleanup())` — the established ID-native pattern (`graph clear --project-id` uses it) — with no project root required.
2. Reorder both modes to **projections first**: clear the Falkor graph scope and delete the Qdrant collection with failure-visible results, then run `indexer::invalidate` (SQL) only after both succeed. On projection failure, abort before SQL with a nonzero exit — discovery rows remain so a retry has every input it needs. **Missing-config semantics are pinned, and differ from failure**: a *missing* Qdrant or Falkor configuration is an honest per-backend skip that still proceeds to SQL — safe only because 1.2 supplies backend-side global enumeration for both (the Qdrant collection sweep and the graph-side Falkor scope sweep), so state skipped here becomes reconcilable the moment configuration returns. A *configured-but-unreachable* backend aborts before SQL, as above. Today's silent-skip (`cleanup_project_projections`, `crates/gcode/src/commands/status/invalidate.rs:35`) is unsafe precisely because no graph-side reconciler exists yet; 1.2's sweep is what licenses the skip.
3. **Run the whole sequence under the project advisory lock** in both root and ID modes, acquired via 1.1's entry point with the 150 ms maintenance policy. Acquisition drains any in-flight indexer (indexing holds the same lock); busy → nonzero exit with nothing touched, so a purge caller fails visibly *before* its hub transaction instead of racing the writer. A new index run starting **after** invalidate completes recreates a consistent row+collection pair — for the root-path-authoritative code index that is legitimate new state (worktree-style indexes need no `projects` row), not a leak; purge does not need to prevent it, only to never interleave with it. Factor the lock-guarded projection-first sequence into a crate-internal helper: prune's stale phase (1.2) runs the same helper per stale project, so there is exactly one guarded invalidation path.

**Acceptance:**

- 1.3.1 - `gcode invalidate --project-id <uuid> --force` cleans a project with no root on disk. file: `crates/gcode/src/commands/status/invalidate.rs`.
- 1.3.2 - Projection cleanup runs before SQL deletion; a projection failure aborts with nonzero exit leaving SQL discovery rows intact. behavior: "projection-first invalidate ordering" in `crates/gcode/src/commands/status/invalidate.rs`.
- 1.3.3 - Both invalidate modes hold the project advisory lock for the full projection+SQL sequence; a concurrently held lock yields a nonzero exit with zero deletions. behavior: "lock-guarded invalidate drains or fails visibly" in `crates/gcode/src/commands/status/invalidate.rs`.
- 1.3.4 - Missing Qdrant or Falkor configuration is a per-backend skip that still completes the SQL phase (each is later reconcilable via 1.2's backend-side sweeps); a configured-but-unreachable backend aborts before SQL with a nonzero exit. behavior: "missing-config skip vs unreachable-backend abort" in `crates/gcode/src/commands/status/invalidate.rs`.

## P2: Honest Cron Result Semantics

`kind: framing`

**Goal**: failed, timed-out, or gateway-unavailable global prunes record as failed cron runs.

### 2.1 Return a structured mapping from the global prune handler [category: code]

`kind: deliverable`

Target: `src/gobby/code_index/prune.py`

Change `CodeIndexPruner.prune_all_projects()` (`prune.py:107`) to return a mapping instead of a plain string:

```python
{
    "success": bool,
    "status": "completed" | "skipped" | "failed" | "timed_out" | "unavailable",
    "run_id": str | None,
    "message": str,          # current human-readable summary string
    "stdout": str,
    "stderr": str,
    "retried_projects": int, # len(outcomes) from the retry branch
}
```

- `success: false` for failed, timed-out, and `gateway is None` outcomes — **explicitly**, because `_coerce_mapping_result` (`src/gobby/scheduler/executor.py:213`) only fails on `success is False` or `status in FAILURE_RESULT_STATUSES`, and `timed_out` is not in that set. Targeted retries do not flip `success` back to true.
- The SIGTERM no-op path (`_is_noop_shutdown_result`, `prune.py:41`) and the global-lock-held skip stay `success: true` with `status: "skipped"`/`"completed"` as appropriate.
- `prune_project` / `prune_dirty_projects` keep their current string returns (internal aggregation, not cron-facing).
- Update `CODE_INDEX_PRUNE_DESCRIPTION` (`prune.py:28`) to mention orphan Qdrant collection cleanup; the existing name-keyed reconcile propagates it in place. Keep the existing job — no new maintenance owner.
- **Fix enabled-state clobbering** in `register_code_index_prune_cron` (`prune.py:288`): today it calls `toggle_job` on any disabled reconciled row, re-enabling operator-disabled jobs at every daemon start. Preserve the operator's enabled toggle on reconcile; wake (`wake_system_job`) only enabled rows whose `next_run_at` is null. First-time creation still defaults to enabled.
- Rewrite the string assertions in `tests/code_index/test_prune.py` (e.g. `test_global_prune_failure_retries_all_indexed_project_roots`, `:193`) to assert on the mapping, including that retries preserve `success: false`; add a disabled-row registration test proving reconcile leaves `enabled = false` untouched.

**Acceptance:**

- 2.1.1 - `prune_all_projects` returns the mapping above with `success: false` for failed, timed-out, and unavailable-gateway outcomes, so the cron executor records failed runs. file: `src/gobby/code_index/prune.py`.
- 2.1.2 - The reconciled job description mentions orphan Qdrant collection cleanup and the existing hourly system job is updated in place without duplication. behavior: "in-place system-job reconcile" in `src/gobby/code_index/prune.py`.
- 2.1.3 - Reconcile no longer re-enables an operator-disabled job; only enabled rows with null `next_run_at` are woken. behavior: "enabled-toggle preservation" in `src/gobby/code_index/prune.py`.

## P3: gwiki Reconciler

`kind: framing`

**Goal**: `gwiki prune` reconciles wiki scopes against the hub `projects` table with the same observability contract as gcode.

### 3.1 Add gwiki prune command [category: code] (depends: 3.2)

`kind: deliverable`

Targets: `crates/gwiki/src/commands/prune.rs` (new), `crates/gwiki/src/commands/mod.rs`, `crates/gwiki/src/commands/purge.rs`, `crates/gwiki/src/cli.rs`, `crates/gwiki/src/cli/mapping.rs`, `crates/gwiki/src/api.rs`

New `gwiki prune [--force]` command (`--force` skips the confirmation prompt, mirroring gcode). Full command wiring is part of this deliverable: a `Command` variant in `api.rs`, `CliCommand` mapping in `cli/mapping.rs`, module declaration and dispatch in `commands/mod.rs`, and CLI mapping coverage in the crate's focused CLI/dispatch tests — an isolated implementing agent must produce a *reachable* command.

Also add an **ID-native purge selector**: `gwiki purge --project-id <uuid> --yes` constructing the project `ScopeIdentity` directly from the UUID without reading any project root — the existing `--project` selector is an `Option<PathBuf>` (`cli.rs:233`), unusable for a soft-deleted project whose root is gone. Wire it through the same api/mapping/dispatch surface and cover a rootless project in the CLI tests. This selector is what `GwikiGateway.purge_project_scope` (4.1) shells to.

1. **Authority**: hub `projects` table. Dead project scope = no row at all. Soft-deleted rows still exist and are retained — retention timing lives solely in the Python purge job (4.2); once it hard-deletes the row, the next hourly prune reconciles the scope.
2. **Dead-scope purge**: SQL scope discovery is the **union of distinct `scope_kind='project'` scopes across every `GWIKI_POSTGRES_TABLES` table** — `gwiki_documents`, `gwiki_chunks`, `gwiki_links`, `gwiki_sources`, `gwiki_ingestions` (`crates/gwiki/src/setup.rs:10`). The five tables are independent with no foreign keys, and `record_ingestion` (`crates/gwiki/src/store/postgres.rs:385`) inserts into `gwiki_ingestions` even when no document row exists, so a documents-only scan misses SQL-only orphan scopes in the other four tables. For each discovered scope whose project row is absent, acquire the 3.2 per-project writer lock (brief try; busy → count `busy`, defer the scope to the next hourly run), **re-query the `projects` row under the lock, and proceed only while it is still absent** — a row inserted between discovery and lock acquisition (project re-created, scope now live) retains the scope untouched (3.2's reconciler admission branch). Only then run the internal purge path non-interactively, reusing `delete_scope_rows` (`crates/gwiki/src/commands/purge.rs:112` — already invoked per table across all five), `purge_qdrant_scope` (`:158`), and `purge_falkor_scope` (`:186`). Do not duplicate backend plumbing — factor purge's internals for shared use.
   **Reorder the shared purge sequence**: clear Qdrant and Falkor projections **before** deleting the SQL discovery rows (today purge deletes SQL first, so a Falkor failure after SQL deletion leaves a Falkor-only scope that dead-scope discovery can never rediscover). SQL rows are the discovery anchor — they go last, and only after both projections cleared. Applies to `gwiki purge` and the prune-internal path alike.
3. **Backend-level reconciliation** (crash-leak safety net): enumerate Qdrant collections once; delete `gwiki_project_<id>` collections whose project row is absent even when SQL rows are already gone. **When Falkor is configured, reconcile it the same way**: enumerate the distinct project-scope identifiers present in the wiki Falkor graph and purge those with no `projects` row even when SQL rows are already gone — discovery must not depend on SQL anchors on either backend, because scope purge can delete SQL rows while Falkor is unconfigured (`purge_falkor_scope` returns a successful skip for `config=None`, `crates/gwiki/src/commands/purge.rs:186`), and a scope leaked that way must become reconcilable as soon as configuration returns. Configured-but-unreachable Falkor aborts before any mutation, exactly like Qdrant enumeration failure. **Topic collections are never deleted by automation** — a live topic ingest can create `gwiki_topic_<name>` between a zero-row check and the delete, and gwiki has no shared scope lock to close that race (see Constraints); topic cleanup stays on the manual `gwiki purge --topic` path. `gwiki_topic_*` and malformed `gwiki_*` names are counted and reported, never deleted.
4. **Config/observability contract identical to 1.2**: missing Qdrant configuration skips **only the Qdrant-specific work** — collection enumeration/reconciliation and the Qdrant step of each scope purge — reported as a successful skip, while SQL dead-scope discovery, SQL row deletion, and Falkor cleanup (when Falkor is configured) still run; those stores are independently reconcilable and must not be held hostage to Qdrant availability. Missing Falkor configuration is symmetrically an honest **Falkor-only skip**: SQL and Qdrant work still run, and the leaked graph state stays discoverable via item 3's graph-side enumeration once Falkor is configured again. Qdrant enumeration *failure* (configured but unreachable) aborts before any deletion. Counts `scanned, active, orphaned, deleted, already_missing, busy, invalid, failed` (`busy` counts scopes deferred on a busy 3.2 writer lock) plus ≤10 affected scope IDs; continue through individual failures; nonzero exit when any failed.

**Acceptance:**

- 3.1.1 - `gwiki prune --force` purges SQL rows, Qdrant collections, and Falkor scope data for project scopes with no `projects` row, and retains scopes whose row exists (including soft-deleted). file: `crates/gwiki/src/commands/prune.rs`.
- 3.1.2 - `gwiki_topic_*` and malformed `gwiki_*` collections are never deleted by prune; they are counted and reported only. behavior: "topics excluded from automation" in `crates/gwiki/src/commands/prune.rs`.
- 3.1.6 - `gwiki purge --project-id <uuid> --yes` purges a project scope with no root on disk, constructing the scope identity directly from the UUID. file: `crates/gwiki/src/cli.rs`.
- 3.1.3 - Counts and exit-status semantics match the gcode reconciler contract (skip on missing config, abort before deletion on enumeration failure, nonzero on any failed deletion). behavior: "reconciler observability contract" in `crates/gwiki/src/commands/prune.rs`.
- 3.1.4 - The command is fully wired and reachable: `api.rs` variant, `cli/mapping.rs` mapping, `commands/mod.rs` dispatch, with CLI mapping test coverage. file: `crates/gwiki/src/cli/mapping.rs`.
- 3.1.5 - Scope purge clears Qdrant and Falkor before deleting SQL discovery rows, so a projection failure leaves the scope rediscoverable. behavior: "projection-before-SQL purge ordering" in `crates/gwiki/src/commands/purge.rs`.
- 3.1.7 - With no Qdrant configuration, prune still deletes dead-scope SQL rows and clears configured Falkor scopes, reporting the Qdrant phase as skipped. behavior: "Qdrant-only skip scope" in `crates/gwiki/src/commands/prune.rs`.
- 3.1.8 - With Falkor configured, prune enumerates project scopes from the wiki Falkor graph itself and purges those with no `projects` row even when their SQL rows are already gone; configured-but-unreachable Falkor aborts before any mutation; missing Falkor config is a Falkor-only skip. behavior: "graph-side Falkor scope reconciliation" in `crates/gwiki/src/commands/prune.rs`.
- 3.1.9 - A dead scope represented **only** in a non-document table (e.g. an ingestion-only scope with rows solely in `gwiki_ingestions`) is discovered and fully purged; SQL discovery provably unions all five `GWIKI_POSTGRES_TABLES` tables. behavior: "five-table scope discovery" in `crates/gwiki/src/commands/prune.rs`.

### 3.2 Cross-process gwiki project writer lock [category: code]

`kind: deliverable`

Targets: `crates/gwiki/src/project_lock.rs` (new), `crates/gwiki/src/commands/mod.rs`, `crates/gwiki/src/commands/index.rs`, `crates/gwiki/src/commands/session_sync.rs`, `crates/gwiki/src/commands/purge.rs`, `crates/gwiki/src/scope.rs`

gwiki writes are reachable from outside the daemon: the standalone CLI's project-scoped mutation commands resolve a project ID from the on-disk root with no liveness admission (`resolve_project_from_root`, `crates/gwiki/src/scope.rs:131`) and write SQL, Qdrant, and Falkor directly — `index`/`collect`/`ingest-file`/`ingest-url` (`crates/gwiki/src/commands/index.rs:112`) and `sync-sessions` (`session_sync::execute` writes the Postgres store, then `sync_qdrant_vectors` + `sync_falkor_graph`; `crates/gwiki/src/commands/session_sync.rs:42`) among them. An in-process Python fence (4.2) cannot see those processes, so the no-post-purge-write guarantee needs a cross-process lock on the store both sides share — hub Postgres.

1. **Lock primitive**: a per-project Postgres advisory lock in gwiki's own keyspace — key = first 8 bytes of `SHA-256("gwiki:project:" || project_id)` as big-endian `i64`, mirroring the derivation *shape* of gcode's `project_lock_key` (`crates/gcode/src/index_lock.rs:186`) with a distinct prefix so the two subsystems never contend. RAII guard released on drop; `pg_try_advisory_lock` polling with a bounded-wait policy for writers and a brief-try policy for prune (150 ms / 25 ms, matching gcode's maintenance policy).
2. **Writer inventory is derived from the dispatch table, not a hand-picked list**: audit every `Command::` arm of `commands::run` (`crates/gwiki/src/commands/mod.rs:44`) and classify it — project-scoped persistent writer (touches the Postgres store, Qdrant, or Falkor for a project scope), read-only, or topic-only. Every arm classified as a persistent writer acquires the lock; the classification is pinned by a test over the dispatch table (a new `Command` variant that is not classified fails the test), so a future writer cannot be silently omitted the way `sync-sessions` was from this plan's first draft.
3. **Admission is three explicit contracts, not one rule** — a single "refuse unless live" rule would make cleanup impossible (purge must operate on a soft-deleted row; prune on an absent one):
   - **Writers** (`index`, `collect`, `ingest-file`, `ingest-url`, `sync-sessions`, and every other arm classified as a persistent writer): acquire the lock, check `SELECT deleted_at FROM projects WHERE id = $1` under it; row absent **or soft-deleted** → refuse with a clear error and exit nonzero without writing. Wiki project scopes are always daemon projects (see Constraints), so the `projects` table is the correct authority. **The guard spans the entire command** — from before the first persistent write through the last SQL, Qdrant, and Falkor write and the command's outcome commit; it is not dropped after the admission check, or a writer could outlive 4.2's drain barrier mid-command.
   - **Explicit purge** (`gwiki purge --project`/`--project-id`): serialized cleanup — holds the lock for the whole scope mutation and operates on its authorized target whether the row is live, soft-deleted, or absent; the user is the authority here, and the lock's job is only to serialize against writers and prune.
   - **Reconciler prune** (3.1): brief-try the lock, re-query the row under it, and proceed **only while the row is still absent**; a row that appeared since discovery → release and retain the scope untouched. Busy → `busy`, deferred.
   Topic-scoped commands are exempt from all three — the lock scheme is project-scope only.
4. **Purge/prune integration**: the Python purge service (4.2) acquires the identical key via `pg_try_advisory_lock` on a dedicated connection as a **drain barrier** — acquisition after soft deletion proves no pre-soft-delete external writer remains in flight (sound only because writer guards span their full command, item 3); it then releases before delegating to the gwiki subprocess, which locks for itself (advisory locks are per-connection and cannot be inherited). The key derivation is therefore specified exactly and pinned by a **cross-language test vector**: a fixed project UUID must produce the same `i64` in the Rust tests and the Python tests, or purge and gwiki would silently lock different keys.

**Acceptance:**

- 3.2.1 - A per-project advisory lock exists in a gwiki-distinct keyspace; the documented derivation is pinned by matching Rust and Python test vectors for the same project UUID. file: `crates/gwiki/src/project_lock.rs`.
- 3.2.2 - Every dispatch arm classified as a project-scoped persistent writer — including `sync-sessions` — acquires the lock, refuses when the row is absent or soft-deleted (checked under the lock), and holds the guard through its final SQL/Qdrant/Falkor write; the dispatch-table classification test fails on an unclassified new `Command` variant; topic-scoped commands are unaffected. behavior: "dispatch-derived cross-process writer admission" in `crates/gwiki/src/commands/mod.rs`.
- 3.2.3 - Purge and prune scope mutations run under the lock; a concurrently held lock defers a prune scope (`busy`) and fails a purge attempt visibly instead of interleaving; prune's under-lock re-query retains a scope whose row appeared since discovery, while explicit purge operates on its authorized live or soft-deleted target. behavior: "three-branch lock admission" in `crates/gwiki/src/commands/purge.rs`.
- 3.2.4 - A project-scoped `sync-sessions` admitted before soft deletion completes its full write sequence before 4.2's drain barrier can acquire (guard-lifetime race: writer paused between its Postgres write and its Qdrant/Falkor sync still blocks the barrier); one started after soft deletion refuses under the lock with zero SQL, Qdrant, or Falkor writes. behavior: "sync-sessions fenced end-to-end" in `crates/gwiki/src/commands/session_sync.rs`.

## P4: Daemon Wiring

`kind: framing`

**Goal**: the daemon runs wiki reconciliation hourly, and projects have a real end-of-life: 30-day retention then full hard delete, plus immediate manual purge.

### 4.1 GwikiGateway prune/purge methods and wiki-prune system job [category: code] (depends: P3)

`kind: deliverable`

Target: `src/gobby/gwiki_gateway.py`, `src/gobby/wiki/prune_job.py`, `src/gobby/runner_lifecycle_subsystems.py`

- Add `GwikiGateway.prune_all_scopes(timeout=...)` shelling to `gwiki prune --force`, and `GwikiGateway.purge_project_scope(project_id, timeout=...)` shelling to `gwiki purge --project-id <uuid> --yes` (the ID-native selector from 3.1 — the root-based `--project` selector cannot address a rootless soft-deleted project). Mirror the gcode gateway's result handling (`GcodeCommandResult`-style: returncode/stdout/stderr/timed_out; timeouts return a result rather than raise — see `GcodeGateway.prune_all_projects` and `GcodeGateway._run_command_result` in the code-index gateway module).
- The global handler, its 2.1-contract result mapping (`success: false` on failed/timed-out/unavailable), and its name-keyed registration live in a **new module** `src/gobby/wiki/prune_job.py` — `src/gobby/wiki/scheduled_jobs.py` is already at 954 lines and must stay below the 1,000-line non-test limit; do not grow it with global-job machinery.
- Register the hourly **global** system job `gobby:wiki-prune` (handler `wiki:prune`), name-keyed with `is_system` and in-place reconcile, following the enabled-state-preserving pattern of `_ensure_wiki_cron_job` (and 2.1's fixed registration) — reconcile must never re-enable an operator-disabled row; wake only enabled rows with null `next_run_at`. A new job is correct here — existing wiki crons are per-project scoped.
- **Registration must not depend on project enumeration**: the startup caller `_register_wiki_cron_handlers` (`src/gobby/runner_lifecycle_subsystems.py:429`) returns at "skipped: no registered projects" when `project_scopes` and `stale_project_ids` are both empty — exactly the state in which orphan scopes can still need global reconciliation. Call the global registration **before** that early return (cron storage/executor guards still apply).

**Acceptance:**

- 4.1.1 - Gateway methods for global wiki prune and scope-targeted purge exist with gcode-gateway result semantics. file: `src/gobby/gwiki_gateway.py`.
- 4.1.2 - An hourly `gobby:wiki-prune` system job is registered name-keyed from the new module, reconciled in place without re-enabling a disabled row, and its handler reports failures as failed cron runs. file: `src/gobby/wiki/prune_job.py`.
- 4.1.3 - With zero non-protected projects (empty scopes, no stale IDs), startup still registers `gobby:wiki-prune` and its handler is callable; `scheduled_jobs.py` remains under 1,000 lines. behavior: "global registration precedes the per-project early return" in `src/gobby/runner_lifecycle_subsystems.py`.

### 4.2 Project purge service, retention cron, and CLI/HTTP surface [category: code] (depends: 4.1, P1)

`kind: deliverable`

Targets: `src/gobby/projects/purge.py` (new), `src/gobby/projects/write_fence.py` (new), `src/gobby/code_index/gcode_gateway.py`, `src/gobby/runner_init/orchestration.py`, `src/gobby/storage/projects.py`, `src/gobby/storage/cron.py`, `src/gobby/wiki/scheduled_jobs.py`, `src/gobby/code_index/codewiki_nightly.py`, `src/gobby/memory/services/lifecycle.py`, `src/gobby/memory/services/dedup.py`, `src/gobby/memory/services/indexing.py`, `src/gobby/memory/services/knowledge_graph/maintenance.py`, `src/gobby/memory/services/knowledge_graph/service.py`, `src/gobby/memory/collection_names.py`, `src/gobby/memory/vectorstore.py`, `src/gobby/mcp_proxy/semantic_search.py`, `src/gobby/ai/embedding_switch_runner.py`, `src/gobby/ai/embedding_switch.py`, `src/gobby/github_triage/issue_index.py`, `src/gobby/runner_maintenance.py`, `src/gobby/runner_lifecycle_subsystems.py`, `src/gobby/cli/projects.py`, `src/gobby/cli/embeddings.py`, `src/gobby/cli/installers/embedding.py`, `src/gobby/servers/routes/projects.py`, `src/gobby/servers/routes/embeddings.py`, `src/gobby/storage/config_store.py`, `src/gobby/config/embedding_keys.py`, `src/gobby/config/app.py`, `src/gobby/mcp_proxy/tools/config.py`, `src/gobby/servers/routes/configuration_values.py`, `src/gobby/servers/routes/configuration_secrets.py`, `src/gobby/servers/routes/configuration_templates.py`, `src/gobby/servers/routes/configuration_import_export.py`, `src/gobby/cli/secrets.py`

A single purge service shared by CLI, HTTP, and cron. Purging project `<id>`:

0. **Protection gate first, inside the service**: refuse any project where `LocalProjectManager.is_protected` (`src/gobby/storage/projects.py:377`) is true — `SYSTEM_PROJECT_NAMES` includes `gobby` itself. Do not rely on CLI/HTTP callers for this check; every entry path (CLI, HTTP, cron, direct service call) hits the same gate.
1. **Cron writer quiescence, in disable → drain → delete order** — soft deletion does not quiesce the running daemon's per-project crons, and `LocalProjectManager.get` returns soft-deleted rows (`src/gobby/storage/projects.py:198`), so a scheduled writer could repopulate projections mid-purge. Deleting jobs first is blind: `delete_system_jobs_by_project_and_name_prefix` removes matching `cron_runs` rows before the jobs (`src/gobby/storage/cron.py:674`), so a drain poll after it always sees nothing while the asyncio handler may still be executing. Instead:
   - **Snapshot and disable** every project-owned writer job — all cron jobs whose `project_id` matches, which covers the `gobby:wiki-*` family **and** `gobby:codewiki-nightly:<project_id>` (`src/gobby/code_index/codewiki_nightly.py:58`, whose handler drives gwiki ingestion via `CodewikiRefreshService`) — closing admission for new runs without touching run records.
   - **Drain** the snapshotted jobs' in-flight runs via the active-run surface (`list_active_runs`, `src/gobby/storage/cron_runs.py:270`) with a bounded wait, while their run rows still exist; still active at the bound → the purge **fails** (`success: false`), the soft-deleted row stays, the next daily run retries.
   - **Delete** the jobs (the existing prefix API plus per-job deletion for non-wiki names) only after the drain succeeds.
   - **Handler liveness guard**: every per-project cron handler that writes derived state — the seven wiki handlers in `scheduled_jobs.py` (refresh, health, audit, sync-sessions, upkeep, librarian, recap) **and the codewiki-nightly handler** — checks its project row at run start and no-ops with a skipped result when the row is absent **or soft-deleted** (the check must test `deleted_at` explicitly — `get` doesn't filter it). This rejects runs admitted before the disable and any ad-hoc trigger. The guard is a one-line call per handler into a shared helper living in `prune_job.py` (4.1's module) so `scheduled_jobs.py` stays under the 1,000-line limit.
2. **Derived-writer fence** — cron quiescence cannot stop non-cron writers already in flight: `MemoryLifecycleService.create_memory` persists the SQL row, then awaits the vector upsert, then queues graph work (`src/gobby/memory/services/lifecycle.py:179`), so a call admitted before soft deletion can pause at the embedding await, outlive cleanup, and land an orphan point after the `projects` row is gone; `GitHubIssueIndexer.upsert` embeds and upserts project-scoped points with no liveness check (`src/gobby/github_triage/issue_index.py:181`). Add a per-project write fence (new module `src/gobby/projects/write_fence.py`) gating **every in-daemon path that emits project-scoped vector or graph state** — the complete inventory, each acquiring as a writer at its service-level chokepoint:
   - memory vector upserts (`MemoryLifecycleService`, `src/gobby/memory/services/lifecycle.py:179`) **and** the detached background dedup task — `fire_background_dedup` (`src/gobby/memory/services/lifecycle.py:111`) spawns `asyncio.create_task` around `DedupService.process` → `_embed_and_upsert` (`src/gobby/memory/services/dedup.py:220`), a project-scoped upsert that outlives the synchronous caller, so **the spawned task itself holds writer admission for its full lifetime**, not just the spawner;
   - memory reindex batch upserts (`IndexingService.reindex_embeddings`, `src/gobby/memory/services/indexing.py:380`) **and** the reconcile/backfill path (`IndexingService.reconcile_stores` → `_backfill_embeddings`, `src/gobby/memory/services/indexing.py:259`);
   - the startup vector-store rebuild — `rebuild_vector_store` (`src/gobby/runner_maintenance.py:208`) runs as a background task from startup (`src/gobby/runner_lifecycle_subsystems.py:311`) and rebuilds the whole `memories` collection asynchronously; the rebuild task holds writer admission (fence-global, since it spans all projects) so purge either drains it or it completes before cleanup;
   - knowledge-graph enqueue/apply mutations (`KnowledgeGraphService`, `src/gobby/memory/services/knowledge_graph/service.py` — the apply path that actually writes Falkor);
   - tool-embedding upserts (`SemanticToolSearch.store_embedding` / `embed_all_tools`, `src/gobby/mcp_proxy/semantic_search.py:215`);
   - GitHub issue index upserts (`GitHubIssueIndexer.upsert`, `src/gobby/github_triage/issue_index.py:181`);
   - embedding-switch collection rebuilds — **all three builders**, `_build_memory_collection` (`src/gobby/ai/embedding_switch_runner.py:308`), `_build_tool_collection` (`:343`), and `_build_github_issue_collection` (`:377`). Today these run in the **standalone CLI process**: `gobby embeddings switch start`/`resume` call `asyncio.run(...)` on `start_embedding_switch` / `resume_embedding_switch` directly (`src/gobby/cli/embeddings.py:152,184`; `src/gobby/ai/embedding_switch_runner.py:100,129`), where the daemon's in-process fence is invisible — a CLI switch could pass its own process-local admission before soft deletion, pause before a staged upsert, survive the daemon's drain (which sees no admission), and resume after hard delete to write purged-project points that a later flip activates. **The daemon therefore owns the complete mutating switch lifecycle**: daemon endpoints in `src/gobby/servers/routes/embeddings.py` run start/resume as a daemon background task and handle abort; `gobby embeddings switch start`/`resume`/`--abort` become thin clients of those endpoints, and every standalone mutation path is **deleted — no fallback**. Abort is standalone today too, and worse than start/resume: `_switch_abort` (`src/gobby/cli/embeddings.py:136`) calls `abort_switch` (`src/gobby/ai/embedding_switch.py:139`), which deletes the persisted journal while a live runner keeps its in-memory copy — and `advance_phase` (`src/gobby/ai/embedding_switch.py:253`), called after the builders by `EmbeddingSwitchRunner.build` (`src/gobby/ai/embedding_switch_runner.py:232`), **unconditionally rewrites** that copy, resurrecting the aborted journal and proceeding to flip. So the daemon holds **exactly one active switch task** (single-flight): start or resume while a task is live is rejected with the active run's ID, never a second concurrent runner. Abort is **phase-aware and cooperative, never bare task cancellation**: every persistent Qdrant mutation runs via `asyncio.to_thread` (`src/gobby/memory/vectorstore.py:3` — upserts, `create_alias`, `delete_collection`), and cancelling the awaiting coroutine leaves the worker thread running to completion, so abort **awaits every in-flight persistent operation to settlement** and stops only at safe checkpoints between operations. Before FLIPPING, abort stops at the next checkpoint and then — after in-flight writes settle — **enumerates and deletes every staged `kind@run_id` physical for the journal's run_id before deleting the journal**: the journal is the only cleanup anchor for those collections (no reconciler owns the `memories@`/`tool_embeddings@`/`gobby_github_issues@` families), so a failed cleanup retains the journal in a durable **aborted/cleanup-pending** state, the journal is deleted only after cleanup succeeds, and a re-issued abort or daemon restart retries the cleanup. Once FLIPPING begins — `EmbeddingSwitchRunner.flip` (`src/gobby/ai/embedding_switch_runner.py:253`) repoints the three aliases in separate awaits and then writes canonical config before the journal transition — the switch is inside an **irreversible forward-completion region spanning FLIPPING, ACTIVE, and GC**, not just the flip: `flip` itself deletes an old physical mid-loop when it shadowed the alias name (`src/gobby/ai/embedding_switch_runner.py:271`), and after the ACTIVE journal write `gc` (`:288`) keeps running with the journal still persisted, awaiting `get_aliases` and old-collection deletions until `complete_switch` finally removes it (`:305`). So abort admission keys on "has FLIPPING ever begun", not on the current phase: from that point abort **never performs staged-run cleanup** (the staged `kind@run_id` physicals are now actual or imminent alias targets) and never cancels a GC deletion mid-thread — it joins or reports the in-progress completion ("too late — switch completing"), a GC failure retains the journal for restart retry through the same single-flight gate, and the switch finishes coherently rather than leaving aliases, config, or GC half-done. On daemon restart, a persisted journal with no live task resumes only as one newly fenced task through the same single-flight gate (a cleanup-pending journal resumes as cleanup retry, never as a build). Daemon unreachable → the CLI refuses with a clear error and **zero persistent writes** (no switch journal, no config mutation, no staged collection). Under the fence, each per-project rebuild step acquires writer admission at the admission-time live-row check and holds it through that project's **final staged write**, skipping projects that fail admission, so a switch running during purge cannot repopulate the purged project;
   - daemon-mediated gwiki ingest (the `CodewikiRefreshService` path).

   Writer acquisition **rejects** projects whose row is absent or soft-deleted — one chokepoint for the liveness check — and admission is held at the actual asynchronous write lifetime: a path that spawns a background task passes admission into the task, which releases only after its final upsert. Purge acquires the fence exclusively before any derived cleanup and holds it through the hub transaction: already-admitted writers finish first (bounded wait; still held at the bound → purge fails, row stays), and new writers are rejected for the duration and forever after. The in-process fence covers daemon writers only; **standalone gwiki processes are fenced by the 3.2 cross-process advisory lock**, and the embedding switch is brought **inside** the daemon rather than fenced across processes (see the builder bullet above) — after this change no embedding-switch writer exists outside the daemon. Purge uses it as a **drain barrier**: after the project is soft-deleted (which makes every subsequent gwiki writer admission fail its under-lock liveness check), the service acquires the 3.2 key Python-side (same pinned derivation, bounded wait on a dedicated connection) — acquisition proves no pre-soft-delete external writer is still in flight — then releases it before delegating wiki cleanup to the gwiki subprocess (advisory locks are per-connection; the subprocess takes the lock itself). Busy at the drain bound → purge fails, row stays. gcode writers are excluded — the project advisory lock (1.3) is the fence there.

   **Journal persistence and mutation-ownership contract** — two verified gaps make the daemon owner unimplementable as previously stated. (a) The switch journal **cannot be persisted through the real ConfigStore today**: `_write_journal` writes `_SWITCH_JOURNAL_KEY = ai.embeddings.switch_run` (`src/gobby/ai/embedding_switch.py:28`), but `ConfigStore.set` validates every key through `validate_embedding_storage_config_key` (`src/gobby/storage/config_store.py:66`), which admits only the six canonical fields in `EMBEDDING_CONFIG_FIELDS` (`src/gobby/config/embedding_keys.py:19`) and raises for anything else under `ai.embeddings.` — the existing switch tests pass only because they mock the store. The deliverable defines an **internal lifecycle-key contract**: the config storage layer accepts `ai.embeddings.switch_run` exclusively through the daemon switch-lifecycle owner's write path, while the public config surfaces (set, batch set, import) reject it; bulk reset/import/`delete_all` **preserve the journal** (or route its removal through the lifecycle owner as an abort) so configuration maintenance can never silently erase the only cleanup anchor of a live or cleanup-pending run. Write-side admission is only half the contract — the key must also be **invisible to every generic read path**: `load_config` feeds the full `config_store` dump through `storage_embedding_config_entries_to_runtime` (`src/gobby/config/app.py:623`), whose key mapper raises on any non-canonical `ai.embeddings.*` key (`src/gobby/config/embedding_keys.py:139`), and the switch runner itself calls `load_config` mid-build (`src/gobby/ai/embedding_switch_runner.py:347`) — so a write-side-only contract makes the daemon switch crash the moment its own journal exists, and the same `get_all` path feeds configuration export and template generation, leaking a live journal into exported bundles the public-key rejection then refuses to re-import. The read-side contract: the lifecycle owner reads the journal via direct keyed get; runtime config loading, public get/list, template generation, and configuration export all **omit internal lifecycle keys**; import rejects an injected lifecycle key while preserving the current journal. Journal persistence is tested against the **real ConfigStore, no mocks**: start, every phase and error write, abort/complete deletion, restart resume through the single-flight gate, plus read-side proofs — a live journal followed by a successful `load_config` and staged build step, export/template/list omitting the key, and an export→import round-trip that neither exposes nor erases it. (b) The daemon is not the complete mutation owner while `gobby install` writes canonical embedding config standalone: `_persist_embedding_config` (`src/gobby/cli/installers/embedding.py:426`) opens the hub directly and `set_many`-writes model/api_base/dim/query_prefix/catalog_key. Direct installer writes are permitted **only for proven first bootstrap** — no existing embedding config, no switch journal, no managed collections, no live daemon switch; any reconfiguration of an existing setup delegates to the daemon lifecycle gate or refuses with zero writes. The gate covers the **complete mutation inventory**, not just set/batch/import: canonical embedding fields and the `ai.embeddings.api_key` secret (SecretStore name `embeddings_api_key`, written at `src/gobby/cli/installers/embedding.py:479`) are also reachable through MCP `delete_config`/`ensure_defaults` (`src/gobby/mcp_proxy/tools/config.py`), the HTTP values set/delete and `reset_config` routes (`src/gobby/servers/routes/configuration_values.py:259`), template save — whose `delete_all_except` helper executes raw `DELETE FROM config_store` outside ConfigStore's key validation (`src/gobby/servers/routes/configuration_secrets.py:62`, invoked from `src/gobby/servers/routes/configuration_templates.py:91`) — configuration import's secret writes (`src/gobby/servers/routes/configuration_import_export.py:298`), `ConfigStore.delete`/`delete_all`/`set_secret`/`clear_secret` (`src/gobby/storage/config_store.py:189,192,227,280`), and generic CLI secret CRUD (`src/gobby/cli/secrets.py`). Every one of these — owner writes, public mutations, bulk replacement/reset, secret changes, and the installer's bootstrap proof — acquires the **same journal-key admission lock** switch start already takes (`transaction_immediate(EmbeddingSwitchJournalMutation)`, `src/gobby/ai/embedding_switch.py:179`) for its complete check-and-write transaction, closing the TOCTOU where a writer observes "no journal", races a concurrent start, and lands its write afterward. While any live or cleanup-pending journal exists, public paths reject with zero writes and bulk replacement/reset paths preserve the journal; no canonical config or embedding-secret write can bypass the single-flight gate.
3. Wiki state via `GwikiGateway.purge_project_scope` (projections before SQL per 3.1's ordering). The gwiki subprocess acquires the 3.2 lock internally — advisory locks are per-connection, so the Python parent cannot hold it *for* the subprocess, and it does not need to: the drain barrier in step 2 has already flushed pre-soft-delete external writers, and any gwiki writer attempting admission afterward fails the under-lock liveness check (row soft-deleted, later absent) and exits without writing. No unlocked window admits a writer at any point after soft deletion.
4. Code index: add `GcodeGateway.invalidate_project_by_id(project_id, timeout=...)` shelling to `gcode invalidate --project-id <uuid> --force` (the 1.3 command: lock-guarded, projections first, SQL last, nonzero exit on projection failure or busy lock). This single path replaces separate `delete_project_index` + collection calls — the command's SQL phase already deletes the child tables and the `code_indexed_projects` row, and its ordering preserves retry inputs. A nonzero result stops the purge before the hub transaction — the lock either drains an in-flight indexer or fails the purge. A re-index starting *after* invalidate completes is legitimate consistent state for the root-path-authoritative code index (see 1.3), not a leak; purge does not guard against it.
5. Vectors and memory graph — the full project-scoped inventory, across **every physical collection, active or staged**: the embedding switch builds into versioned physical collections (`kind@run_id`, `CollectionNameResolver.physical_name`, `src/gobby/memory/collection_names.py:26`) and only later flips the serving aliases (`EmbeddingSwitchRunner.flip`, `src/gobby/ai/embedding_switch_runner.py:253`), so deleting from the three active aliases alone lets a project copied into a staged physical while live **reappear when flip activates it after the hard delete**. Purge therefore enumerates the actual collection set at cleanup time — the active aliases for the three managed kinds plus every staged `kind@run_id` physical (`parse_physical_name`) — and deletes the project's points from each (`GITHUB_ISSUE_COLLECTION`, `src/gobby/github_triage/issue_index.py:18`; every issue point carries a `project_id` payload). This runs after the fence drain, so an admitted mid-build switch has finished its staged writes first; a switch admitted later skips the dead project (step 2). Deletion is **exact-match plus captured-ID**: (a) `VectorStore.delete(filters={"project_id": <id>}, collection_name=...)`, which builds `FieldCondition` musts, and (b) deletion by the project's memory IDs captured from SQL **before** the hub transaction — required because the startup rebuild historically wrote payloads with no `project_id` (`memory_dicts` carries only `id`/`content`, `src/gobby/runner_lifecycle_subsystems.py:309`, and `VectorStore.rebuild` stores only supplied keys, `src/gobby/memory/vectorstore.py:821`), so project-owned points can be payload-unscoped and invisible to the filter. **Fix the source too**: the startup rebuild payload includes `project_id` so future rebuilds are scoped. Do **not** reuse `memory_project_scope_filter`: it is a recall filter whose `include_global=True` default also matches empty/null/missing `project_id` payloads, and `VectorStore.delete` does not accept its Qdrant `Filter` object. Genuinely global/unscoped points (IDs not owned by the project) and other-project points must survive. Then clear the project's memory knowledge graph via a **fail-visible** variant of `KnowledgeGraphMaintenance.clear_project_graph` (`src/gobby/memory/services/knowledge_graph/maintenance.py:173`) — the current method swallows Falkor errors and returns zeros; purge must surface backend failure and stop before the hub transaction.
6. **Only after steps 1–5 all succeed**, in one hub transaction: delete `tasks`, `plans`, `sessions`, `memories` rows for the project (their own child cascades apply), then `DELETE FROM projects WHERE id = %s` — the 23 `ON DELETE CASCADE` referencing tables cascade; `comms_identities.project_id` becomes `SET NULL`. **No tombstone remains.**

**Retry anchor — the row is deleted last, deliberately**: memory, tool, and GitHub-issue points and the memory knowledge graph have no orphan reconciler (the hourly reconcilers cover only the code index — Qdrant collections and, when configured, code-graph Falkor scopes — plus wiki Qdrant collections and configured wiki Falkor scopes), so the soft-deleted `projects` row is the only durable cleanup authority for those stores. If any derived-store step fails, the service stops before the SQL transaction, marks the run failed (2.1 mapping contract, `success: false`), and leaves the soft-deleted row in place — the next daily run retries.

Retention, wiring, and surfaces:

- Register a daily system job `gobby:project-purge` (handler `projects:purge-expired`) that runs the service for every project with `deleted_at` older than 30 days, excluding `SYSTEM_PROJECT_NAMES` in the selection query as defense in depth (protected rows are never soft-deleted, but the query must not trust that). Registration follows the same enabled-state-preserving invariant as 2.1/4.1: reconcile never re-enables an operator-disabled row and wakes only enabled rows whose `next_run_at` is null.
- **Batch semantics of the daily handler**: process every eligible project independently — one project's failure never aborts the loop or starves later candidates. The handler returns the 2.1 mapping shape with aggregate fields: `{success, status, message, purged: [ids], failed: [ids], skipped_protected: [ids]}` (ID lists bounded to 10 with counts). `success: false` / `status: "failed"` when any candidate fails; each failed project's soft-deleted row remains for the next daily retry.
- **Startup wiring is part of this deliverable**: construct the purge service and register the handler plus the name-keyed system job in `src/gobby/runner_init/orchestration.py` (where `CronExecutor` handlers and `register_code_index_prune_cron` are already wired), before `CronScheduler` starts. A registration function that nothing calls is a non-deliverable.
- `gobby projects purge <id>` CLI (confirmation required, `--yes` to skip) and an HTTP endpoint: run the same service immediately, soft-deleting first when the project is still live.

**Acceptance:**

- 4.2.1 - The purge service deletes the four `NO ACTION` tables' rows and the `projects` row in one transaction only after cron quiescence, the derived-writer fence, the cross-process gwiki drain barrier, wiki, code-index, vector, and memory-graph cleanup all succeed, with all cascades applying and no project row remaining. file: `src/gobby/projects/purge.py`.
- 4.2.2 - A daily `gobby:project-purge` system job is registered from startup orchestration and purges projects soft-deleted more than 30 days ago, never touching live projects or any `SYSTEM_PROJECT_NAMES` project. file: `src/gobby/runner_init/orchestration.py`.
- 4.2.3 - `gobby projects purge <id>` (CLI, confirmed) and the HTTP endpoint run the same service immediately, soft-deleting live projects first; protected names are refused inside the service on every entry path. file: `src/gobby/cli/projects.py`.
- 4.2.4 - Vector cleanup applies exact-match `project_id` deletion **and** captured-memory-ID deletion across every active and staged physical collection of `memories`, `tool_embeddings`, and `gobby_github_issues`; a legacy point with a project-owned ID but no `project_id` payload (the startup-rebuild shape) is removed, while genuinely global/unscoped and other-project points survive in all three kinds. behavior: "exact-match plus captured-ID vector cleanup" in `src/gobby/projects/purge.py`.
- 4.2.5 - A failed derived-store step leaves the soft-deleted row in place and records a failed run; the next daily run retries and completes the hard delete. behavior: "retry-anchor failure handling" in `src/gobby/projects/purge.py`.
- 4.2.6 - The daily handler isolates per-project failures (later candidates still processed), returns the aggregate mapping with bounded ID lists, and reports `success: false` when any candidate fails. behavior: "batch failure isolation" in `src/gobby/projects/purge.py`.
- 4.2.7 - `gobby:project-purge` reconcile preserves an operator-disabled row and wakes only enabled rows with null `next_run_at`. behavior: "purge-job enabled-toggle preservation" in `src/gobby/runner_init/orchestration.py`.
- 4.2.8 - Code-index cleanup goes through `GcodeGateway.invalidate_project_by_id` and a nonzero result stops the purge before the hub transaction. file: `src/gobby/code_index/gcode_gateway.py`.
- 4.2.9 - Purge disables every project-owned writer cron job (the wiki family and `gobby:codewiki-nightly`), drains their in-flight runs while the run rows still exist, and deletes the jobs only after the drain succeeds; a run still active at the drain bound — wiki or codewiki — fails the purge with the soft-deleted row intact. behavior: "disable-drain-delete cron quiescence" in `src/gobby/projects/purge.py`.
- 4.2.10 - Every per-project wiki cron handler and the codewiki-nightly handler no-op with a skipped result when the project row is absent or soft-deleted, covering the writer/purge overlap window. behavior: "handler liveness guard" in `src/gobby/wiki/scheduled_jobs.py`.
- 4.2.11 - The project's memory knowledge graph is cleared through a fail-visible clear path — a Falkor backend failure stops the purge before the hub transaction — and other projects' graph nodes survive. behavior: "fail-visible memory-graph cleanup" in `src/gobby/projects/purge.py`.
- 4.2.12 - A per-project write fence gates the complete in-daemon derived-writer inventory (memory vector upserts, the detached background-dedup task, reindex **and** reconcile/backfill batches, the startup vector rebuild, knowledge-graph apply, tool-embedding upserts, GitHub issue index, all three embedding-switch builders — which after this deliverable execute only in-daemon (4.2.16) — and daemon-mediated gwiki ingest): purge holds it exclusively through the hub transaction, admitted writers — including spawned background tasks, which carry admission for their full lifetime — finish before cleanup starts, and writer acquisition for an absent or soft-deleted project is rejected. Race tests pause each distinct path mid-flight — a memory write, a background-dedup task, a reindex batch, a backfill batch, the startup rebuild, a tool-embedding upsert, an issue-index write, and a graph-apply — and prove no point or node lands after purge completes; an embedding-switch rebuild running during purge skips the purged project in all three builders. file: `src/gobby/projects/write_fence.py`.
- 4.2.13 - The purge service acquires the 3.2 cross-process gwiki lock as a drain barrier after soft deletion: a race test holds the lock from a second Postgres connection (simulating a standalone gwiki writer mid-command, including paused between its Postgres write and its Qdrant/Falkor sync) and proves purge waits, fails visibly at the bound with the row intact, and — once the writer admission path sees the soft-deleted row — no external gwiki write can land after purge completes. behavior: "cross-process gwiki drain barrier" in `src/gobby/projects/purge.py`.
- 4.2.14 - Staged-collection races are closed for all three managed kinds: an embedding-switch build that copied a live project into staged `memories@run`, `tool_embeddings@run`, and `gobby_github_issues@run` physicals, paused before flip, then resumed after the project's purge, activates aliases containing **zero** purged-project points. behavior: "staged-collection purge coordination" in `src/gobby/projects/purge.py`.
- 4.2.15 - The startup vector rebuild writes `project_id` into every rebuilt point's payload; a regression test proves rebuilt project memories are reachable by the exact-match purge filter. behavior: "scoped startup-rebuild payload" in `src/gobby/runner_lifecycle_subsystems.py`.
- 4.2.16 - Embedding-switch mutation is in-daemon only: `gobby embeddings switch start`, `resume`, and `--abort` delegate to daemon endpoints, and with the daemon stopped all three refuse with a clear error and **zero persistent writes** — no switch journal, no config mutation, no staged collection. A race test pauses an in-daemon builder after per-project writer admission but before that project's staged upsert and proves purge's fence drain waits for the builder to finish (or fails at the drain bound with the soft-deleted row intact as the retry anchor), and the eventual flip activates aliases containing zero purged-project points. behavior: "in-daemon-only embedding switch" in `src/gobby/servers/routes/embeddings.py`.
- 4.2.17 - The daemon is the single-flight owner of the switch lifecycle: a second `start` or `resume` while a switch task is live is rejected with the active run's ID and spawns no second runner and no journal write; abort is phase-aware and cooperative — it awaits every in-flight persistent operation to settlement, stops only at safe checkpoints before FLIPPING, and once FLIPPING has ever begun the switch is inside an irreversible forward-completion region spanning FLIPPING, ACTIVE, and GC: abort never performs staged-run cleanup, never cancels a GC deletion, joins or reports the in-progress completion ("too late"), and a GC failure retains the journal for restart retry; after a daemon restart with a persisted journal and no live task, `resume` produces exactly one newly fenced task, and re-completion of the forward region is idempotent. Race tests abort while the switch is paused **inside an `asyncio.to_thread` upsert, inside `create_alias`, between two alias repoints, between the canonical config write and the journal transition, immediately after the ACTIVE journal write before `gc` begins, and inside `gc`'s `asyncio.to_thread` old-collection deletion**, proving abort never returns success before persistent state is coherent, **no active alias target is ever deleted, no worker thread outlives a successful abort response**, and **no later staged write, no partial alias flip, and no journal resurrection** occurs — the `advance_phase` rewrite (`src/gobby/ai/embedding_switch.py:253`) never lands after a completed abort. behavior: "single-flight cooperative switch lifecycle ownership" in `src/gobby/servers/routes/embeddings.py`.
- 4.2.18 - The switch journal persists through the **real ConfigStore**: the internal lifecycle-key contract admits `ai.embeddings.switch_run` through the daemon lifecycle owner for start, every phase and error write, and abort/complete deletion, while public config set/batch/import surfaces reject the key, and bulk reset/import/`delete_all` preserve a live or cleanup-pending journal (or route its removal through the lifecycle owner); on the read side, runtime config loading, public get/list, template generation, and configuration export omit the key — with a live journal persisted, `load_config` and a staged build step succeed, export/template/list output contains no lifecycle key, import rejects an injected lifecycle key while preserving the current journal, and an export→import round-trip neither exposes nor erases it; restart resume through the single-flight gate is exercised against the real store with **no mocked ConfigStore anywhere in the switch-lifecycle tests**. behavior: "internal lifecycle-key journal persistence" in `src/gobby/storage/config_store.py`.
- 4.2.19 - A pre-flip abort (FLIPPING never entered) cleans its staged artifacts: after in-flight writes settle, every staged `kind@run_id` physical collection for the journal's run_id is enumerated and deleted **before** the journal is deleted; a failed cleanup retains a durable aborted/cleanup-pending journal, and a re-issued abort or daemon restart retries the cleanup to completion — tested with abort after each of the three staged collections is created and with a cleanup failure injected. Once FLIPPING has ever begun, staged-run cleanup is never executed — the staged physicals are actual or imminent alias targets and the run completes forward per 4.2.17. behavior: "staged-artifact cleanup before journal deletion" in `src/gobby/ai/embedding_switch_runner.py`.
- 4.2.20 - The daemon is the complete embedding-config mutation owner: `gobby install` writes embedding config directly only on proven first bootstrap (no existing embedding config, no switch journal, no managed collections, no live switch) and otherwise delegates to the daemon lifecycle gate or refuses with zero writes; every mutation surface in the step-2 inventory — generic set/batch/import, MCP `delete_config`/`ensure_defaults`, HTTP values delete and `reset_config`, template save's bulk replacement, `ConfigStore.delete`/`delete_all`/`set_secret`/`clear_secret`, and CLI/server secret CRUD for `ai.embeddings.api_key` — executes its check-and-write under the same journal-key admission lock as switch start, and while a switch is live each rejects with zero writes or routes through the owner, with bulk paths preserving the journal. Simultaneous-barrier race tests run switch start against each mutation class (installer reconfigure, generic set, delete/reset, template save, `ensure_defaults`, secret CRUD), and live-switch zero-write tests cover delete, reset, template save, defaults, and secret CRUD — proving no canonical config or embedding-secret write ever bypasses the single-flight gate. file: `src/gobby/cli/installers/embedding.py`.

- 4.2.21 - Every embedding-config and secret mutation makes its live-journal
  admission decision while holding the lifecycle owner's journal-key lock. If a
  live or cleanup-pending journal exists, that transaction either rejects the
  mutation atomically with zero writes or executes it through the lifecycle
  owner; no caller performs a pre-lock check followed by an independent write.
  behavior: "atomic live-journal mutation admission" in
  `src/gobby/ai/embedding_switch.py`.

## P5: Rollout

`kind: framing`

**Goal**: the live environment is reconciled, the one-time debt is cleared, and failure visibility is demonstrated.

### 5.1 Live reconciliation verification and one-time cleanup [category: test] (depends: P1, P2, P4)

`kind: deliverable`

Target: `~/.gobby/bin/{gcode,gwiki}` (reinstall), live daemon

1. Rebuild and atomically promote the release `gcode` and `gwiki` binaries through the existing native-binary lock path; restart the daemon so Python changes and the reconciled job definitions load.
2. One-time cleanup the automation intentionally will not do:
   - Delete the 2 non-UUID collections via the Qdrant API: `code_symbols_graph-standalone-cpp-local-manual`, `code_symbols_graph-standalone-no-phantom`.
   - Purge the 11 junk topics via `gwiki purge --topic <t> --yes`: `refresh-test, vverify, vision-smoke, gobby-17644-verify, wp3-mm, wp3-mm2, wp3-mm-auto, wp3-honesty, track-b-bakeoff, agent-run-caps, hn-local-first-dev-tools`. Keep `sessions`, `hn-local-first-developer-tools`, `arxiv-daily-relevance-2026-07-05`.
   - `gobby projects purge` gobby-cli (`3bf57fe7`) and gsqz (`accc5a11`) — user-approved full hard delete.
3. Before purging, snapshot (a) `SELECT id FROM code_indexed_projects`, (b) per-project point counts by `project_id` payload in `memories`, `tool_embeddings`, and `gobby_github_issues`, and (c) per-project `Memory`-node counts in the memory knowledge graph. Then trigger `gobby:code-index-prune`, `gobby:wiki-prune`, and `gobby:project-purge` once via `run_cron_job`. Verify:
   - Zero orphaned `code_symbols_<uuid>` collections remain, and the surviving collection set equals the **post-purge** `code_indexed_projects` rows — not a hard-coded count: the gobby-cli purge removes its row (`3bf57fe7`) and collection, so the registered set shrinks from the pre-rollout 15. The distinct `project` scopes in the code Falkor graph likewise equal the post-purge registered set (1.2's graph-side sweep clears dead scopes accumulated by historical stale prunes).
   - The 3 dead gwiki project scopes (`6f1f9a54`, `00000000-…-17650`, `00000000-…-17651`) are gone from SQL, Qdrant, and Falkor.
   - The `memories`, `tool_embeddings`, and `gobby_github_issues` collections still exist; points owned by gobby-cli or gsqz — matched by `project_id` payload **or** by their captured memory IDs (legacy payload-unscoped points) — are gone from every active and staged physical collection; genuinely global/unscoped and other-project point counts match the pre-purge snapshot; the purged projects' `Memory` nodes are gone from the memory knowledge graph while other projects' node counts match the snapshot.
   - Cron run rows record cleanup counts and `completed`; stopping Qdrant and re-triggering records a **failed** run (then restore).
   - Qdrant allocated and apparent volume sizes decrease.

**Acceptance:**

- 5.1.1 - Post-rollout, zero orphaned `code_symbols_*` UUID collections remain and the surviving set equals the post-purge `code_indexed_projects` rows. behavior: "live code-collection reconciliation" in `crates/gcode/src/commands/status/prune.rs`.
- 5.1.2 - Dead gwiki scopes and the one-time debt (non-UUID collections, junk topics, gobby-cli/gsqz) are fully reclaimed, and a forced-failure cron run records as failed. behavior: "live wiki/purge verification with failure visibility" in `src/gobby/projects/purge.py`.

## Task Mapping

`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

## V1 Plan Changelog

`kind: verification`

**Round 1** `kind: verification`

- reviewer_run: f23062ea-4009-4593-ac0b-294f024da0bf
- reviewer_session: 7be5f12b-22bf-43f0-8668-d5c3bf67c906
- verdict: needs_review
- findings:
  - R1-lock-api-contract/blocking: prescribed lock signature used nonexistent `IndexLockError` and leaked private types
  - R1-prune-confirmation/blocking: plain `gcode prune` could delete collections without any prompt when no stale projects exist
  - R1-cron-enabled-state/blocking: registration re-enables operator-disabled jobs; plan claimed preservation
  - R1-gwiki-command-wiring/blocking: new gwiki command missing api.rs/cli mapping/mod.rs dispatch targets
  - R1-project-purge-registration/blocking: daily purge job had no startup consumer
  - R1-retry-anchor/blocking: hard delete on best-effort failure loses cleanup authority for non-reconcilable stores; gwiki purge deletes SQL before projections
  - R1-memory-delete-filter/blocking: `memory_project_scope_filter` matches global/unscoped points and is incompatible with `VectorStore.delete`
  - R1-protected-project-purge/blocking: purge service lacked internal protection gate; `gobby` is in `SYSTEM_PROJECT_NAMES`
- resolution_notes: All 8 findings verified against the codebase and accepted. 1.1 now pins a `pub(crate)`/`anyhow::Result` contract with guard visibility raised to `pub(crate)`. 1.2 adds a combined non-force confirmation gate (new item 1.2.6). 2.1 takes the enabled-state clobbering fix into scope (new item 2.1.3); Constraints corrected. 3.1 targets full command wiring (api.rs, cli/mapping.rs, commands/mod.rs) and reorders shared purge to projections-before-SQL (new items 3.1.4, 3.1.5). 4.1 requires the `_ensure_wiki_cron_job` preserving pattern. 4.2 rewritten: internal `is_protected` gate on every entry path, exact-match `VectorStore.delete(filters={"project_id": ...})` vector cleanup, SQL transaction runs only after all derived stores succeed with the soft-deleted row as durable retry anchor, and startup wiring in `runner_init/orchestration.py` (new items 4.2.4, 4.2.5).

**Round 2** `kind: verification`

- reviewer_run: 8a48cc36-9142-4e35-8bb1-119b2ea6a630
- reviewer_session: 5a39e383-84c0-46ba-b7cf-bbb2a33a30b6
- verdict: needs_review
- findings:
  - R2-gwiki-project-id-selector/blocking: `gwiki purge --project` takes a filesystem root (`Option<PathBuf>`); no ID-native wiki cleanup path for rootless soft-deleted projects
  - R2-topic-prune-race/blocking: automated topic-collection deletion races a live topic ingest; no shared scope lock exists
  - R2-code-index-retry-contract/blocking: invalidate paths delete SQL before projections and GcodeGateway exposes no project purge API, breaking the retry-anchor guarantee
  - R2-project-purge-enabled-state/blocking: `gobby:project-purge` registration lacked the enabled-toggle-preserving invariant
  - R2-project-purge-batch-failure/blocking: daily handler lacked per-project failure isolation and an aggregate result contract
  - R2-rollout-oracle/blocking: hard-coded "15 collections remain" contradicts the gobby-cli purge; vector assertions did not verify point deletion
- resolution_notes: All 6 findings verified against the codebase (confirmed `--project: Option<PathBuf>` at `cli.rs:233` and `indexer::invalidate`-before-`cleanup_project_projections` in `invalidate.rs`) and accepted. New 1.3 adds ID-native, projection-first `gcode invalidate --project-id` (items 1.3.1, 1.3.2). 3.1 adds `gwiki purge --project-id` (item 3.1.6) and drops automated topic-collection deletion — topics are report-only for automation (item 3.1.2 rewritten; Constraints updated). 4.1 shells the ID-native selector. 4.2 now depends on P1, routes code-index cleanup through `GcodeGateway.invalidate_project_by_id` (item 4.2.8), pins the enabled-toggle invariant for the purge job (item 4.2.7), and defines batch isolation plus the aggregate mapping (item 4.2.6). 5.1's oracle derives the expected collection set from post-purge `code_indexed_projects` and snapshots vector points by `project_id` (item 5.1.1 updated).

**Round 3** `kind: verification`

- reviewer_run: 95da449e-4e12-407a-aa81-bcbe9f4955ce
- reviewer_session: 5d4bb6b6-8701-46c2-8c3c-f287a2fa8992
- verdict: needs_review
- findings:
  - R3-prune-confirmation-order/blocking: stale phase prompts and mutates before orphan discovery can run, so the promised combined pre-mutation confirmation was unimplementable in the specified order
  - R3-gcode-invalidate-lock/blocking: invalidate paths acquire no advisory lock; an overlapping indexer can recreate state mid-cleanup, and the plan never said how purge relates to post-cleanup re-indexing
  - R3-gwiki-purge-writer-race/blocking: the no-lock rationale covers absent-row prune scopes only — purge targets a soft-deleted row that still exists, and running-daemon wiki crons are not quiesced by soft deletion
  - R3-wiki-prune-zero-project-startup/blocking: `_register_wiki_cron_handlers` returns before any registration when project scopes and stale IDs are both empty, so the global job would never register in a zero-project state
  - R3-gwiki-missing-qdrant-branch/blocking: the missing-Qdrant "successful skip" never defined whether SQL and Falkor cleanup still run
  - R3-wiki-scheduled-jobs-monolith/blocking: `scheduled_jobs.py` is 954 lines; adding global-job machinery there risks the 1,000-line limit with no named extraction target
- resolution_notes: All 6 findings verified against the codebase (confirmed prompt-then-invalidate in `prune_stale_projects` at `prune.rs:139`, no lock in `invalidate.rs`, unfiltered `LocalProjectManager.get` at `projects.py:198`, the empty-scope early return in `_register_wiki_cron_handlers` at `runner_lifecycle_subsystems.py:429`, and `wc -l` = 954) and accepted. 1.2 restructured into discovery → single combined authorization prompt → mutation, with decline = zero mutations and a pinned stale/orphan matrix (item 1.2.6 rewritten, new 1.2.7). 1.3 now depends on 1.1 and runs both modes under the project advisory lock with busy → nonzero exit, plus an explicit statement that post-invalidate re-indexing is legitimate root-path-authoritative state (new item 1.3.3). 4.2 gains a wiki writer-quiescence step — delete the project's wiki jobs via `delete_system_jobs_by_project_and_name_prefix`, drain in-flight runs with a bounded wait that fails the purge, and add a `deleted_at`-aware liveness guard to every per-project wiki handler (new items 4.2.9, 4.2.10; Constraints updated). 4.1 moves the global handler/registration into new module `src/gobby/wiki/prune_job.py` and registers it in `_register_wiki_cron_handlers` before the empty-scope early return (new item 4.1.3). 3.1 defines the missing-Qdrant branch as a Qdrant-only skip with SQL and Falkor cleanup continuing (new item 3.1.7).

**Round 4** `kind: verification`

- reviewer_run: 053a3e3b-f6d8-4aef-9cf0-2d12baea187b
- reviewer_session: 97ce297f-f414-4e85-af82-4f4a724ef241
- verdict: needs_review
- findings:
  - R4-gcode-stale-invalidation-contract/blocking: the stale phase still calls `indexer::invalidate` unlocked while indexing holds the project lock, and it strands the project's Falkor projection — `prune_all_project_projections` iterates only remaining registered projects
  - R4-wiki-quiescence-drain-order/blocking: `delete_system_jobs_by_project_and_name_prefix` deletes `cron_runs` before `cron_jobs`, blinding the planned drain; `gobby:codewiki-nightly:<project_id>` is a wiki-writing project job outside the wiki prefix
  - R4-gwiki-falkor-anchor-loss/blocking: `purge_falkor_scope` returns a successful skip for `config=None`, so scope purge can delete the SQL discovery anchor and permanently strand Falkor graph state
  - R4-project-derived-store-inventory/blocking: purge missed `gobby_github_issues` (project_id-payload points) and the memory knowledge graph (`clear_project_graph`), neither SQL-cascaded
  - R4-project-derived-writer-fence/blocking: no fence for in-flight non-cron writers — `create_memory` and `GitHubIssueIndexer.upsert` can land project-scoped points/nodes after the hard delete
- resolution_notes: All 5 findings verified against the codebase (confirmed `with_project_lock` around index SQL+projections at `index.rs:62` vs the unlocked stale path; `collect_projects()`-only iteration in `prune_all_project_projections` at `prune.rs:390`; runs-before-jobs deletion order in `cron.py:674`; active-only `list_active_runs` at `cron_runs.py:270`; `gobby:codewiki-nightly:` naming at `codewiki_nightly.py:58`; `Ok(skipped)` for `config=None` in `purge_falkor_scope` at `purge.rs:186`; `gobby_github_issues` in `EMBEDDING_COLLECTION_KINDS` and per-point `project_id` payloads at `issue_index.py:181`; error-swallowing `clear_project_graph` at `maintenance.py:173`; SQL-then-vector-then-graph ordering in `create_memory` at `lifecycle.py:179`) and accepted. 1.2 now depends on 1.3 and runs the stale phase per project under the advisory lock via 1.3's factored projection-first sequence — Falkor scope, Qdrant collection, and SQL removed in one guarded pass, busy defers the whole project (mutation step rewritten, new item 1.2.8). 3.1 adds graph-side Falkor project-scope reconciliation with unreachable-aborts and defines the missing-Falkor branch as an honest Falkor-only skip (item 3 extended, new 3.1.8; 4.2's retry-anchor paragraph updated to match). 4.2's quiescence is reordered to disable → drain → delete with codewiki-nightly included and its handler guarded (step 1 rewritten, items 4.2.9/4.2.10 rewritten). 4.2's inventory now covers `gobby_github_issues` exact-match deletion and a fail-visible `clear_project_graph` variant (step 5, item 4.2.4 extended, new 4.2.11; 5.1 snapshots extended). A per-project derived-writer fence in new `src/gobby/projects/write_fence.py` gates memory vector/graph, issue-index, and daemon-mediated gwiki writes, held by purge through the hub transaction with liveness-rejecting writer admission (new step 2, new item 4.2.12).

**Round 5** `kind: verification`

- reviewer_run: 31f9b53c-6fef-4449-976d-a24807d15d82
- reviewer_session: 31ffcea0-5b96-482d-b9f3-445250a7d158
- verdict: needs_review
- findings:
  - R5-gcode-falkor-missing-config-anchor/blocking: the shared invalidation contract had no safe missing-Falkor branch — `cleanup_project_projections` silently skips graph cleanup when Falkor is unconfigured, and gcode had no graph-side scope enumerator, so a stale/invalidate SQL delete under missing config would permanently strand the Falkor scope
  - R5-gwiki-sql-scope-discovery/blocking: dead-scope discovery enumerated only `gwiki_documents`, but the five `GWIKI_POSTGRES_TABLES` tables are FK-free and `record_ingestion` inserts into `gwiki_ingestions` without a document row, so SQL-only orphan scopes in the other four tables were invisible
  - R5-derived-writer-fence-inventory/blocking: the fence inventory missed tool-embedding upserts (`semantic_search.py`), memory reindex (`indexing.py`), knowledge-graph apply (`knowledge_graph/service.py`), and embedding-switch rebuilds (`embedding_switch_runner.py`)
  - R5-inprocess-fence-external-gwiki/blocking: standalone gwiki CLI mutation commands resolve a project ID from disk with no liveness admission and write SQL/Qdrant/Falkor directly — an external process cannot observe a Python in-process fence
- resolution_notes: All 4 findings verified against the codebase (confirmed the `ctx.falkordb.is_some()` silent skip in `cleanup_project_projections` at `invalidate.rs:35` and the skip-preserves-row contrast in `cleanup_orphan_project_projections` at `prune.rs:263`; the five-table `GWIKI_POSTGRES_TABLES` constant at `setup.rs:10` and anchor-free `record_ingestion` at `postgres.rs:385`; project-scoped upserts with no liveness check in `store_embedding` at `semantic_search.py:215`, `reindex_embeddings` at `indexing.py:380`, `KnowledgeGraphService` mutations, and the all-projects rebuild loop in `_build_tool_collection` at `embedding_switch_runner.py:343`; standalone write commands at `cli.rs:118`, admission-free `resolve_project_from_root` at `scope.rs:131`, and direct SQL/Qdrant/Falkor writes in `commands/index.rs`) and accepted. 1.2 adds a graph-side Falkor scope sweep over the shared code graph's `project` node property (`clear_project_query` at `deletion.rs:191` proves enumerability; new 1.2.9), which licenses 1.3's pinned missing-config semantics: missing backend config is a per-backend skip that still completes SQL because both backends now have global reconcilers, while configured-but-unreachable aborts before SQL (new 1.3.4). 3.1's SQL discovery is now the union of distinct project scopes across all five tables with an ingestion-only acceptance case (item 2 rewritten, new 3.1.9). New deliverable 3.2 adds a cross-process per-project gwiki advisory lock (own keyspace, cross-language key test vector, liveness recheck under the lock in every project-scoped mutation command; 3.1 now depends on it and counts real `busy` deferrals), replacing the former no-gwiki-locking constraint. 4.2's fence inventory is completed with the four missed writers (targets extended; step 2 rewritten as an explicit chokepoint list; embedding-switch rebuilds skip dead projects; 4.2.12 race tests extended), and purge gains a cross-process drain barrier on the 3.2 key — acquire post-soft-delete to flush pre-soft-delete external writers, release before delegating to the self-locking gwiki subprocess, safety after release resting on under-lock liveness refusal (new 4.2.13; 4.2.1 preconditions extended; 5.1 verifies code-graph scopes equal the post-purge registered set).

**Round 6** `kind: verification`

- reviewer_run: 56d34eff-8eee-4cb8-9366-13386a3b5637
- reviewer_session: 753885cf-2819-4e34-94d0-029c7a252e19
- verdict: needs_review
- findings:
  - R6-gcode-falkor-discovery-authorization-gap/blocking: the Falkor scope sweep lived inside mutation while discovery only resolved config and the prompt covered only stale projects and Qdrant orphans — graph-only orphans could be deleted unprompted, and Falkor unreachability could surface after other backends had already mutated
  - R6-gwiki-sync-sessions-unfenced/blocking: `sync-sessions` is a standalone project-scoped persistent writer (Postgres store, then Qdrant and Falkor sync) outside 3.2's command list and targets, bypassing the advisory lock
  - R6-gwiki-cleanup-admission-contradiction/blocking: 3.2's single refuse-unless-live rule made both cleanup paths impossible — purge must operate on a soft-deleted row, prune on an absent one — and prune lacked an under-lock absence re-check against a concurrently re-created project
  - R6-gwiki-drain-lock-lifetime/blocking: neither 3.2.2 nor 4.2.13 pinned the writer guard lifetime; a guard dropped after admission would let a pre-soft-delete writer outlive the Python drain barrier mid-command
  - R6-derived-writer-inventory-still-incomplete/blocking: the fence inventory missed the detached background-dedup task, `reconcile_stores`/`_backfill_embeddings`, the async startup vector rebuild, and the memory/GitHub-issue embedding-switch builders
  - R6-embedding-switch-staged-collection-race/blocking: builds write staged `kind@run_id` physicals and flip aliases later — purging only active aliases lets a project staged while live reappear when flip activates after the hard delete
  - R6-unscoped-memory-rebuild-purge-gap/blocking: the startup rebuild writes payloads with only `id`/`content`, so project memories become payload-unscoped points the exact-match purge filter preserves as "global"
- resolution_notes: All 7 findings verified against the codebase (confirmed `session_sync::execute` writing the Postgres store then `sync_qdrant_vectors`/`sync_falkor_graph` at `session_sync.rs:42` and the full write-arm dispatch table at `commands/mod.rs:44`; the detached `asyncio.create_task` in `fire_background_dedup` at `lifecycle.py:111` → `_embed_and_upsert` at `dedup.py:220`; `reconcile_stores` → `_backfill_embeddings` at `indexing.py:259`; the background `rebuild_vector_store` at `runner_maintenance.py:208` called from `runner_lifecycle_subsystems.py:311`; all three switch builders at `embedding_switch_runner.py:308/343/377` and alias flip onto staged physicals at `:253` with `kind@run_id` naming in `collection_names.py:26`; the id/content-only `memory_dicts` at `runner_lifecycle_subsystems.py:309` and payload-from-supplied-keys-only `VectorStore.rebuild` at `vectorstore.py:821`) and accepted. 1.2 moves Falkor scope enumeration and connectivity into zero-mutation discovery, computes existing and would-be graph orphans there, adds them to the single combined prompt including graph-only runs, and makes configured-but-unreachable Falkor abort the entire run before any mutation (steps 1–3, 1.2.7, 1.2.9 rewritten). 3.2 now derives its writer inventory from the `commands::run` dispatch table with a classification test that fails on unclassified new variants, adds `session_sync.rs` and `commands/mod.rs` to targets, splits admission into three explicit contracts — writers require a live row under a guard spanning the entire command through the last persistent write; explicit purge is serialized cleanup over its authorized live/soft-deleted/absent target; prune re-queries under the lock and proceeds only while the row remains absent — and pins the guard lifetime the drain proof depends on (items 2–4 rewritten, 3.2.2/3.2.3 extended, new 3.2.4 guard-lifetime race; 3.1 item 2 gains the under-lock absence re-check). 4.2's fence inventory adds the background-dedup task, reconcile/backfill, and the startup rebuild, with admission carried by spawned tasks for their full lifetime, and names all three embedding-switch builders (targets extended with `dedup.py`, `runner_maintenance.py`, `runner_lifecycle_subsystems.py`, `collection_names.py`, `vectorstore.py`; 4.2.12 races extended per path). Step 5 now enumerates every active and staged physical collection of the three managed kinds at cleanup time and deletes by exact-match `project_id` **plus** captured memory IDs, fixing the startup-rebuild payload to include `project_id` at the source (4.2.4 rewritten; new 4.2.14 staged-collection race and 4.2.15 scoped-payload regression; 5.1 verification updated to captured-ID semantics).

**Round 7** `kind: verification`

- reviewer_run: ea783d28-3b3c-4ee7-98bd-08cbd1733a3b
- reviewer_session: 9a754387-9be7-40ca-9bbd-70c01d0e137e
- verdict: needs_review
- findings:
  - R7-embedding-switch-cross-process-fence-gap/blocking: `gobby embeddings switch start`/`resume` run `EmbeddingSwitchRunner` directly in the standalone CLI process while 4.2 relies on an in-process daemon fence — a CLI switch passes only its own process-local admission, can pause before a staged upsert, survive the daemon purge's drain (which sees no admission), resume after the hard delete, write purged-project points, and have flip activate them; the drain-then-enumerate proof did not hold across the actual process boundary for any of the three kinds
- resolution_notes: Verified against the codebase and accepted — `_switch_start` calls `asyncio.run(start_embedding_switch(...))` at `src/gobby/cli/embeddings.py:184` and the resume subcommand calls `asyncio.run(resume_embedding_switch(...))` at `:152`, with `start_embedding_switch`/`resume_embedding_switch` (`src/gobby/ai/embedding_switch_runner.py:100,129`) running `EmbeddingSwitchRunner(...).run(journal)` in the calling process; a gcode grep over `src/` confirmed these CLI call sites are the **only** invokers, so every switch today executes outside the daemon fence. Resolved by moving switch execution in-daemon rather than building a parallel cross-process lock: 4.2 item 2's builder bullet now specifies a daemon endpoint in `src/gobby/servers/routes/embeddings.py` executing start/resume as a daemon background task under the fence, the CLI subcommands as thin clients, deletion of the standalone execution path with no fallback, zero-persistent-write refusal when the daemon is unreachable, and per-project admission held from the admission-time live-row check through that project's final staged write in all three builders. The step-2 closing paragraph and the Constraints writer-fencing bullet now state the switch is brought inside the daemon (leaving gwiki and gcode as the only out-of-daemon writers, covered by the 3.2 and 1.1/1.3 locks), 4.2.12 notes in-daemon-only builder execution, new acceptance 4.2.16 pins the delegation, the daemon-down zero-write refusal, and the pause-after-admission-before-staged-write race proving purge drains the builder or fails at the bound with the retry anchor intact and flip exposing zero purged-project points, and targets gain `src/gobby/cli/embeddings.py` and `src/gobby/servers/routes/embeddings.py`.

**Round 8** `kind: verification`

- reviewer_run: 83d1cca2-bab8-4d94-ace6-cae24eef669d
- reviewer_session: 4f0e046f-585c-43fc-84e2-95b908a37f5e
- verdict: needs_review
- findings:
  - R8-embedding-switch-background-abort-race/blocking: moving only start/resume in-daemon leaves `gobby embeddings switch --abort` as a standalone journal mutation — `_switch_abort` (`src/gobby/cli/embeddings.py:136`) calls `abort_switch` (`src/gobby/ai/embedding_switch.py:139`), which deletes the persisted journal while a live runner keeps its in-memory copy; `EmbeddingSwitchRunner.build` (`src/gobby/ai/embedding_switch_runner.py:232`) then calls `advance_phase` (`src/gobby/ai/embedding_switch.py:253`), which unconditionally rewrites that copy — resurrecting the aborted journal and proceeding to flip; concurrent start/resume also had no stated single-flight owner
- resolution_notes: Verified against the codebase and accepted — all four anchors confirmed: `_switch_abort` mutates the journal standalone, `abort_switch` never touches a running task, and `advance_phase`'s unconditional `_write_journal` makes journal resurrection after a CLI abort real. Resolved by making the daemon own the **complete mutating switch lifecycle**: the 4.2 builder bullet now specifies daemon endpoints handling start, resume, **and abort** (all three CLI subcommands become thin clients; every standalone mutation path deleted), exactly one active switch task with single-flight admission (concurrent start/resume rejected with the active run's ID), abort cancelling and awaiting the active task before deleting its journal or reporting success, and restart semantics resuming a persisted journal only as one newly fenced task through the same gate. The Constraints writer-fencing bullet now names `--abort` (`src/gobby/cli/embeddings.py:136`) as a standalone mutation moved in-daemon, 4.2.16 extends the zero-write daemon-down refusal to `--abort`, new acceptance 4.2.17 pins single-flight ownership, cancel-and-await abort, restart resume-as-one-task, and the required race test (pause mid-builder, abort, prove no later staged write, no alias flip, no journal resurrection via the `advance_phase` path), and targets gain `src/gobby/ai/embedding_switch.py`. Review cap note: max_review_rounds = 8 is exhausted with this round; the resolution is recorded for user disposition rather than an automatic round 9.

**Round 9** `kind: verification`

- reviewer_run: 6d1ef36c-4e60-430d-a8ef-a9802987ebea
- reviewer_session: 45b073b5-e6e2-43e8-acb4-3a04828ae86d
- verdict: needs_review
- findings:
  - R9-switch-journal-configstore-key-rejection/blocking: the daemon owner cannot persist its journal through the real ConfigStore — `_SWITCH_JOURNAL_KEY = ai.embeddings.switch_run` (`src/gobby/ai/embedding_switch.py:28`) is rejected by `validate_embedding_storage_config_key` (`src/gobby/storage/config_store.py:66`; `src/gobby/config/embedding_keys.py:19,111`), which admits only the six canonical fields; existing switch tests mock the store, hiding a production-breaking failure, and bulk config reset/`delete_all` would erase an accepted journal
  - R9-embedding-switch-abort-threaded-flip-race/blocking: cancel-and-await is not a safe abort barrier — Qdrant mutations run via `asyncio.to_thread` and cancelling the awaiting task leaves the worker thread running; flip repoints three aliases in separate awaits, so abort could report success while an upsert or half-done alias flip still lands
  - R9-embedding-switch-abort-staged-artifact-leak/blocking: pre-flip abort deleted the journal without cleaning the run's staged `kind@run_id` physicals; no reconciler owns those families, so a mid-build abort permanently strands partial collections and erases their only cleanup anchor
  - R9-embedding-switch-mutation-owner-bypasses/blocking: `gobby install` → `_persist_embedding_config` (`src/gobby/cli/installers/embedding.py:426`) `set_many`-writes canonical embedding config standalone, and generic config set/batch/import can overwrite canonical keys without entering the switch single-flight gate
- resolution_notes: Round 9 ran as a user-approved one-round extension past the max_review_rounds = 8 cap; all four findings were verified against the codebase (validator field tuple and raise path, the 25+ `asyncio.to_thread` mutation sites in `src/gobby/memory/vectorstore.py`, the alias-by-alias flip loop at `src/gobby/ai/embedding_switch_runner.py:267`, `abort_switch` touching only the journal, and the installer's direct `set_many` at `src/gobby/cli/installers/embedding.py:475`) and accepted; the user then chose to absorb all four resolutions and run round 10. The 4.2 builder bullet replaces cancel-and-await with phase-aware cooperative abort — settle every in-flight persistent operation, stop only at safe checkpoints pre-FLIPPING, make FLIPPING a non-cancellable forward-completion critical section through aliases, canonical config, and journal transition — and adds staged-artifact cleanup before journal deletion with a durable cleanup-pending journal on failure. A new step-2 contract paragraph defines the internal lifecycle-key journal persistence contract (real-ConfigStore admission only via the lifecycle owner, public set/batch/import rejection, reset/import/`delete_all` preservation) and confines installer embedding-config writes to proven first bootstrap with live-switch generic-mutation gating. The Constraints writer-fencing bullet records the journal-key production defect and the installer bootstrap-only exception. Acceptance: 4.2.17 rewritten to cooperative-abort semantics with the four required race points (inside a `to_thread` upsert, inside `create_alias`, between alias repoints, between config write and journal transition); new 4.2.18 pins real-ConfigStore journal persistence with unmocked lifecycle tests, new 4.2.19 pins staged-artifact cleanup before journal deletion with failure-retry, new 4.2.20 pins complete mutation ownership across installer and generic config surfaces. Targets gain `src/gobby/storage/config_store.py`, `src/gobby/config/embedding_keys.py`, and `src/gobby/cli/installers/embedding.py`.

**Round 10** `kind: verification`

- reviewer_run: 2dd29a4e-8276-4646-b422-da3b66cdff1a
- reviewer_session: f27aa8c9-381e-4c07-8a40-0e3d95b68df8
- verdict: needs_review
- findings:
  - R10-switch-journal-runtime-config-contamination/blocking: write-side admission of `ai.embeddings.switch_run` without a read-side split crashes the daemon the moment a journal exists — `load_config` feeds the full `config_store` dump through `storage_embedding_config_entries_to_runtime` (`src/gobby/config/app.py:623`), whose mapper raises on any non-canonical `ai.embeddings.*` key (`src/gobby/config/embedding_keys.py:139`), and the switch runner itself calls `load_config` mid-build (`src/gobby/ai/embedding_switch_runner.py:347`); the same `get_all` path leaks a live journal into export/template output that the public-key rejection then cannot re-import
  - R10-embedding-config-owner-surface-and-admission-bypasses/blocking: the claimed complete owner covered only set/batch/import plus the installer — canonical fields and the `embeddings_api_key` secret are also mutable via MCP `delete_config`/`ensure_defaults` (`src/gobby/mcp_proxy/tools/config.py`), HTTP `reset_config` (`src/gobby/servers/routes/configuration_values.py:259`), template save's raw `DELETE FROM config_store` in `delete_all_except` (`src/gobby/servers/routes/configuration_secrets.py:62`), import secret writes, `ConfigStore.delete`/`delete_all`/`set_secret`/`clear_secret`, and CLI/server secret CRUD, none in §4.2's targets; the admission check was also not atomic with switch start — only start takes `transaction_immediate(EmbeddingSwitchJournalMutation)`, so a writer can observe "no journal", race a start, and write afterward (TOCTOU)
  - R10-embedding-switch-abort-postflip-gc-window/blocking: the non-cancellable region ended at the FLIPPING→ACTIVE journal transition, but `gc` (`src/gobby/ai/embedding_switch_runner.py:288`) then runs with the journal persisted at ACTIVE across awaited `get_aliases`/`delete_collection` calls until `complete_switch` (`:305`), and `flip` deletes an old physical mid-loop (`:271`); an abort treating only `phase == FLIPPING` as too late could run pre-flip staged cleanup against `kind@run_id` collections that are now live alias targets or cancel a `to_thread` old-collection deletion
- resolution_notes: Round 10 ran as the second user-approved extension past the max_review_rounds = 8 cap; all three findings were verified against the codebase (the raising key mapper and its `load_config` call site, the runner's mid-build `load_config`, every listed mutation surface including the raw `DELETE FROM config_store` helper and the secret CRUD paths, and the ACTIVE-phase `gc` window) and accepted; the user then chose to absorb all three resolutions and run round 11. The step-2 contract paragraph (a) now defines the read side of the lifecycle-key contract — owner keyed get only; runtime config loading, public get/list, template generation, and export omit lifecycle keys; import rejects injected lifecycle keys while preserving the journal — with real-store read-side proofs (live journal → successful `load_config` and staged build, export/template/list omission, export→import round-trip). Paragraph (b) now carries the complete mutation inventory (MCP delete/defaults, HTTP values/reset, template bulk replacement, import secret writes, `ConfigStore` delete/delete_all/set_secret/clear_secret, CLI secret CRUD) with every surface's check-and-write executed under the same `transaction_immediate(EmbeddingSwitchJournalMutation)` admission lock as switch start, closing the TOCTOU. The builder bullet's critical section is widened to an irreversible forward-completion region spanning FLIPPING, ACTIVE, and GC keyed on "has FLIPPING ever begun" — no staged-run cleanup, no GC cancellation, join-or-report, journal retained on GC failure for restart retry. Acceptance: 4.2.17 rewritten with the region semantics and two new race points (after the ACTIVE write before `gc`; inside `gc`'s `to_thread` deletion) plus no-active-alias-target-deleted and no-worker-outlives-abort proofs; 4.2.18 gains the read-side omission and round-trip proofs; 4.2.19 scoped to pre-flip aborts with forward-completion after FLIPPING; 4.2.20 rewritten to the full inventory with simultaneous-barrier races per mutation class and live-switch zero-write tests. The Constraints defect note records the read-side crash. Targets gain `src/gobby/config/app.py`, `src/gobby/mcp_proxy/tools/config.py`, `src/gobby/servers/routes/configuration_values.py`, `src/gobby/servers/routes/configuration_secrets.py`, `src/gobby/servers/routes/configuration_templates.py`, `src/gobby/servers/routes/configuration_import_export.py`, and `src/gobby/cli/secrets.py`.

**Round 11** `kind: verification`

- reviewer_run: 91baa8be-8880-4156-9337-4c0409301778
- reviewer_session: 1347c71c-6adc-4d67-aed8-bf9799d0aa9d
- verdict: approved
- findings: none
- resolution_notes: Round 11 ran as the third user-approved extension past the max_review_rounds = 8 cap, scoped to the three absorbed Round-10 resolutions. The adversary re-verified the read-side lifecycle-key contract across the `get_all` consumers, the complete mutation inventory and shared admission lock, and the irreversible forward-completion region against the real runner's flip/gc paths, swept the whole plan for regressions from the Round-10 edits, and returned zero findings. The adversary wrote `## M1 Task Manifest` and reported `manifest: written`; `uv run gobby plans validate --mode expansion` passes. Convergence reached after 11 rounds.

## M1 Task Manifest

`kind: manifest`

```yaml
- title: Add project-identity advisory lock entry point
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "Focused gcode lock tests pass for acquisition and busy-lock deferral."
  labels:
    - covers:orphaned-derived-state-reconciliation:1.1:1.1.1
    - covers:orphaned-derived-state-reconciliation:1.1:1.1.2
  implementation_domain: backend
  tdd: true
  source_section: "1.1"

- title: Add global Qdrant and Falkor reconciliation to gcode prune
  category: code
  task_type: feature
  depends_on: ["1.1", "1.3"]
  validation_criteria: "Focused gcode prune tests pass for discovery, authorization, locking, reconciliation, and failure visibility."
  labels:
    - covers:orphaned-derived-state-reconciliation:1.2:1.2.1
    - covers:orphaned-derived-state-reconciliation:1.2:1.2.2
    - covers:orphaned-derived-state-reconciliation:1.2:1.2.3
    - covers:orphaned-derived-state-reconciliation:1.2:1.2.4
    - covers:orphaned-derived-state-reconciliation:1.2:1.2.5
    - covers:orphaned-derived-state-reconciliation:1.2:1.2.6
    - covers:orphaned-derived-state-reconciliation:1.2:1.2.7
    - covers:orphaned-derived-state-reconciliation:1.2:1.2.8
    - covers:orphaned-derived-state-reconciliation:1.2:1.2.9
  implementation_domain: backend
  tdd: true
  source_section: "1.2"

- title: Add ID-native projection-first lock-guarded gcode invalidation
  category: code
  task_type: feature
  depends_on: ["1.1"]
  validation_criteria: "Focused gcode invalidate tests pass for rootless selection, projection-first ordering, locking, and backend failure semantics."
  labels:
    - covers:orphaned-derived-state-reconciliation:1.3:1.3.1
    - covers:orphaned-derived-state-reconciliation:1.3:1.3.2
    - covers:orphaned-derived-state-reconciliation:1.3:1.3.3
    - covers:orphaned-derived-state-reconciliation:1.3:1.3.4
  implementation_domain: backend
  tdd: true
  source_section: "1.3"

- title: Return structured global gcode prune results
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: "Focused code-index prune tests pass for structured failures and enabled-state-preserving job reconciliation."
  labels:
    - covers:orphaned-derived-state-reconciliation:2.1:2.1.1
    - covers:orphaned-derived-state-reconciliation:2.1:2.1.2
    - covers:orphaned-derived-state-reconciliation:2.1:2.1.3
  implementation_domain: backend
  tdd: true
  source_section: "2.1"

- title: Add gwiki prune and ID-native purge commands
  category: code
  task_type: feature
  depends_on: ["3.2"]
  validation_criteria: "Focused gwiki command tests pass for dead-scope reconciliation, rootless purge, backend cleanup, locking, and observability."
  labels:
    - covers:orphaned-derived-state-reconciliation:3.1:3.1.1
    - covers:orphaned-derived-state-reconciliation:3.1:3.1.2
    - covers:orphaned-derived-state-reconciliation:3.1:3.1.3
    - covers:orphaned-derived-state-reconciliation:3.1:3.1.4
    - covers:orphaned-derived-state-reconciliation:3.1:3.1.5
    - covers:orphaned-derived-state-reconciliation:3.1:3.1.6
    - covers:orphaned-derived-state-reconciliation:3.1:3.1.7
    - covers:orphaned-derived-state-reconciliation:3.1:3.1.8
    - covers:orphaned-derived-state-reconciliation:3.1:3.1.9
  implementation_domain: backend
  tdd: true
  source_section: "3.1"

- title: Add cross-process gwiki project writer lock
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: "Focused gwiki lock and mutation-command tests pass for writer serialization, prune deferral, and liveness admission."
  labels:
    - covers:orphaned-derived-state-reconciliation:3.2:3.2.1
    - covers:orphaned-derived-state-reconciliation:3.2:3.2.2
    - covers:orphaned-derived-state-reconciliation:3.2:3.2.3
    - covers:orphaned-derived-state-reconciliation:3.2:3.2.4
  implementation_domain: backend
  tdd: true
  source_section: "3.2"

- title: Add gwiki gateway reconciliation and wiki-prune system job
  category: code
  task_type: feature
  depends_on: ["3.1", "3.2"]
  validation_criteria: "Focused gateway and lifecycle tests pass for global registration, failure reporting, and disabled-job preservation."
  labels:
    - covers:orphaned-derived-state-reconciliation:4.1:4.1.1
    - covers:orphaned-derived-state-reconciliation:4.1:4.1.2
    - covers:orphaned-derived-state-reconciliation:4.1:4.1.3
  implementation_domain: backend
  tdd: true
  source_section: "4.1"

- title: Add project purge service, retention cron, and lifecycle-safe surfaces
  category: code
  task_type: feature
  depends_on: ["1.1", "1.2", "1.3", "4.1"]
  validation_criteria: "Focused purge, fence, embedding-switch, config, CLI, HTTP, cron, and gateway tests pass for all twenty acceptance contracts."
  labels:
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.1
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.2
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.3
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.4
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.5
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.6
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.7
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.8
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.9
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.10
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.11
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.12
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.13
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.14
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.15
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.16
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.17
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.18
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.19
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.20
    - covers:orphaned-derived-state-reconciliation:4.2:4.2.21
  implementation_domain: backend
  tdd: true
  source_section: "4.2"

- title: Verify live reconciliation and perform one-time cleanup
  category: test
  task_type: test
  depends_on: ["1.1", "1.2", "1.3", "2.1", "4.1", "4.2"]
  validation_criteria: "Release binaries and daemon pass live orphan reconciliation, cleanup, and forced-failure visibility checks."
  labels:
    - covers:orphaned-derived-state-reconciliation:5.1:5.1.1
    - covers:orphaned-derived-state-reconciliation:5.1:5.1.2
  tdd: false
  source_section: "5.1"
```
