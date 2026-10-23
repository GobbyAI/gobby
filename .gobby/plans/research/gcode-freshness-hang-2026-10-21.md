# gcode non-stale hang after the 19:54Z restart

Researcher gobby#17246, 2026-10-21. Read-only diagnosis requested by the Orchestrator.

## Summary

- **Status:** the hang has cleared and no longer reproduces. At 20:19Z, `gcode outline src/gobby/tasks/landing_policy.py` without `--allow-stale` returned in 2.7 s in the main checkout. `gcode status` was healthy and reported `last_indexed_at` 2026-10-21 20:17:23Z.
- **Root cause:** unconfirmed. The evidence that would settle it is `pg_stat_activity` plus `pg_blocking_pids()` captured during the hang, and nobody captured it. This seat can't query PostgreSQL, and the sandbox blocks Docker logs.
- **Where the wait was:** elimination narrows it to one place. The freshness refresh writes to the index (`refresh_files` → `index_files`), and nothing bounds how long those writes can wait on a PostgreSQL row, relation or BM25 index lock. Every other step on the non-stale path is bounded or was proved working by `--allow-stale`.
- **Fix:** give the freshness write connection a `lock_timeout` and a backstop `statement_timeout`. A timeout then falls through the existing `Degraded` branch ("serving existing index") with no new error mapping. That matches the crate's own rule that a gcode process must never park indefinitely in a PostgreSQL lock wait (#17701, #21233).
- **No-code recovery:** `--allow-stale` works as before. If the hang recurs, run the diagnostic query in "Next occurrence" below, then the operator cancels the blocking backend. Killing gcode clients does not help, because PostgreSQL does not reclaim a backend parked in a lock wait.

## What changed at the restart

- **Binary:** `~/.gobby/bin/gcode` has an mtime of 2026-10-21 12:11:37 CDT, so the binary that worked before the restart is the one that hung after it. The indexer version is 1.9.45. No version bump forced a full reindex.
- **Daemon log** (`~/.gobby/logs/daemon.log`, lines 23647-23656):
  - 14:54:28 CDT: BM25 indexes verified healthy.
  - 14:54:30 CDT: maintenance loop and sync worker started.
  - No code-index errors appear afterwards. `errors.log`, `hooks.log` and `mcp.log` have no gcode, freshness or timeout lines between 14:54 and 15:19 CDT.
- **Maintenance:** `code_index_maintenance_loop` runs `_run_maintenance` once at startup, before its first interval wait (`src/gobby/code_index/maintenance.py:36-97`). That pass launches a maintenance `gcode index` for every indexed project, then summaries and community labels. It logs nothing when it succeeds. The gobby project's community-label batch logged at 14:56:00, so that project's maintenance index had finished by then.

## Non-stale path inventory

The `--allow-stale` call succeeded in 0.01 s. That proves grant resolution, the PostgreSQL connection, runtime schema validation and the symbol reads all worked during the hang. The only code that runs without `--allow-stale` is `freshness::ensure_fresh`:

`crates/gcode/src/freshness.rs:48-95`, excerpt_hash `be0ab95141c13792793caa5d80b073799c8680cb39eb2387bd82b9392bb06428`

| Step | Bound | Notes |
| --- | --- | --- |
| `can_refresh_index` (`config/context.rs:331-339`) | connect 5 s, one SELECT | `DEFAULT_CONNECT_TIMEOUT` is 5 s (`crates/gcore/src/postgres.rs:22`, `793c34f326373b1b85ebcdb3747ce48a507ca6ef56160837998e4d432f2e9357`). A plain MVCC read; nothing in gcode takes a relation-level exclusive lock. |
| `project_needs_refresh` (status only, `freshness.rs:135-179`) | SELECTs plus a tree walk | Reads only. The tree walk covers 8,609 files and is fast. |
| Project advisory lock (status) / shared gate plus file lock (outline) | 150 ms | `brief_freshness_try` = `maintenance_try` = 150 ms total, 25 ms poll (`index_lock.rs:46-55`, `70e8c1b32ff995e0026ef925213515d6a91be07e55c63dac9f9fbf9cf4aa7b55`). The loop calls non-blocking `pg_try_advisory_lock` and returns `Busy` (`index_lock.rs:396-445`). |
| `describe_lock_holder` | one catalog SELECT | Runs only on contention, then prints a `WARN` to stderr and serves the existing index. That gives output, not a silent hang. |
| `refresh_files` → `index_files` → `connect_readwrite` + writes | **unbounded** | No `statement_timeout` or `lock_timeout` is set on any gcode production connection. The only `lock_timeout` in the Rust tree is the schema runner (`crates/gcore/src/schema/runner.rs:389-405`, `29ff0040b6c8b64393135abc42ea5e02e721019d63acd3f5a2c04f317c617d00`). |

The crate's own design forbids an unbounded lock wait for advisory locks. `brief_index_flush_try` documents why (`index_lock.rs:56-66`, `3c2926e29c82249029b9aa08ccd1871abd6151511f638fd9c4896fa2a5c356c2`): a backend parked in a PostgreSQL lock wait survives the death of its client, and that produced the #17701 waiter pileup. The same failure applies to row and index lock waits inside the write path, and nothing guards those.

## Lock points every refresh write passes through

- **Checkout row.** Every primary index write begins with `upsert_project_seed` (`crates/gcode/src/index/api.rs:336-353`, `fa0439a5e2a5d4525c58624183899bcbf48652a2fca8c39ea5c022756bd97124`). It runs `INSERT … ON CONFLICT DO UPDATE SET updated_at=NOW()` on the `code_indexed_projects` row, then upserts `code_indexed_project_states` under `SELECT … FOR SHARE` on the matching `project_checkouts` row. The project-checkout-identity plan (`.gobby/plans/completed/project-checkout-identity.md:610`) requires every primary write, Python `upsert_file`/`delete_file` included, to take that `FOR SHARE`, and a different-root rebind takes `FOR UPDATE`. Any open transaction that updated either row blocks every primary refresh until it ends.
- **BM25 indexes.** Inserts into `code_symbols_search_bm25` and `code_content_search_bm25` (pg_search) are the one write surface shared by the main checkout and a worktree overlay. Overlay writes take no checkout lock, so a blocker that hung both the main checkout and `task-23850` together sits on a surface they share: the BM25 indexes, or a blocker active on each separately. The worktree hang comes from the Orchestrator's observation. I could not re-test it, because this seat's grant refuses that overlay with `code_overlay_mismatch`.

## Most consistent hypothesis

1. After the restart, some backend held a lock that refresh writes need: a row lock on `code_indexed_projects`, a `project_checkouts` lock stronger than share, or a pg_search index lock.
2. Every non-stale gcode call took its advisory locks in under 150 ms, then blocked in its first write with no output. That matches "hangs with no output" in both commands and both checkouts.
3. Each `perl alarm` kill left that call's backend parked in PostgreSQL.
4. The blocker released some time before 20:17:23Z, when a refresh committed and advanced `last_indexed_at`. Gcode calls have worked since.

I can't name the blocker from here. Plausible candidates are daemon-side code-index writes (sync worker projection marks, projection cleanup retries, summary and label updates) and a pg_search merge or vacuum. The summary updates are one short transaction per symbol (`maintenance.py:498-529`), which makes them unlikely.

**Question for the Orchestrator:** did the hanging repros print nothing at all, including stderr? A silent hang confirms the advisory step succeeded. A `WARN … index refresh already running` line would instead point at advisory contention, and gcode serves the existing index after that warning rather than hanging.

## Next occurrence: capture this first

The operator, or a seat with hub SQL access, runs this during the hang:

```sql
SELECT pid, application_name, state, wait_event_type, wait_event,
       now() - xact_start AS xact_age, pg_blocking_pids(pid) AS blocked_by,
       left(query, 200) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND (wait_event_type = 'Lock' OR state LIKE 'idle in transaction%')
ORDER BY xact_start;
```

- **Reading the result:** gcode backends show as `gobby-cli` or `gobby-agent-…` (`crates/gcore/src/postgres.rs:19-20`). A holder of the gcode index lock carries the ` gcode-index-lock@<epoch>` suffix.
- **Recovery:** `pg_cancel_backend(<blocker pid>)`, escalating to `pg_terminate_backend` if needed. Do not kill gcode clients; that only adds parked backends.

## Fix

This is a crate change. The owning lane implements it under a task. This seat does not.

1. **Bound the write wait.** Freshness-initiated indexing sets two timeouts on its read/write connection:
   - **`lock_timeout`, about 5 s.** It bounds the heavyweight-lock cases: the checkout row (`FOR SHARE` against `FOR UPDATE`), the project row, and relation locks.
   - **`statement_timeout`, well above a normal refresh.** A normal refresh takes 5.7 s (`index_duration_ms` 5673), so about 60 s fits. This is the backstop for waits that `lock_timeout` never fires on, such as a pg_search extension-internal wait, which shows as LWLock or IO.

   Two ways to do it:
   - an `IndexOptions` flag that `index_files` applies after `connect_readwrite`;
   - a `refresh_files`-specific connect that runs the `SET`s.

   Explicit `gcode index` and maintenance runs keep their current behaviour.
2. **Degrading needs no new code.** `ensure_fresh` already turns any `refresh_files` error into `FreshnessStatus::Degraded`:
   - project scope, through the `map_err(degraded_detail)` closure (`freshness.rs:71-73`);
   - file scope, through `Err(error) => Degraded` (`freshness.rs:88-91`).

   So a `55P03` (lock timeout) or `57014` (statement timeout) error already prints "index refresh failed (…); serving existing index" (`crates/gcode/src/dispatch.rs:110-122`).
3. **Tests.** In an isolated fixture:
   - Hold `SELECT … FOR UPDATE` on the project's `project_checkouts` row from a second connection. Assert that a non-stale `gcode outline` returns within the lock timeout plus a margin, with the degraded warning and the indexed outline.
   - Run a second case with a lowered `statement_timeout`, to cover the statement bound.
4. **Patch-version bump** for `gcode` with `Cargo.lock`, per AGENTS.md release rules.
