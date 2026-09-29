Plan artifact: `.gobby/plans/gcore-async-postgres.md`

# gcore Async Postgres Layer

**Plan ID:** gcore-async-postgres

## Overview
`kind: framing`

Task #21557 (gcore async Postgres layer), ROADMAP Stage 2 item S2.1, planned
under #23078 (Plan the gcore async Postgres layer (S2.1)). Decision 16 composes
the absorbed daemon from family crates whose row types and repositories run
"over the S2.1 `gcore` async pool and transaction seam". This plan delivers that
seam: a pooled async connection layer in `gobby-core`, its transaction boundary
and row-mapping primitives, runtime-role compatibility with the baseline
capability roles, and the connection budget the native pool shares with the
Python daemon during the strangler period. S2.3 (#21559) is the first consumer
and wires the pool into `gdaemon`'s service container.

## Decision Record
`kind: framing`

1. **Async pool over `tokio-postgres`; `spawn_blocking` around the sync client
   is rejected.** The sync `postgres` 0.19.13 crate is a wrapper: each `Client`
   owns its own current-thread tokio runtime (`postgres-0.19.13/src/connection.rs:14-19`,
   runtime built per connect at `config.rs:460`) and drives `tokio-postgres`
   0.7, already in `Cargo.lock` at 0.7.17 (tokio 1.50 too). Behind
   `spawn_blocking` every query would park a blocking-pool thread while a nested
   runtime runs the socket; the front door's own runtime would then carry two
   runtime layers per connection and an unbounded blocking pool as its only
   back-pressure. Driving `tokio-postgres` directly removes both layers and
   reuses the existing URL, `sslmode`, and OpenSSL code (`postgres-openssl`
   implements `MakeTlsConnect`, `lib.rs:111`). Measured demand does not argue
   for either on throughput: the Python daemon's watchdog recorded a peak of 14
   of 64 pool connections in use (2026-09-24 16:44:41: 1,080,450 checkouts,
   mean hold 21.6 ms; 2026-09-25 12:25:04: 218,483 checkouts, mean hold 5.3 ms),
   and both saturation events were executor-boundary stalls with 1–2 of 32
   workers active. The decision rests on structure, and one confirming test
   (1.2) pins the property that matters: a pool of N under K > N concurrent
   transactions never holds more than N server connections.
2. **Pool library: `deadpool-postgres` 0.14.2 (over `deadpool` 0.13.1) with
   `tokio-postgres` at least 0.7.18.** 0.7.17 carries RUSTSEC-2026-0178 (a
   malformed `DataRow` panics; 0.7.18 returns an error), and the sync
   `postgres` crate shares the same `tokio-postgres` in the lockfile, so the
   floor lifts both paths. It takes a
   `tokio_postgres::Config` plus any `MakeTlsConnect<Socket>` connector
   (`Manager::from_config`), exposes `post_create` and `post_recycle` hooks on
   `PoolBuilder`, and bounds wait, create, and recycle phases through
   `Timeouts` on the tokio runtime. Its `RecyclingMethod::Fast` checks only
   `Client::is_closed`; `RecyclingMethod::Clean` runs `CLOSE ALL; SET SESSION
   AUTHORIZATION DEFAULT; RESET ALL; UNLISTEN *; SELECT
   pg_advisory_unlock_all(); DISCARD TEMP; DISCARD SEQUENCES;`, and `SET
   SESSION AUTHORIZATION DEFAULT` plus `RESET ALL` drop the runtime role, so
   the layer uses `Fast` plus its own verify hook (Decision 4). Hook failure
   semantics (`deadpool` `managed/pool.rs`): a failing `pre_recycle` or
   `post_recycle` hook discards that object and `get()` continues to the next
   idle object or creates a new one; a failing `post_create` hook makes
   `get()` return `PoolError::PostCreateHook`. Rejected: `bb8`
   (the same shape with no Postgres-specific manager) and a hand-rolled
   semaphore pool (reimplements recycle and timeouts for no gain).
3. **Feature `postgres-pool` implies `postgres`.** PD-approved (2026-09-28).
   `ghook` and `gcode` enable `postgres` for their sync paths and gain no async
   pool, tokio runtime features, or `deadpool` dependency. `gdaemon` enables
   `postgres-pool` when S2.3 wires it. Decision 16's "S2.1 `gcore` async pool"
   is this feature.
4. **Runtime-role compatibility is three obligations.** S1.6's tunnel and
   machine-scoped role were dropped by decision 17 (#22068); the live
   dependency is `baseline@420`'s capability roles.
   - Every pooled connection runs as `gobby_daemon_runtime` in UTC:
     `post_create` issues `SET ROLE gobby_daemon_runtime` and `SET TIME ZONE
     'UTC'`; `post_recycle` re-issues `SET TIME ZONE 'UTC'` (`Fast` recycling
     resets no session state, so a borrower's timezone change would otherwise
     leak); both hooks then verify `SELECT current_user,
     current_setting('TimeZone')` in autocommit and fail on either mismatch.
     A recycled connection that fails
     is discarded and never handed out; a new connection that cannot take the
     role (an unapplied database) fails the checkout with a typed
     `RuntimeRoleUnavailable` error. Deadpool recycles on every checkout of an
     idle connection, so this matches the Python hub's per-checkout
     `assert_runtime_role`.
   - Grant issuing stays in the database: `gobby_agent_auth.issue_principal`
     and `issue_tool_principal` are `SECURITY DEFINER` functions the runtime
     role calls (`src/gobby/storage/managed_credentials.py:128`). The pool
     needs no issuer identity; S2.3 ports the callers.
   - `gcode`'s grant login and rehandshake stay on the sync client
     (`crates/gcore/src/postgres.rs::connect_after_grant_rehandshake`).
5. **The native share comes from the single-daemon budget.** One rule in both
   resolvers: `native_pool_max_size = min(8, pool_budget - pool_max_size)`.
   Python's sizing is unchanged: on a standard host (97 usable connections,
   budget 72, Python pool 64) the native share is 8, and Python plus native
   equals the budget. With automatic sizing a host gets a share of at least 2
   only when `pool_budget >= 66`, which on budget multiples of 8 means
   `pool_budget >= 72` (97 usable connections, PostgreSQL's default
   `max_connections=100` less 3 superuser slots). Below that the share is 0
   and building the native pool with a share below 2 fails with the typed
   `PoolError::NativeShareTooSmall { actual, minimum: 2 }`. The builder sees
   only the share, so the operator guidance belongs to S2.3, which holds the
   full resolution: when `pool_budget >= 34` the operator sets
   `database_concurrency.pool_max_size` to at most `pool_budget - 2` or
   raises PostgreSQL `max_connections`; when `pool_budget < 34` only raising
   `max_connections` works, because the Python pool cannot go below
   `MIN_POOL_SIZE = 32`. Such hosts run no native family until the Python
   daemon retires; S2.3 keeps every family on its decision 16 `Proxy` backend
   and logs that guidance when the build fails. The
   measured peak of 14 of 64 shows Python needs no reduction to make room. No
   new config key: the share has one rule.
6. **Transaction seam mirrors the Python hub contract**
   (`src/gobby/storage/hub/postgres_pool.py::transaction_context`).
   - One transaction per logical operation, run as an async closure over
     `&Transaction`; the seam commits on `Ok` and rolls back on `Err`.
   - A connection whose state is uncertain never returns to the pool: after
     `IndeterminateCommit`, a failed rollback, or cancellation while `COMMIT`
     or `ROLLBACK` is in flight, the seam detaches and closes it.
   - After the COMMIT is submitted, an error is a definite server rejection
     when its SQLSTATE class is `23` or `40` or its severity is `ERROR`, and
     its code is none of `57014` (query canceled), `55P03` (lock not
     available), `40003` (statement completion unknown). Every other error
     after submission, including I/O loss and a `FATAL` termination, is
     `IndeterminateCommit`.
   - `after_commit` callbacks return `Result` and run in registration order
     after a confirmed commit and after the checkout is released, as
     `transaction_context` runs them after leaving its connection context; a
     callback error is logged, the next callback still runs, and the
     operation's result never changes.
   - Transaction-scoped advisory locks through lock targets: each target
     yields string keys and a priority; each key is taken with
     `pg_advisory_xact_lock(hashtext($1))` as `_acquire_advisory_lock` does,
     so a native and a Python transaction using the same key string contend on
     the same lock. Nested targets must strictly increase in priority (the
     `_acquire_lock` rule). A target's identity is its priority plus its
     ordered keys: re-acquiring an identical target is a no-op, and a target
     with no keys is rejected.
   - `dedicated_session()` checks a connection out for session-level work
     (session advisory locks, leases) and discards it on release.
   - Parameters bind positionally (`$1`, `$2`, …); identifiers go through a
     validated quoting helper mirroring `validate_identifier`; no SQL is built
     from values.
   - Row mapping is a `FromRow` trait over `tokio_postgres::Row`; family
     repositories are free functions over `&Transaction`.
   - Deadlines and `bounded_transaction` are left to the first family that
     needs them; the seam carries no timeout beyond the pool's acquire bound.
7. **Distinct application name.** Pooled connections carry
   an `application_name` that must start with `gobby-gdaemon`
   (`PoolSettings::application_name`); the sync path keeps `gobby-cli`
   (`postgres.rs::connection_config`) and managed agents keep `gobby-agent-*`.
   The Python hub appends a lifecycle-unique marker
   (`src/gobby/storage/hub/postgres.py:122`); S2.3 chooses the suffix it needs
   when it builds the settings. The `gobby` prefix is load-bearing:
   maintenance-epoch quiescence filters `pg_stat_activity` with
   `application_name LIKE 'gobby%'`, so native backends are counted with the
   Python pools during a maintenance fence.

## As-Is Facts
`kind: framing`

- `crates/gcore/Cargo.toml` feature `postgres` enables `postgres` 0.19
  (`with-uuid-1`), `postgres-openssl` 0.5, `base64`, `scrypt`, `sha2`, `time`.
  Consumers: `gdaemon`, `gcode`, `ghook` (each `features = ["postgres", …]`).
  `deadpool-postgres` and `bb8` are absent from `Cargo.lock`, which pins
  `tokio-postgres` 0.7.17.
- `crates/gcore/src/postgres.rs` (780 lines) owns the sync connect path:
  `connection_config` (`:120`, forces `application_name=gobby-cli`, pinned by
  `connection_config_enforces_gobby_application_name`), `sslmode` normalization
  (`requested_ssl_mode`, `normalize_sslmode_for_parser`, `:328-396`), and TLS
  connector builders (`connect_with_tls_*`, `tls_connector_builder`,
  `:398-485`).
- `gdaemon` has no tokio runtime today; the front-door plan
  (`.gobby/plans/gdaemon-front-door.md`) introduces it. Its per-request
  `api_keys` lookup and lease connection are unimplemented.
- `baseline.sql` header: `baseline@420: canonical pg_dump of baseline@419`;
  migrations head is 454. Roles at `crates/gcore/assets/schema/baseline.sql:6-40`
  are all `NOLOGIN`: `gobby_agent_issuer` (`CREATEROLE`),
  `gobby_gcode_capability`, `gobby_daemon_runtime`. The `$runtime_membership$`
  block grants `gobby_daemon_runtime TO current_user WITH SET TRUE` at apply
  time, so `SET ROLE` fails against an unapplied database.
- Python hub contract: `init_hub_database` (`runner_init/helpers.py`) opens a
  2-connection bootstrap pool with `runtime_role="gobby_daemon_runtime"`;
  `configure_runtime_role` (`postgres_pool.py:511`) sets the role;
  `assert_runtime_role` (`:522`) verifies every checkout in autocommit and
  closes on mismatch; `pool_connection` (`:116`) retries only a
  `PoolTimeout` for ordinary callers, with backoff, and makes one bounded
  attempt for deadline-owned callers; `validate_identifier` (`:558`);
  `advisory_lock_keys` (`:645`). Hub connections set TCP keepalives 1, idle
  30 s, interval 10 s, count 3 (`src/gobby/storage/hub/postgres.py:99-102`).
- Sizing: `crates/gcore/src/database_concurrency.rs::resolve_database_concurrency`
  and `src/gobby/storage/concurrency.py::resolve_database_concurrency` share
  `docs/contracts/database-concurrency-v1.json` (8 cases), checked by
  `database_concurrency.rs::shared_sizing_vectors_conform` and
  `tests/storage/test_database_concurrency.py`. Rules: `pool_budget =
  floor8(usable * 3 / 4)`, auto pool `min(64, budget)`, pool at least 32.
- DB-backed Rust tests follow `crates/gcore/src/schema/runner_tests.rs:134`:
  skip when `GOBBY_SCHEMA_TEST_DATABASE_URL` is unset, require a `*_test`
  database, apply the schema first.

## Constraints
`kind: framing`

- No implementation in #23078. No cargo runs until the PD lifts the load
  breach; leaves run their verification once it is lifted.
- `ghook` and `gcode` keep `features = ["postgres"]` and gain no dependency.
- `postgres.rs` stays under 1,000 lines: the pool lives in a new
  `postgres_pool` module directory; `postgres.rs` changes only helper
  visibility.
- The front-door `auth.rs` `api_keys` lookup and lease connection are S2.3's
  (#21559) to move onto this pool; neither is a Target here.
- The pool is a library: no `gdaemon` wiring, config sourcing, or metrics
  export here. S2.3 reads the resolution and passes the share to the builder.
- No backward compatibility shims; Python behavior does not change.

## P1: Pool And Budget
`kind: framing`

**Goal:** a verified-role async pool exists behind `postgres-pool`, sized by
the shared resolver.

### 1.1 Native pool share in the shared sizing contract [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/database_concurrency.rs::*` — scope-reason: add native_pool_max_size to the resolution and its vector test
- `src/gobby/storage/concurrency.py::*` — scope-reason: add native_pool_max_size to the resolution
- `docs/contracts/database-concurrency-v1.json::*` — scope-reason: add native_pool_max_size to every expected block and one new case
- `tests/storage/test_database_concurrency.py::*` — scope-reason: assert the new expected field

**Research context:** both resolvers compute `pool_budget` and the Python pool
the same way (As-Is Facts). Add `native_pool_max_size: u32`/`int` to
`DatabaseConcurrencyResolution` in both, computed after the pool as
`min(8, pool_budget - pool_max_size)` (Decision 5); add
`NATIVE_POOL_MAX_SIZE = 8` beside the existing constants. No config field and
no validation error in the resolver: a 0 share is a valid resolution. Every
existing case gains `native_pool_max_size` in `expected`: 8 on the 97-usable
auto cases, 0 on the 47-usable case (budget 32, pool 32) and on the explicit
`pool_max_size: 72` case. Add one case where the share is below 8 (an explicit
`pool_max_size: 66` on budget 72 resolves 6). Both test harnesses read every
`expected` key they know, so each gains one assertion line.
`runner_init/storage.py` reads only the existing fields and is unchanged.

**Acceptance:**

- 1.1.1 - Both resolvers report `native_pool_max_size = min(8, pool_budget -
  pool_max_size)` and agree on every vector. test:
  `crates/gcore/src/database_concurrency.rs::shared_sizing_vectors_conform`.
- 1.1.2 - The Python resolver matches the same vectors, including the 0 and 6
  shares. test:
  `tests/storage/test_database_concurrency.py::test_shared_database_concurrency_vectors`.
- 1.1.3 - Python pool, worker, coverage, and reserve values are unchanged for
  every pre-existing case. file: `docs/contracts/database-concurrency-v1.json`.

Verification planned: `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_database_concurrency.py -q`;
`cargo test -p gobby-core database_concurrency`.

### 1.2 Pooled connections with runtime-role verification [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/Cargo.toml::*` — scope-reason: add the postgres-pool feature and its optional dependencies
- `crates/gcore/src/lib.rs::*` — scope-reason: gate the postgres_pool module on the new feature
- `crates/gcore/src/postgres.rs::*` — scope-reason: widen sslmode and TLS helper visibility to pub(crate) for reuse
- `crates/gcore/src/postgres_pool/mod.rs`
- `crates/gcore/src/postgres_pool/config.rs`
- `crates/gcore/src/postgres_pool/tests.rs`
- `Cargo.lock`

**Granularity:** eight acceptance items and seven Targets, one behavior:
the pool is untestable apart from its role hooks, bounds, and error mapping,
and the dependency checks (1.2.6, 1.2.8) and sync-path check (1.2.7) guard
the same feature wiring.

**Research context:** feature `postgres-pool = ["postgres",
"dep:tokio-postgres", "dep:deadpool-postgres", "dep:tokio"]` with
`tokio-postgres = { version = "0.7.18", features = ["with-uuid-1"] }`,
`deadpool-postgres = { version = "0.14.2", features = ["rt_tokio_1"] }`,
`tokio = { version = "1", features = ["rt", "time"] }`; tests use
`#[tokio::test]` through a dev-dependency with `macros` and
`rt-multi-thread`. `config.rs` builds a `tokio_postgres::Config` from the
database URL: parse `normalize_sslmode_for_parser(url)`, set
`application_name` from the validated `PoolSettings::application_name`,
`connect_timeout(5 s)` as `DEFAULT_CONNECT_TIMEOUT` does, keepalives on with
`keepalives_idle(30 s)`, `keepalives_interval(10 s)`, `keepalives_retries(3)`
(the hub values; no config knob), and choose the connector from
`requested_ssl_mode` exactly as `connect_for_mode` maps modes (disable,
prefer/require unverified, verify-ca, verify-full) over
`postgres_openssl::MakeTlsConnector` built from `tls_connector_builder`.
`mod.rs` exposes:

- `PoolSettings { max_size, application_name, wait_timeout, create_timeout,
  recycle_timeout }` and `Pool::build(database_url, settings)`; the builder
  calls `.runtime(Runtime::Tokio1)` and sets all three `Timeouts`; `max_size`
  below 2 returns `PoolError::NativeShareTooSmall { actual, minimum: 2 }` with
  generic display text (Decision 5); an `application_name` without the
  `gobby-gdaemon` prefix returns `PoolError::InvalidApplicationName`.
- `post_create` hook: `SET ROLE gobby_daemon_runtime`, `SET TIME ZONE 'UTC'`,
  then the verify query; `post_recycle` hook: `SET TIME ZONE 'UTC'`, then the
  verify query. Verify is `SELECT current_user,
  current_setting('TimeZone')` through `simple_query` (autocommit); anything
  other than `gobby_daemon_runtime` and `UTC`, or an error, fails the hook.
  `RecyclingMethod::Fast` (Decision 2).
- `Pool::get()` is one bounded deadpool `get()` with no retry loop of its
  own: deadpool already discards a failed recycled object and moves on, and
  the Python retry covers only a pool-wait timeout that native callers have
  no measured need to repeat. Mapping from `deadpool::managed::PoolError`:
  `Timeout(Wait)` to `PoolError::WaitTimeout`; `Timeout(Create)` and
  `Backend` to `PoolError::Unavailable`; `Timeout(Recycle)` to
  `PoolError::Unavailable`; `PostCreateHook` to
  `PoolError::RuntimeRoleUnavailable` (that hook exists only to establish the
  role and session invariants); `Closed` to `PoolError::Closed`;
  `NoRuntimeSpecified` is unreachable because the builder sets the runtime
  and maps to `PoolError::Closed` if it ever occurs. `Unavailable` and
  `RuntimeRoleUnavailable` carry a host, port, and database label built the
  way `postgres.rs::endpoint_label` builds it, and no secret.
- A connection dropped with an open transaction: `tokio_postgres::Transaction`
  queues `ROLLBACK` on drop, and the next recycle's verify query runs after
  it, so the connection returns clean or is discarded.

Tests follow `runner_tests.rs:134` (skip when
`GOBBY_SCHEMA_TEST_DATABASE_URL` is unset, `*_test` database only, schema
applied first).

**Acceptance:**

- 1.2.1 - Every checkout runs as `gobby_daemon_runtime` with `TimeZone=UTC`
  and exactly the configured `application_name` (for example
  `gobby-gdaemon-test-1`); a name without the prefix is refused. test:
  `crates/gcore/src/postgres_pool/tests.rs::checkout_runs_as_runtime_role`.
- 1.2.2 - With `max_size=1`, a borrower that runs `RESET ROLE` hands back a
  connection the next checkout discards (a different `pg_backend_pid()`); a
  borrower that runs `SET TIME ZONE 'America/Chicago'` hands back a
  connection the next checkout reuses (same PID) in `UTC`. test:
  `crates/gcore/src/postgres_pool/tests.rs::recycle_restores_or_discards_session_state`.
- 1.2.3 - With `max_size=1`, a connection dropped mid-transaction is reused by
  the next checkout (same `pg_backend_pid()`), which sees no open transaction
  and none of the dropped writes. test:
  `crates/gcore/src/postgres_pool/tests.rs::dropped_transaction_is_rolled_back`.
- 1.2.4 - A pool of N under K > N concurrent checkouts never exceeds N server
  connections with its application name, and a waiter past `wait_timeout`
  gets `PoolError` without opening a connection. test:
  `crates/gcore/src/postgres_pool/tests.rs::pool_bounds_server_connections`.
- 1.2.5 - A share of 1 returns `NativeShareTooSmall { actual: 1, minimum: 2
  }`; an unreachable endpoint returns `Unavailable` whose display text omits
  the URL password; an unapplied database (no runtime-role membership)
  returns `RuntimeRoleUnavailable`. test:
  `crates/gcore/src/postgres_pool/tests.rs::build_and_connect_failures_are_typed`.
- 1.2.6 - `gobby-hooks` and `gobby-code` build without `deadpool-postgres` in
  their dependency tree. behavior: "no deadpool-postgres entry" in
  `cargo tree -p gobby-hooks` output recorded in the close summary.
- 1.2.7 - The sync path still forces `gobby-cli`. test:
  `crates/gcore/src/postgres.rs::connection_config_enforces_gobby_application_name`.
- 1.2.8 - `Cargo.lock` holds one `tokio-postgres` at 0.7.18 or later and
  `deadpool-postgres` at 0.14.2 or later; the pool config sets keepalives
  1/30 s/10 s/3. test:
  `crates/gcore/src/postgres_pool/config.rs::config_sets_hub_keepalives` and
  behavior: "one tokio-postgres >= 0.7.18" in `cargo tree -p gobby-core
  --features postgres-pool -i tokio-postgres` output recorded in the close
  summary.

Verification planned: `cargo test -p gobby-core --features postgres-pool
postgres_pool` with `GOBBY_SCHEMA_TEST_DATABASE_URL` pointing at the
`gobby_test` hub; `cargo clippy -p gobby-core --features postgres-pool
--all-targets -- -D warnings`; `cargo tree -p gobby-hooks -i
deadpool-postgres` (expects no match); `cargo tree -p gobby-core --features
postgres-pool -i tokio-postgres`.

## P2: Transaction Seam
`kind: framing`

**Goal:** family crates run one transaction per operation with the Python
hub's commit semantics, and session-level work has its own connection.

### 2.1 Transaction boundary, lock targets, and row mapping [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gcore/src/postgres_pool/mod.rs`
- `crates/gcore/src/postgres_pool/transaction.rs`
- `crates/gcore/src/postgres_pool/row.rs`
- `crates/gcore/src/postgres_pool/tests.rs`

**Granularity:** seven acceptance items, one behavior: the transaction
boundary owns its checkout, commit outcome, callbacks, and locks together, and
row mapping and identifier quoting are the seam's small value helpers that
the same tests exercise.

**Research context:** `Pool::transaction(lock: Option<LockTarget>, f)` where
`f: for<'t> AsyncFnOnce(&'t Transaction<'t>) -> Result<T, E>`, with `E:
From<TransactionError>`. The seam checks out, begins, acquires `lock` if
given, runs `f`, commits on `Ok`, rolls back on `Err`, releases the
checkout, and then runs `after_commit` callbacks. A checkout guard owns the
pooled object: it is armed before `COMMIT` or `ROLLBACK` is awaited and
disarmed only when rollback succeeds, commit succeeds, or `COMMIT` returns a
definite server rejection; if the guard drops armed (an `IndeterminateCommit`,
a failed rollback, or the future cancelled mid-command) it detaches the
object with `Object::take` and drops the client, so the connection closes.
After a failed rollback the closure's error is returned and the rollback
error is logged. `Transaction` wraps
`deadpool_postgres::Transaction` and offers `query`, `query_opt`,
`query_one`, `execute` (positional `$n` parameters through `&(dyn ToSql +
Sync)`), `acquire_lock(&dyn LockTarget)`, and `after_commit(Box<dyn FnOnce()
-> anyhow::Result<()> + Send>)`. Commit classification (Decision 6) is a pure
function `is_definite_commit_rejection(code: Option<&SqlState>, severity:
Option<&str>) -> bool`: true when the code is none of `57014`, `55P03`,
`40003` and either the code's class is `23` or `40` or the severity is
`ERROR`; false with no database diagnostic. A `COMMIT` error for which it
returns true is `TransactionError::Server`; every other `COMMIT` error is
`TransactionError::IndeterminateCommit`. Errors before submission are
plain `Server` or `Pool` errors. `LockTarget` is a trait with `fn priority(&self)
-> i32` and `fn keys(&self) -> Vec<String>`, mirroring the Python protocol
(`storage/hub/protocol.py:57`, class-level `PRIORITY`) and
`advisory_lock_keys` (`postgres_pool.py:645`), whose keys are strings such as
`task_lifecycle:{task_id}`. Each key is locked with `SELECT
pg_advisory_xact_lock(hashtext($1))` (`postgres_pool.py:404`); `hashtext` runs
in the server, so a family target that returns the Python key string contends
with the Python daemon. The transaction keeps a stack of held identities,
each the `(priority, keys)` value the target returned: an identity already
held is a no-op; an empty key list returns `TransactionError::EmptyLockTarget`;
otherwise a priority not strictly greater than the top returns
`TransactionError::LockOrder` before any SQL (`_acquire_lock`, `:695-704`),
and the keys are then locked in the order given. Family crates define their own targets; this seam defines none.
`quote_identifier(name) -> Result<String, IdentifierError>` accepts exactly
`^[A-Za-z_][A-Za-z0-9_]*$` (`_SQL_IDENTIFIER_PATTERN`, `:71`, used by
`validate_identifier`, `:558`) and returns it double-quoted.
`row.rs`: `pub trait FromRow: Sized { fn from_row(row: &Row) -> Result<Self,
RowError>; }` with helpers `query_as::<T>` and `query_opt_as::<T>` on
`Transaction`. Rustdoc on the module states the family-repository shape:
free functions taking `&Transaction`, returning `FromRow` types.

**Acceptance:**

- 2.1.1 - `Ok` commits and `Err` rolls back. With `max_size=1`, after a
  confirmed commit callback 1 checks out a connection (proving the checkout
  was released), callback 2 returns `Err`, and callback 3 still runs, in that
  order; a rolled-back transaction and an `IndeterminateCommit` run none.
  test:
  `crates/gcore/src/postgres_pool/tests.rs::transaction_commits_and_runs_callbacks`.
- 2.1.2 - `is_definite_commit_rejection` matches a table covering classes
  `23` and `40`, the exclusions `57014`, `55P03`, `40003`, a generic `ERROR`
  severity, a `FATAL` severity, and no diagnostic. test:
  `crates/gcore/src/postgres_pool/transaction.rs::commit_rejection_table`.
- 2.1.3 - A deferred unique violation at COMMIT (a temp table with an
  `INITIALLY DEFERRED` unique constraint) is `Server` and its connection is
  reused; a transaction whose closure reads `pg_backend_pid()`, has a second
  connection run `pg_terminate_backend` on that pid, and returns `Ok` is
  `IndeterminateCommit` (from `57P01` or a closed connection), and the next
  checkout has a different PID. test:
  `crates/gcore/src/postgres_pool/tests.rs::commit_outcome_is_classified`.
- 2.1.4 - With `max_size=1`, a transaction future dropped while its `COMMIT`
  is in flight (a closure that holds a lock another connection waits on,
  cancelled by `tokio::time::timeout`) leaves a pool whose next checkout has a
  different PID. test:
  `crates/gcore/src/postgres_pool/tests.rs::cancelled_transaction_discards_connection`.
- 2.1.5 - A native transaction holding a target keyed `task_lifecycle:t1`
  blocks a second connection's `pg_advisory_xact_lock(hashtext('task_lifecycle:t1'))`
  until commit; re-acquiring the identical target sends no SQL; a different
  target at equal or lower priority returns `LockOrder` and an empty target
  returns `EmptyLockTarget`, both with no SQL sent. test:
  `crates/gcore/src/postgres_pool/tests.rs::lock_targets_share_python_keys_and_order`.
- 2.1.6 - `quote_identifier` accepts and rejects the same names as
  `validate_identifier`. test:
  `crates/gcore/src/postgres_pool/tests.rs::identifiers_match_python_validation`
  (cases: `tasks`, `_x9`, `9x`, `a-b`, `a b`, `"q"`, empty).
- 2.1.7 - A `FromRow` type round-trips through `query_as`. test:
  `crates/gcore/src/postgres_pool/tests.rs::from_row_maps_rows`.

Verification planned: as 1.2.

### 2.2 Dedicated session connections [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/postgres_pool/mod.rs`
- `crates/gcore/src/postgres_pool/session.rs`
- `crates/gcore/src/postgres_pool/tests.rs`

**Research context:** Python opens advisory-lock connections outside the
transaction pool (`open_advisory_lock_connection`, `postgres_pool.py:238`)
because session-level locks outlive a transaction. It takes the same key
strings as 2.1's lock targets and shares 2.1's `mod.rs` re-exports.
`dedicated_session()`
returns a `DedicatedSession` holding one pooled object with the runtime role
verified, offering `simple_query`, `query`, `try_session_lock(&str)`
(`pg_try_advisory_lock(hashtext($1))`, `postgres_pool.py:590`), and
`session_unlock(&str)` (`pg_advisory_unlock(hashtext($1))`, `:617`). On drop it detaches the object
from the pool (`Object::take`) and closes it, so no session lock or session
state survives into another checkout. The session counts against
`max_size` while held; S2.3's lease holder is the first user.

**Acceptance:**

- 2.2.1 - A session lock taken on a dedicated session (PID recorded) becomes
  acquirable from a control connection within a bounded poll after the
  session drops, and the next pooled checkout has a different PID with the
  pool's size restored. test:
  `crates/gcore/src/postgres_pool/tests.rs::dedicated_session_is_discarded`.
- 2.2.2 - A dedicated session runs as `gobby_daemon_runtime`. test:
  `crates/gcore/src/postgres_pool/tests.rs::dedicated_session_runs_as_runtime_role`.

Verification planned: as 1.2.

## V1: Plan Changelog
`kind: framing`

- 2026-09-28: First draft (e6db320): async pool over `tokio-postgres` with
  `deadpool-postgres`, the `postgres-pool` feature (PD-approved), runtime-role
  verification, the native share from the single-daemon budget, the
  transaction seam, and dedicated sessions. One enhancer pass (run
  8f30e16f); the Program Director accepted ENH-01 through ENH-11 and the
  Writer applied them: dependency floors, no native acquire retry and an
  explicit deadpool error mapping, timezone reset on recycle, a checkout
  guard for uncertain connections, result-returning callbacks after release,
  a pure commit classifier, lock identity, PID-based lifecycle tests, hub
  keepalives, a numeric share error, and this changelog; plus the `gobby`
  application-name prefix note.

## V2: Verification
`kind: verification`

After each leaf, once the PD lifts the load breach:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_database_concurrency.py -q
cargo test -p gobby-core --features postgres-pool
cargo clippy -p gobby-core --features postgres-pool --all-targets -- -D warnings
cargo tree -p gobby-hooks -i deadpool-postgres
cargo tree -p gobby-core --features postgres-pool -i tokio-postgres
uv run gobby plans validate .gobby/plans/gcore-async-postgres.md -p /Users/josh/Projects/gobby
```

The Rust DB tests need `GOBBY_SCHEMA_TEST_DATABASE_URL` set to the
`gobby_test` hub; without it they skip, and a skip is not a pass for 1.2 or
P2 close evidence.
