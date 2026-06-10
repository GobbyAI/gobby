# Review: storage core (hub/postgres/migrations)

- **Scope:** `src/gobby/storage/hub/` (postgres.py, protocol.py, _ambient.py, runtime.py), `src/gobby/storage/migrations.py` + `src/gobby/storage/migrations/*.sql` (16 files), `src/gobby/storage/postgres_baseline_schema.sql`, `src/gobby/storage/executor.py`, `src/gobby/storage/sql_dialect.py`, `src/gobby/storage/secrets.py`, `src/gobby/storage/config_store.py`, `src/gobby/storage/__init__.py`, plus the call-site seams where the rest of `src/gobby` consumes this boundary and `tests/storage/` coverage. Split boundary: domain CRUD modules (tasks/, sessions/, memories, etc.) are #15774.
- **Reviewer:** Claude (Fable 5) — 5 parallel subagent reviewers + synthesizer; all Blockers personally re-verified against source by the synthesizer.
- **Commit / branch:** b09503b71 on `0.5.0`
- **Summary:** 4 Blocker · 27 Important · 16 Nit — the transaction machinery is well-built per-thread but its concurrency model is split across two domains (ContextVar vs threading.local) and its strongest guarantee (`transaction_immediate` + LockTarget) is optional, which several call sites silently depend on not being optional. The migration system's dual-maintained baseline has already shipped three schema drifts.

## Findings

### [BLOCKER] `transaction_immediate(lock=None)` is not a mutex — integration workspace merge lease admits two concurrent holders
- **Where:** `src/gobby/dispatch/workspace_merge.py:612-635` (`_acquire_integration_mutex`); root cause `src/gobby/storage/hub/protocol.py:230-235` + `src/gobby/storage/hub/postgres.py:123-125` (`lock: LockTarget | None = None`), lock acquisition only when `initial_lock is not None` at `postgres.py:147-167`
- **Failure mode:** `transaction_immediate()` with no `LockTarget` opens a plain READ COMMITTED transaction that acquires nothing. The lease check-then-upsert (SELECT lease at :616-619, upsert at :622-634) can be entered by two concurrent callers; both see the lease absent/expired, both upsert, both return `True` — two simultaneous holders of the integration-workspace merge mutex. On the retired SQLite backend `BEGIN IMMEDIATE` serialized this globally; the Postgres port serializes only when a lock is passed, and this call site passes none. (Synthesizer-verified.)
- **Why it matters:** The function exists solely to provide mutual exclusion for merges into the integration workspace; two holders can corrupt the integration branch/worktree.
- **Minimal fix:** Pass a `LockTarget` (e.g. an `IntegrationWorkspaceMutex(integration_key)`); systemically, make `lock` required on `transaction_immediate` so the remaining lock-less call sites fail type-check (the Postgres-hub migration plan §3.8.2 required exactly this and it was not enforced).
- **Confidence:** high

### [BLOCKER] Session-variable merge/pop are lock-less read-modify-writes racing locked writers of the same row — silent lost updates
- **Where:** `src/gobby/sessions/compact_continuation.py:335-359` (`_merge_session_variable`) and `:428` (`_pop_session_variable`) — both `db.transaction_immediate()` with no lock; the same `session_variables` row is written under `SessionVariableMutation(session_id)` at `src/gobby/workflows/state_manager.py:222` (also :260, :304, :344)
- **Failure mode:** Both functions SELECT `session_variables.variables`, mutate the JSON dict in Python, and UPDATE/INSERT. Advisory locks only serialize participants that take them, so these two paths race both the locked `WorkflowStateManager` writers and each other: concurrent writes to the same session last-writer-win and one variable update silently vanishes. The INSERT branch can also raise an unhandled unique violation on a concurrent first write. The protocol-level lock for exactly this row exists (`SessionVariableMutation`, `protocol.py:133-138`, "Serializes read-modify-write updates to one session variable row") and is simply not used here. (Synthesizer-verified both sides.)
- **Why it matters:** Silent loss of session/workflow state under normal concurrent hook traffic — workflow gates and continuation state depend on these variables.
- **Minimal fix:** Pass `SessionVariableMutation(session_id=session_id)` to both `transaction_immediate()` calls.
- **Confidence:** high

### [BLOCKER] Stale ambient transaction leaks into `loop.create_task` dispatcher ticks — statements can run on a pool-returned connection
- **Where:** `src/gobby/build/dispatch_tick.py:61-68` (`loop.create_task(_run_scheduled_dispatcher_tick(db=db, ...))` fallback), reached from inside an open ambient transaction via `src/gobby/storage/tasks/_manager.py:541-565` (`close_task_with_commit` holds `transaction_immediate(TaskLifecycleMutation)` around `_close_task`) → `src/gobby/storage/tasks/_lifecycle.py:74` (`wake_dispatcher_for_task_change` called inside the transaction scope) → `src/gobby/storage/tasks/_dispatcher_wake.py:31`; ambient lookup with no liveness check at `src/gobby/storage/hub/_ambient.py:56-61`
- **Failure mode:** `loop.create_task` snapshots the current contextvars context, including the `_AMBIENT` entry `{id(db): txn}`. `_AMBIENT.reset(token)` (`_ambient.py:53`) resets only the creating context, not the snapshot. When the tick coroutine later runs — after the close transaction committed and its connection returned to the pool — every `db.execute()`/`db.transaction()` in the tick resolves the dead `_PostgresTransaction` and executes on `txn._conn`: a connection now idle in the pool or checked out by another borrower. Statements either run in an orphan transaction silently rolled back by pool reset, or interleave into an unrelated borrower's transaction. Reachability requires (a) the close path running on the event-loop thread (true for async MCP/HTTP handlers calling the sync manager) and (b) the `automation_loop.schedule_project_dispatch` fast path at `dispatch_tick.py:45-54` being unavailable so the `create_task` fallback fires. (Synthesizer-verified each link: wake inside the with-block, the create_task fallback, and the lookup without liveness check.)
- **Why it matters:** Cross-transaction statement injection / silent rollback of committed-looking work — data corruption.
- **Minimal fix:** Mark `_PostgresTransaction` closed on `_transaction_context` exit and have `ambient_transaction`/`enter_transaction` skip closed entries; additionally run the tick task in a fresh `contextvars.Context()`. Belt-and-braces: move `wake_dispatcher_for_task_change` outside the transaction scope (it is a post-commit side effect — see the after-commit findings below).
- **Confidence:** med — mechanism verified line-by-line; an end-to-end repro test (close an automated task on the loop thread with no automation loop registered) would confirm.

### [BLOCKER] Secret-salt creation is non-atomic — racing creators or a crash window permanently orphan all encrypted secrets
- **Where:** `src/gobby/storage/secrets.py:75-95` (`_get_or_create_salt`): `SALT_FILE.exists()` check, then `os.open(SALT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` — no `O_EXCL`, no temp+rename; read path `SALT_FILE.read_bytes()` with no length validation
- **Failure mode:** Two processes racing past the `exists()` check (e.g. `gobby install`/CLI secrets write and a starting daemon on a fresh `~/.gobby`; 20+ call sites construct `SecretStore(db)` independently) each generate a different `os.urandom(16)` salt and the loser's overwrites the winner's. Any secret encrypted under the first salt becomes permanently undecryptable (`InvalidToken` → `get()` returns `None`). Secondary window: `O_TRUNC` creates an empty file before `os.write`; a crash in between leaves a 0-byte salt the read path accepts forever. There is no rotation/re-encryption tooling in the repo, so recovery is manual re-entry of every secret. (Synthesizer-verified.)
- **Why it matters:** Permanent, silent data loss of stored secrets (API keys, tokens).
- **Minimal fix:** Write to a temp file and `os.rename` into place; open with `os.O_CREAT | os.O_EXCL` and on `FileExistsError` re-read the existing file; validate `len(salt) == 16` on read and raise a specific error otherwise.
- **Confidence:** high on mechanics; med on real-world trigger frequency (first-creation window only).

### [IMPORTANT] Transaction state is split across two concurrency domains — ContextVar ambient registry vs threading.local lock/after-commit stacks
- **Where:** `src/gobby/storage/hub/_ambient.py:12-15` (`_AMBIENT` ContextVar) vs `src/gobby/storage/hub/postgres.py:94` (`self._state = threading.local()`), stacks at `postgres.py:569-638`; concrete production host: `src/gobby/build/lifecycle.py:156-175` (`_build_dry_run` holds `with db.transaction_immediate():` across `await _build_impl(...)`, suspension points in `src/gobby/build/target_branch.py:43-90`)
- **Failure mode:** Asyncio tasks interleaving on one loop thread share threading.local but not ContextVars; threads entered via `asyncio.to_thread` share ContextVars but not threading.local. While `_build_dry_run` is suspended at an await with its transaction open, another task on the same thread that commits its own transaction has its after-commit callbacks merged into the dry-run's scope (`_pop_after_commit_scope` merge at `postgres.py:631-634`); the dry-run then always rolls back (`_DryRunRollback`) and discards them — committed transactions silently lose their post-commit side effects (task change listeners `_manager.py:194`, session bootstrap follow-ups `_bootstrap.py:70`, expansion notify `_apply.py:277`). The shared lock stack symmetrically produces spurious `LockAcquisitionOrderError` on valid acquisitions and `_truncate_lock_stack` in the dry-run's finally (`postgres.py:168-169`) wipes other tasks' still-held entries. In reverse (to_thread), a worker thread joins the ambient transaction but sees empty thread-local stacks, so `after_commit()` fires immediately pre-commit and lock-order enforcement is bypassed. The dry-run also pins a pool connection (max 10) across arbitrarily long awaits.
- **Why it matters:** Lost after-commit side effects for durable work, spurious crashes on valid lock acquisitions, silently bypassed deadlock prevention — all timing-dependent and undiagnosable in production.
- **Minimal fix:** Key the lock/after-commit stacks in the same ContextVar registry as `_AMBIENT` (one ownership model), or per-transaction on `_PostgresTransaction`; separately, stop holding the dry-run transaction across awaits (resolve the target branch before opening it, or run the whole body on one dedicated thread).
- **Confidence:** high on mechanism; med on real-world frequency.

### [IMPORTANT] `_pop_after_commit_scope` merge branch is wrong by construction — stack depth ≥ 2 only ever means two independent DB transactions
- **Where:** `src/gobby/storage/hub/postgres.py:620-638` (`if committed and stack: stack[-1].extend(callbacks)`)
- **Failure mode:** Ambient reuse (`_ambient.py:36-44`) yields the existing transaction without invoking the opener, so a nested `transaction()` on the same adapter never pushes a second scope. The only way the stack reaches depth 2 is two separate `_transaction_context` invocations on one thread — i.e. the cross-task interleavings above, where the inner transaction really committed on its own connection. Its callbacks are nonetheless deferred into the outer (unrelated) scope and discarded if that outer transaction rolls back. The merge models "nested = same DB transaction," which is precisely the case that can never produce depth 2. Only depth-1 behavior is tested (`tests/storage/test_storage_database.py:117-139`).
- **Why it matters:** After-commit callbacks silently dropped or arbitrarily delayed for durably committed work.
- **Minimal fix:** Run the popped scope's callbacks unconditionally on its own commit (delete the merge branch); nesting deferral is already handled by ambient reuse never creating a second scope.
- **Confidence:** high

### [IMPORTANT] `getattr(self.db, "after_commit", None)` never resolves — "after commit" silently means "right now", including mid-transaction
- **Where:** `src/gobby/storage/tasks/_manager.py:190-196` and `src/gobby/storage/sessions/_bootstrap.py:47-73`; concrete in-transaction firing at `src/gobby/mcp_proxy/tools/tasks/_artifacts.py:232-257` (`_notify_listeners()` inside an open `transaction_immediate` block)
- **Failure mode:** `after_commit` exists only on the `Transaction` protocol (`protocol.py:199-206`) and `_PostgresTransaction` (`postgres.py:303-304`) — `HubDatabase`/`PostgresHubDatabase` never define it. The getattr is always `None` and the fallback runs listeners immediately. At `_artifacts.py:257` that happens before COMMIT: listeners re-reading the DB on other connections see pre-change state, and if the transaction subsequently fails, a change notification was emitted for work that never happened. Every other `_notify_listeners()` call works only by the accident of being placed after its with-block.
- **Why it matters:** Stale/phantom change notifications drive websocket broadcasts and dispatcher wakes; the deferral mechanism the code visibly intends does not exist.
- **Minimal fix:** Implement `after_commit` on `PostgresHubDatabase` (register on the ambient transaction if active, else run immediately), add it to the `HubDatabase` protocol, and move the `_artifacts.py:257` call out of the transaction block.
- **Confidence:** high

### [IMPORTANT] `TaskSeqAllocation` falls back to a different lock kind when the project row is invisible — two allocators can hold "the lock" concurrently
- **Where:** `src/gobby/storage/hub/postgres.py:318-330` (`_acquire_lock_target`: `SELECT 1 FROM projects ... FOR UPDATE`; on no-row, advisory lock `task_seq:{project_id}`)
- **Failure mode:** Around project creation, allocator T1 (project INSERT not yet visible) takes the advisory lock while T2 (after commit) takes the row lock. The two lock kinds don't conflict, so both proceed into seq allocation simultaneously → duplicate `seq_num` (the read-then-insert in `src/gobby/storage/tasks/_creation.py:48` relies on this lock for uniqueness).
- **Why it matters:** Duplicate task sequence numbers / unique-violation crashes in the first-tasks-after-project-creation window (init/import flows).
- **Minimal fix:** Always take the advisory lock (uniform regardless of row visibility) and drop the row-lock branch.
- **Confidence:** med — mechanism certain; frequency depends on task creation racing project creation.

### [IMPORTANT] After-commit callback exception escapes `transaction()` after a successful COMMIT; remaining callbacks silently dropped
- **Where:** `src/gobby/storage/hub/postgres.py:166-167` (`for callback in callbacks: callback()` — no per-callback isolation, outside the commit try/except)
- **Failure mode:** If callback N raises, callbacks N+1.. never run (no log), and the exception propagates out of `with db.transaction():` even though the transaction committed. Callers treating the exception as transaction failure retry and double-apply, or report failure for durable work.
- **Why it matters:** Success-reported-as-failure corrupts retry logic above the storage layer.
- **Minimal fix:** Run each callback in try/except with `exc_info=True` logging and continue (matching the listener-isolation pattern at `_bootstrap.py:55-66`).
- **Confidence:** high

### [IMPORTANT] Returned cursors outlive their connection's pool checkout — correctness rests on unpinned psycopg client-side buffering
- **Where:** `src/gobby/storage/hub/postgres.py:171-180` (`execute` returns the cursor after the transaction exits), `:277-283` (`_PostgresTransaction.execute` wraps the live psycopg cursor without materializing), `:336-363` (`_PostgresCursor` delegates lazily)
- **Failure mode:** The non-ambient `execute()` path commits and returns the connection to the pool before the caller fetches. Verified safe today: psycopg default cursors buffer the full result client-side and fetches never touch the connection. But nothing pins that assumption — `ServerCursor`, cursor_factory changes, pipeline mode, or `stream()` would make the returned cursor consult a connection another thread may own. No test asserts post-checkout fetchability.
- **Why it matters:** The single most-used storage API is one configuration change away from undefined behavior, with no guard to catch it.
- **Minimal fix:** Materialize eagerly in the non-ambient path (snapshot `fetchall()`/`rowcount` into `_PostgresCursor` before the transaction exits), or at minimum add a regression test pinning post-checkout fetch behavior.
- **Confidence:** med — current behavior verified safe; the finding is the unpinned load-bearing assumption.

### [IMPORTANT] `apply_pending` runs with no advisory lock — concurrent migrators crash on startup
- **Where:** `src/gobby/storage/migrations.py:64-74` (no lock) and `:110-114` (`INSERT INTO schema_migrations` with no `ON CONFLICT`), vs the baseline path which locks at `src/gobby/storage/hub/postgres.py:236`
- **Failure mode:** Daemon startup (`runner_init/helpers.py:84`) racing any CLI command (~25 CLI modules call `runtime_hub_database` with default `apply_migrations=True`): both read applied versions, both execute migration N, both INSERT version N; `version` is the PK, so the loser dies with a raw `psycopg.errors.UniqueViolation`. The startup retry wrapper retries only `OperationalError`/`PoolTimeout`, so the daemon crashes. The PK prevents double-commit (loser's transaction, including data migrations, rolls back — all 16 current files are fully transactional, no `CREATE INDEX CONCURRENTLY`/`ALTER TYPE ADD VALUE`), so this is a crash, not corruption. No test covers concurrent `apply_pending` or a failing migration.
- **Why it matters:** Startup failure during exactly the post-upgrade window when concurrent migration attempts are most likely.
- **Minimal fix:** Take `pg_advisory_xact_lock` inside each per-migration transaction and re-check the version row under the lock (or `INSERT ... ON CONFLICT (version) DO NOTHING` and skip when 0 rows); add a concurrency test mirroring the baseline race test.
- **Confidence:** high (mechanism), med (frequency)

### [IMPORTANT] Baseline-version strict equality bricks every install stamped at an older baseline — re-arms on every future `BASELINE_VERSION` bump
- **Where:** `src/gobby/storage/hub/postgres.py:484-489` (`_has_baseline_version`: `WHERE version = %s` exact match, synthesizer-verified), `postgres.py:396-427` (`_classify_baseline_state` falls through to `corrupt_partial`), `src/gobby/storage/migrations.py:31` (`BASELINE_VERSION = 261`)
- **Failure mode:** A DB whose `schema_migrations` lacks the exact current `BASELINE_VERSION` row but has application tables classifies `corrupt_partial` and raises `MigrationUnsupportedError` telling the user to dump-and-restore — before `apply_pending` ever runs, so forward migrations that would converge the schema never get the chance. Git history shows six baseline bumps in roughly a month (220→239→258→259→260→261). Today this is latent for released users because v0.4.9 — the first Postgres-hub release — stamped 261; dev-channel installs stamped 258-260 are bricked now. The moment `BASELINE_VERSION` is bumped for a future release, every existing install (schema_migrations max = old baseline + applied increments) classifies as corrupt and refuses to start. The test matrix in `tests/storage/hub/test_postgres_baseline_application.py:42-69` has no case for an older-baseline-stamp state.
- **Why it matters:** Becomes a mass upgrade-bricking event with data-loss-adjacent guidance (dump-and-restore for a healthy DB) on a routine maintenance operation. Graded Important only because the currently shipped population stamps 261; treat as release-blocking before any baseline bump.
- **Minimal fix:** Treat any recorded version in the known baseline lineage as already-baselined (e.g. `MAX(version) >= <lowest supported baseline>` or a `_PRIOR_BASELINE_VERSIONS` frozenset) so `apply_pending` rolls the install forward; add the missing classification test case; require every future bump to ship a same-numbered migration.
- **Confidence:** high

### [IMPORTANT] `context_usage_ratio` column type drift: fresh installs get `DOUBLE PRECISION`, upgraded installs get `NUMERIC(5,4)`
- **Where:** `src/gobby/storage/migrations/267_context_usage_snapshot.sql:5` (`NUMERIC(5, 4)`) vs `src/gobby/storage/postgres_baseline_schema.sql:179` (`DOUBLE PRECISION`)
- **Failure mode:** The baseline column was changed to `DOUBLE PRECISION` with no migration altering the type for DBs that ran 267 (v0.4.9 upgraders run 267 and get `NUMERIC`). Populations permanently diverge: NUMERIC rounds to 4 decimals and psycopg returns `decimal.Decimal` vs `float`, so code reading `sessions.context_usage_ratio` sees different Python types depending on install history (`json.dumps` on a raw row works on one population and raises `TypeError` on the other).
- **Why it matters:** Same schema_migrations contents, different schema — type-dependent bugs reproduce on only one install population.
- **Minimal fix:** New migration: `ALTER TABLE sessions ALTER COLUMN context_usage_ratio TYPE DOUBLE PRECISION;`
- **Confidence:** high

### [IMPORTANT] `idx_sessions_context_usage_ratio` definition drift: partial DESC index vs full ASC index under the same name
- **Where:** `src/gobby/storage/migrations/267_context_usage_snapshot.sql:15-16` (`(context_usage_ratio DESC) WHERE context_usage_ratio IS NOT NULL`) vs `src/gobby/storage/postgres_baseline_schema.sql:260` (full, ASC, no predicate)
- **Failure mode:** Fresh installs run the baseline first so 267's `CREATE INDEX IF NOT EXISTS` no-ops (full ASC index); v0.4.9 upgraders get the partial DESC index. Divergent query-plan eligibility and maintenance characteristics across populations. The contract test asserts only that the index *name* appears in both files (`tests/storage/test_migration_contract.py:283-284`) — exactly how this slipped through.
- **Why it matters:** Same-name different-definition indexes across install populations; demonstrates the name-only contract test cannot catch definition drift.
- **Minimal fix:** Migration that drops and recreates the canonical definition; strengthen the contract test to compare full statement text.
- **Confidence:** high

### [IMPORTANT] `tasks.assignee` removed from baseline with no drop migration — upgraded DBs permanently diverge
- **Where:** `src/gobby/storage/postgres_baseline_schema.sql` tasks table (no `assignee`) vs v0.4.9 which shipped it; no file in `src/gobby/storage/migrations/` references `assignee`
- **Failure mode:** v0.4.9 DBs keep a dead `assignee TEXT` column forever; fresh installs lack it. No code references it (verified) — but it is the third instance of editing the baseline without a paired migration, silently violating the upgrade contract.
- **Why it matters:** Schema state recorded as identical isn't identical; the contract erosion pattern is the real risk.
- **Minimal fix:** Migration: `ALTER TABLE tasks DROP COLUMN IF EXISTS assignee;`
- **Confidence:** high

### [IMPORTANT] gcore/gwiki adoption trusts external schema shape with no verification, and the table whitelist bricks onboarding if gcode evolves
- **Where:** `src/gobby/storage/hub/postgres.py:57-66` (`_GCORE_CODE_INDEX_TABLES` hardcoded 6-name set), `:412-420` (the `all(...)` whitelist), `:430-447` (skip logic skips `CREATE TABLE` and `CREATE INDEX` for adopted tables)
- **Failure mode:** (a) A gcode-first DB is adopted only if *every* non-infra table is one of exactly 6 names or `gwiki_*`; gcode is an independently-updated external binary (this repo contains no gcode DDL), so any new gcode table makes onboarding classify `corrupt_partial` → dump-and-restore error. (b) In the adoption states, the pre-existing tables' column shape and index set (including the bm25 search indexes, skip asserted at `tests/storage/hub/test_postgres_baseline_application.py:300`) are never compared to the baseline; divergence is stamped as a successful baseline and surfaces later as runtime query failures with no breadcrumb.
- **Why it matters:** Wrong schema silently recorded as success; cross-repo contract enforced only by a name list on one side.
- **Minimal fix:** After adoption, probe `information_schema` for expected columns/indexes and create missing secondary/bm25 indexes with `IF NOT EXISTS` instead of skipping; pin the gcode↔gobby table contract in a shared fixture or version handshake.
- **Confidence:** med — mechanism certain; actual gcode divergence unverifiable from this repo.

### [IMPORTANT] `corrupt_partial` error message is factually wrong for the bookkeeping-present case
- **Where:** `src/gobby/storage/hub/postgres.py:240-244`
- **Failure mode:** The message says "has application tables but no schema_migrations," but `corrupt_partial` is also returned when `schema_migrations` exists (older-baseline installs and genuinely corrupt bookkeeping). The diagnosis sends users toward dump-and-restore on a false premise.
- **Why it matters:** Misleading guidance on a destructive-action path; compounds the baseline-bump finding.
- **Minimal fix:** Branch the message on `has_bookkeeping` and include the observed max version and table state in the exception.
- **Confidence:** high

### [IMPORTANT] Migration version numbers were reused after deletion — installs that applied the deleted file silently skip the replacement
- **Where:** `src/gobby/storage/migrations/` git history: `261_auth_session_token_hashes.postgres.sql` (added/deleted May 21) vs `261_implementation_domain.sql` (May 22); `274_nullable_task_merge_status.sql` (Jun 1, deleted) vs `274_memory_dream.sql` (Jun 5)
- **Failure mode:** `apply_pending` keys solely on version number. An install that applied the deleted 274 skips `274_memory_dream` → `memory_dream_runs`/`memory_dream_snapshots` never created → migration 276 (`UPDATE memory_dream_snapshots ...`) crashes startup with "relation does not exist". The 261 case yields a missing `implementation_domain` column stamped as baselined.
- **Why it matters:** Wrong schema recorded as success / startup crash; the process hazard recurs every time a number is recycled. Exposure windows were short, so affected installs are likely few.
- **Minimal fix:** Never reuse a deleted version number; add a test asserting versions are monotonically appended against a checked-in high-water mark.
- **Confidence:** high (mechanism), low (live affected installs)

### [IMPORTANT] `DatabaseExecutor.run()` drops ContextVars — ambient transactions are invisible on executor threads, and the `to_thread` fallback behaves differently
- **Where:** `src/gobby/storage/executor.py:59-68` (`loop.run_in_executor` — does not copy context); `src/gobby/app_context.py:126-130` (`run_db` falls back to `asyncio.to_thread`, which *does* copy context)
- **Failure mode:** DB work bridged through `DatabaseExecutor.run` that calls `db.execute()` sees no ambient transaction and opens its own per-statement transaction — silently escaping any transaction the awaiting caller holds. The build dry-run pattern (`build/lifecycle.py:154-175`) survives today only because build code happens to use `asyncio.to_thread`; routing it through `run_db` — the daemon's blessed bridge — would silently commit writes from a "dry run." Production (executor) and test (to_thread) semantics diverge for the same `run_db` API. No contextvars coverage in `tests/storage/test_database_executor.py`.
- **Why it matters:** Silent transaction-escape is a data-integrity trap; tests can pass while production breaks.
- **Minimal fix:** Capture `contextvars.copy_context()` in `run()` and submit `ctx.run(...)` (matching to_thread), or detect a non-None ambient transaction and raise; add a regression test.
- **Confidence:** high on mechanics; med on a currently-broken call site (none found — latent trap plus prod/test divergence).

### [IMPORTANT] Every fresh `SecretStore` pays 600k PBKDF2 iterations; key cache is per-instance only
- **Where:** `src/gobby/storage/secrets.py:98-107` (`_derive_fernet_key`, `iterations=600_000`), `:130-139` (`_get_fernet` caches on `self`)
- **Failure mode:** The key derives from process-stable inputs `(machine_id, salt)`, yet 20+ call sites construct a fresh `SecretStore(db)` per operation (e.g. `servers/routes/auth.py:51`, `mcp_proxy/client_manager/secrets.py:43`, `integrations/linear_graphql.py:47`), burning ~0.2–0.5 s of CPU per first use — on async routes, on the event loop thread.
- **Why it matters:** Hidden per-request latency that stalls the loop; the construct-per-call pattern was flagged in the routes review (#15769) — this is the storage-side fix.
- **Minimal fix:** Module-level memoization keyed on `(machine_id, salt)`; document the derivation cost in the class docstring.
- **Confidence:** high

### [IMPORTANT] `SecretStore.set()` is a 3-statement check-then-act with no transaction — concurrent first-writes raise UniqueViolation
- **Where:** `src/gobby/storage/secrets.py:160-196` (SELECT → INSERT/UPDATE → SELECT); `secrets.name` is UNIQUE (`postgres_baseline_schema.sql:1140`)
- **Failure mode:** Without an ambient transaction each statement autocommits. Two concurrent `set()` calls for the same new name both pass the SELECT and both INSERT; the loser gets an unhandled `UniqueViolation` (a 500 from routes). The post-upsert SELECT is racy against concurrent delete (`ValueError "not found after upsert"`).
- **Why it matters:** Concurrency hazard plus transaction-boundary contract drift; `ConfigStore.set` already shows the correct `ON CONFLICT` pattern.
- **Minimal fix:** Single `INSERT ... ON CONFLICT (name) DO UPDATE ... RETURNING *`.
- **Confidence:** high

### [IMPORTANT] Decrypt failure is conflated with "not found" and `resolve()` forwards literal `$secret:` refs downstream
- **Where:** `src/gobby/storage/secrets.py:198-221` (`get` returns `None` for both missing row and `InvalidToken`), `:279-290` (`resolve` leaves `match.group(0)` in output, logs "reference not found")
- **Failure mode:** On key-material problems (machine-id loss, salt overwrite — see the salt Blocker) `get()` is indistinguishable from a missing secret; `resolve()` then logs a misleading warning and the literal `$secret:name` string is sent to external services as e.g. an Authorization header.
- **Why it matters:** Operators chase "not found" while the real problem is key material; the unresolved ref leaves the machine.
- **Minimal fix:** Raise a specific `SecretDecryptionError` on `InvalidToken`; log the two cases differently in `resolve()` and substitute empty rather than forwarding the raw ref.
- **Confidence:** high

### [IMPORTANT] `ConfigStore.set()/set_many()` never write `is_secret`, forcing raw-SQL patching in routes
- **Where:** `src/gobby/storage/config_store.py:125-160` (upserts omit `is_secret`); workaround at `src/gobby/servers/routes/configuration_secrets.py:60-68` (`mark_secret_keys` raw `UPDATE config_store SET is_secret = TRUE`)
- **Failure mode:** `set()` explicitly allows canonical `$secret:` references but writes them with `is_secret = FALSE`; all five `get_secret_keys()` consumers then misclassify the key unless a route remembers to call `mark_secret_keys` afterwards — an invariant maintained outside the store, by convention, in raw SQL against the store's own table.
- **Why it matters:** One forgotten call silently breaks secret masking/partitioning in configuration UI/export paths.
- **Minimal fix:** Compute `is_secret` in `set()`/`set_many()` and include it in the upsert; fold `mark_secret_keys` into a ConfigStore method.
- **Confidence:** high on mechanics; med on a currently-broken consumer (all present callers compensate).

### [IMPORTANT] `set_many()` is documented as bulk upsert but is not atomic
- **Where:** `src/gobby/storage/config_store.py:141-160`; bare callers at `src/gobby/servers/routes/configuration_ui_settings.py:62` and `src/gobby/cli/installers/embedding.py:371`
- **Failure mode:** Each entry autocommits separately; a mid-loop DB failure persists a prefix of the batch.
- **Why it matters:** Transaction-boundary contract; sibling methods (`set_secret`, `clear_secret`) open their own transactions.
- **Minimal fix:** Wrap the loop in `with self.db.transaction():` (ambient reuse keeps wrapped callers safe).
- **Confidence:** high

### [IMPORTANT] SecretStore accepts names that `resolve()` can never match
- **Where:** `src/gobby/storage/secrets.py:29` (`SECRET_REF_PATTERN` allows `[A-Za-z_][A-Za-z0-9_]*`) vs `:126-128` (`_normalize_name` only strips/lowercases; `set()` has no charset validation, empty name allowed)
- **Failure mode:** `gobby secrets set my-key ...` stores `my-key`, but `$secret:my-key` partially matches as `$secret:my` → lookup fails → consumer receives the garbled literal. Dots/dashes/leading digits are silently unreferenceable.
- **Why it matters:** Silent edge-case failure with a confusing partial-match symptom; write-time validation is cheap.
- **Minimal fix:** Validate normalized names against the same grammar as `SECRET_REF_PATTERN` in `set()`.
- **Confidence:** high

### [IMPORTANT] `db_executor.shutdown(wait=True)` blocks the event loop unboundedly during daemon shutdown
- **Where:** `src/gobby/runner_lifecycle_shutdown.py:539-544` calling `src/gobby/storage/executor.py:103-109` from inside `async def shutdown_daemon_services`
- **Failure mode:** `ThreadPoolExecutor.shutdown(wait=True)` joins worker threads synchronously on the loop thread. A hung DB call (network partition, stuck lock) blocks shutdown forever; because the loop is blocked, the remaining cleanup (including `database.close()` at :547) never runs.
- **Why it matters:** A daemon that can't finish shutting down needs SIGKILL and skips later cleanup.
- **Minimal fix:** `await asyncio.wait_for(asyncio.to_thread(db_executor.shutdown, wait=True), timeout=...)` falling back to `shutdown(wait=False, cancel_futures=True)`.
- **Confidence:** med (shutdown-only path; requires a stuck DB call — exactly what shutdown should survive)

### [IMPORTANT] `bulk_move_sessions` swallows per-row failures inside one transaction — reports "moved" for sessions whose updates rolled back
- **Where:** `src/gobby/servers/routes/sessions/lifecycle.py:110-133`
- **Failure mode:** Per-row `except Exception` (:122-123) inside a single transaction swallows statement errors; on Postgres one failed statement aborts the transaction, every subsequent execute raises `InFailedSqlTransaction` (also swallowed), and COMMIT of an aborted transaction becomes ROLLBACK without raising. The route returns `status: success` with `moved: N` and broadcasts `session_updated` for sessions that never changed.
- **Why it matters:** False success plus stale UI broadcasts; violates both atomicity and no-silent-swallow contracts.
- **Minimal fix:** Fail the batch atomically (try/except outside the transaction) or give each session its own transaction; compute `moved_ids` from durable outcomes only.
- **Confidence:** high (med on exact psycopg aborted-commit behavior; the mixed accounting is wrong either way)

### [IMPORTANT] MCP tool cache rebuild is non-atomic: DELETE-all then per-row INSERTs in separate one-shot transactions
- **Where:** `src/gobby/storage/mcp.py:599-645` (`cache_tools`: DELETE at :619, INSERT loop at :629); same per-row pattern in `refresh_tools_incremental` at `:730/:755/:785`; bare callers at `src/gobby/mcp_proxy/client_manager/tool_inventory.py:151` and `mcp.py:929`
- **Failure mode:** Between the DELETE and the last INSERT, concurrent readers (`get_cached_tools` feeding progressive discovery `list_tools`) see an empty or partial tool list; a mid-loop failure leaves the cache truncated until the next refresh. The sibling `_replace_tools_for_server_id` (`mcp.py:234-256`) does the same dance but its caller wraps it in a transaction (`mcp.py:487-504`) — proving the intended pattern.
- **Why it matters:** Progressive discovery is the primary tool-routing surface; transient empties cause spurious "tool not found" failures for agents.
- **Minimal fix:** Wrap the delete+insert bodies in `with self.db.transaction():`.
- **Confidence:** high

### [IMPORTANT] Metrics archival double-counts on partial failure: archive UPSERT and source DELETE are separate transactions
- **Where:** `src/gobby/mcp_proxy/metrics_events.py:382-434` (INSERT...ON CONFLICT DO UPDATE at :392, DELETE at :426, both bare `db.execute`)
- **Failure mode:** A crash between the two leaves old events both aggregated into `metrics_events_archive` and still present in `metrics_events`; the next run re-aggregates and the conflict arm adds them again (`call_count = archive.call_count + excluded.call_count`). No idempotency key — counts inflate permanently.
- **Why it matters:** Non-self-healing data corruption in metrics.
- **Minimal fix:** Wrap both statements in one transaction.
- **Confidence:** high

### [IMPORTANT] Lifecycle repair force-reseed can strand a task with no stage manifest
- **Where:** `src/gobby/tasks/lifecycle_repair.py:310-340` (`_apply_candidate`: bare DELETE of `task_stage_states`, then `initialize_manifest(...)` as a separate transaction)
- **Failure mode:** A failure between the DELETE and the re-seed leaves zero manifest rows; the dispatcher resolves "current stage" as the first non-done row, so the task silently drops out of automation until repair is re-run.
- **Why it matters:** A repair tool that can leave the patient worse off; mechanical fix via ambient join.
- **Minimal fix:** Wrap DELETE + `initialize_manifest` in one `with db.transaction():`.
- **Confidence:** med (non-atomicity verified; impact depends on `initialize_manifest` failure modes, which include validation raises)

### [IMPORTANT] Missing tests for the three most load-bearing storage-core concurrency behaviors
- **Where:** `tests/storage/test_database_executor.py` (only worker-cap and reject-after-shutdown), `tests/storage/test_storage_database.py:73-139` (single-scope only), `tests/storage/hub/test_ambient.py:36` (fake-based)
- **Failure mode:** Untested: (1) executor/ambient non-propagation — nothing asserts `DatabaseExecutor.run` work does NOT join an open ambient transaction or can't deadlock on a held row lock; (2) same-thread asyncio interleaving against the thread-keyed stacks — the exact invariant `_build_dry_run` violates; (3) `_after_commit`'s no-scope fallback firing callbacks immediately (before commit) when a Transaction object is used from another thread. The fixture story is otherwise solid (real Postgres, schema-per-worker, MVCC/lock tests).
- **Why it matters:** These behaviors fail only under concurrency — exactly where missing tests let regressions ship.
- **Minimal fix:** Add the three tests described; each is a two-coroutine or executor-bridge scenario against the real fixture.
- **Confidence:** high

### [NIT] Dead defensive branches silently change semantics for test doubles
- **Where:** `src/gobby/storage/hub/postgres.py:104-106` (`getattr(pool, "open")` callable check), `:109` (`getattr(self, "_pool_opened", False)` — set unconditionally in `__init__`), `:285-296` (`executemany` driver branch — psycopg3 `Connection` has no `executemany`; only fakes hit it and get `rowcount=-1` instead of the real count), `:346-353` (`fetchall` filters `None` rows `_normalize_row` can't produce)
- **Note:** Production never takes these branches; test doubles that do get different semantics, masking bugs the real adapter would surface. Delete them.

### [NIT] `close()` implies reopenability psycopg_pool doesn't support; env-var pool config crashes opaquely on malformed values
- **Where:** `src/gobby/storage/hub/postgres.py:260-262` (`_pool_opened = False` after `pool.close()`), `:86-97` (`int(os.getenv("PGPOOL_MIN", "2"))` etc.)
- **Note:** psycopg_pool cannot re-open a closed pool, so `close()` → `transaction()` raises a confusing pool error; `PGPOOL_MIN=abc` raises a bare ValueError naming no variable, and `PGCONNECT_TIMEOUT=2.5` rejects a valid float.

### [NIT] `_normalize_value` re-serializes jsonb with `sort_keys=True` and passes UUID/Decimal through as driver objects
- **Where:** `src/gobby/storage/hub/postgres.py:374-381`
- **Note:** Byte-wise stored-vs-reread JSON comparisons (e.g. definition-drift hashing) can see false drift; UUID/Decimal diverge from the SQLite-era all-TEXT row contract. Confidence low on real-world impact — no concrete broken caller identified.

### [NIT] `261_implementation_domain.sql` is unreachable dead code
- **Where:** `src/gobby/storage/migrations/261_implementation_domain.sql` vs baseline stamping `BASELINE_VERSION = 261` (`postgres.py:255-258`)
- **Note:** Every DB that passes classification already has 261 recorded, so the file is skipped everywhere; its contract test asserts behavior that never executes. Fold into the baseline and delete.

### [NIT] Statement splitter mishandles `E'...'` backslash-escaped strings (latent)
- **Where:** `src/gobby/storage/migrations.py:185-195` (`_skip_single_quoted_string` treats `\'` + terminator as a doubled quote)
- **Note:** No current migration or baseline file contains `E'` strings (verified); failure would be a loud syntax error in a rolled-back transaction. Nested block comments are handled correctly; `$1` params and `$`-in-identifier are correctly rejected as tags. Add escape-string support or a guard, plus a splitter test.

### [NIT] `_discover_migrations` silently skips files that don't match the filename regex
- **Where:** `src/gobby/storage/migrations.py:94-96` (no log on regex miss; regex at :34)
- **Note:** A misnamed file ending in lowercase `.sql` is caught by the contract test's pinned list; `.SQL`/`.sql.txt` evade both the test filter and the runner with zero diagnostics. Warn or raise for unexpected files.

### [NIT] Routine migration application logs at WARNING
- **Where:** `src/gobby/storage/migrations.py:70` (level pinned by `tests/storage/test_migration_runner.py:56`)
- **Note:** Fresh installs warn 15 times for expected behavior. Use INFO; update the test.

### [NIT] Migration 269 adds a CHECK constraint without cleaning violating rows first
- **Where:** `src/gobby/storage/migrations/269_context_usage_ratio_range.sql:3-18`
- **Note:** 268/270/276 all normalize before constraining; 269 doesn't — a row with ratio > 1 (possible only in dev DBs created between the 267 and 269 builds) aborts the upgrade. Prepend a clamping UPDATE.

### [NIT] Migration 270's cleanup UPDATE has no WHERE clause — rewrites the entire sessions table
- **Where:** `src/gobby/storage/migrations/270_context_usage_value_constraints.sql:3-41`
- **Note:** Touches every row (heap + ~17 index rewrites) inside the migration transaction for almost-always-zero violating rows. Add a WHERE mirroring the SET branches.

### [NIT] Migration 273's constraint drop is a permanent no-op against baseline-created DBs
- **Where:** `src/gobby/storage/migrations/273_task_merge_status.sql` (drops `tasks_merge_in_progress_bool_check`) vs baseline inline unnamed `CHECK(merge_in_progress IN (FALSE, TRUE))` (`postgres_baseline_schema.sql:315-318`)
- **Note:** The baseline's auto-named constraint never matches the dropped name; `IF EXISTS` hides the divergence.

### [NIT] Every CLI invocation re-runs baseline classification and migration discovery
- **Where:** `src/gobby/storage/hub/runtime.py:16-40`; ~25 CLI callers default `apply_migrations=True`
- **Note:** Each command pays pool-open + pg_tables classify + version probe + bookkeeping transaction + dir scan, and widens the unlocked `apply_pending` race window. Fast-path when `schema_migrations` already contains `latest_known_version()`.

### [NIT] `run()` shutdown race leaks the stdlib error and permanently skews stats
- **Where:** `src/gobby/storage/executor.py:59-68`
- **Note:** Between the locked `_shutdown` check and submission, a concurrent `shutdown()` yields the stdlib `RuntimeError` instead of the curated one, and `_submitted` was already incremented with no matching `_completed` — `stats().queued` overcounts forever after.

### [NIT] sql_dialect dead code: `dialect_of` has zero callers; `is_postgres` is constant True yet callers still branch
- **Where:** `src/gobby/storage/sql_dialect.py:16-25`; dead branches at `src/gobby/storage/inter_session_messages.py:245` and `src/gobby/servers/routes/admin/_token_timeseries.py:26-31`; every helper carries an unused `db` parameter
- **Note:** `dialect_of` silently coerces any unexpected adapter to "postgres" — would mask a misconfiguration rather than fail. All fragment-builder call sites pass literals (no injection surface, verified). Delete and simplify.

### [NIT] `ConfigStore.get/get_all` crash unhandled on corrupt JSON rows
- **Where:** `src/gobby/storage/config_store.py:110-123` (`json.loads(row["value"])` unguarded)
- **Note:** One bad row prevents daemon startup via `load_config` (`config/app.py:879`) with a traceback that doesn't name the key. Catch, log the key, skip/raise a named error.

### [NIT] flatten/unflatten asymmetries: empty dicts vanish; conflicting scalar+nested keys crash order-dependently
- **Where:** `src/gobby/storage/config_store.py:248-283`
- **Note:** `flatten_config({"a": {}})` drops the key; `unflatten_config({"a": 1, "a.b": 2})` raises TypeError depending on row order (`get_all` has no ORDER BY). Low reachability — route-layer shape validation compensates.

### [NIT] `ConfigStore.delete/delete_all` orphan rows in the secrets table
- **Where:** `src/gobby/storage/config_store.py:162-170` vs `clear_secret` at `:230-240`
- **Note:** Deleting a secret-flagged key via plain `delete()` leaves the encrypted row behind (still encrypted — bookkeeping drift only). Warn or require `clear_secret`; add a sweep.

## Systemic patterns

1. **SQLite `BEGIN IMMEDIATE` semantics were assumed; Postgres delivers them only on request — and the request is optional.** `transaction_immediate(lock=None)` is legal (`protocol.py:230-235`) and seven runtime call sites use it lock-less (`build/lifecycle.py:156`, `dispatch/dispatcher.py:685`, `dispatch/workspace_merge.py:615`, `github_triage/service.py:436`, `mcp_proxy/tools/tasks/_artifacts.py:232`, `sessions/compact_continuation.py:342,428`) — each a read-then-write serialized on SQLite and racing on Postgres. The hub migration plan required a mandatory, type-checked `LockTarget`; the optional default quietly voided that. Making `lock` required is the one-line systemic fix; two of the seven are Blockers above.
2. **Transaction state ownership is split-brain.** The ambient registry is a ContextVar (per-task); the lock and after-commit stacks are threading.local (per-thread). They agree only while transactions are synchronously scoped on one thread — an invariant nothing asserts, one production path already violates (`_build_dry_run`), and two async→sync bridges with different ContextVar semantics (`DatabaseExecutor.run` vs `asyncio.to_thread`) coexist under the same `run_db` name. One ownership model (context- or transaction-keyed) removes the whole class.
3. **Dual-maintained schema with no equivalence check.** The 2,134-line baseline is hand-edited alongside migration files and has already shipped three drifts (column type, index definition, dropped column without migration); `IF NOT EXISTS` guards swallow divergence and the contract tests compare names/substrings, not definitions. A CI job that builds both paths into scratch DBs and diffs `pg_dump --schema-only` would catch every instance. Related: fresh installs replaying migrations 262–277 over the final baseline is an undocumented invariant that holds only by convention.
4. **Baseline-version bookkeeping is bump-fragile.** Exact-match watermark, no lineage concept, classification runs before forward migrations can repair the stamp — every future `BASELINE_VERSION` bump is an upgrade-bricking event by construction, with a misleading "corrupt" message pointing at dump-and-restore.
5. **Bare `db.execute()` convenience invites non-atomic multi-write sequences.** Because it transparently works inside and outside transactions, related-write groups silently degrade to per-statement commits when no ambient transaction exists (tool cache rebuild, metrics archival, lifecycle repair, `SecretStore.set`, `set_many`). The correct idiom exists in-repo (`mcp.py:487`); nothing enforces it.
6. **Invariants enforced by call-site convention instead of the storage API.** `is_secret` flagging lives in route-side raw SQL, `set_many` atomicity lives in caller transactions, secret-name grammar is enforced only by the reader, and `after_commit` deferral is a `getattr` that never resolves. Each works because every current caller compensates; each breaks silently on the next caller that doesn't.
7. **Silent-degradation `getattr` fallbacks.** `getattr(db, "after_commit")`, `getattr(conn, "executemany")`, `getattr(pool, "open")` — when the attribute is missing the code substitutes weaker semantics instead of failing loudly; two hide real bugs today.
8. **Sync DB on the event loop remains pervasive at the seams** (`bulk_move_sessions`, tool-cache writes from async discovery, PBKDF2 on routes, executor shutdown join) — consistent with the systemic finding in the servers and routes reviews; `run_db`/`DatabaseExecutor` exist but are used by a handful of call sites.

Verified non-issues worth recording for triage: the baseline TOCTOU is correctly handled (waiter re-classifies under the advisory lock; tested); fresh-install replay of 262–277 over the baseline is idempotent today; the dollar-quote splitter faces no hazardous constructs in current files; `safe_update` call sites all pass literal where-clauses (no injection); `lastrowid` has zero real callers; no reachable `transaction_immediate`-inside-`transaction` RuntimeError path was found; exactly one shared `DatabaseExecutor` exists in production; `storage/__init__.py` lazy exports are clean.
