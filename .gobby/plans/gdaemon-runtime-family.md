Plan artifact: `.gobby/plans/gdaemon-runtime-family.md`

# Gobby 1.0 Stage 2: runtime family (config, runtime handshake, grant issuing)

**Plan ID:** gdaemon-runtime-family

## Overview
`kind: framing`

Epic #21559 is the first Stage 2 takeover and the template for every later route
family. gdaemon serves part of `/api/config/*` and `/api/runtime/*` from a new
statically linked family crate, `crates/gruntime` (package `gobby-runtime`). Python
stays behind it as the proxy target and the rollback path. The plan also makes the
front door's `Compare` backend real. Compare mode records outcomes durably, and
`gdaemon compare-report` turns a soak window into a pass/fail verdict.

At close, interactive `gcode` handshakes, grant presentation and effective-config
reads are served by Rust. Managed principals (agent runs, tool chat, maintenance)
still go to Python. The three exit criteria in #21559 become this template:

1. A compare soak with zero mismatches over a representative window.
2. Killing the Python backend leaves the delegated routes serving.
3. Flipping the routing table, and rolling it back, needs no redeploy.

## Constraints
`kind: framing`

- **Stage gate.** The #21559 epic is `blocked-by` Stage 1 (#21543) in the task
  graph, so no leaf starts before Stage 1 closes. Stage 1 leaves #23457, #23458 and
  #23273 touch `crates/gdaemon/src/serve.rs`, `crates/gdaemon/src/state.rs` and
  `crates/gdaemon/src/lease/mod.rs`. Executors re-resolve those Targets against
  the post-Stage-1 tree before editing.
- **No backward compatibility.** 0.5.0 has not shipped.
- **Python stays as the rollback target.** Python keeps `runtime_handshake.py`,
  `runtime_config.py` and `configuration_effective.py`, and serves every request
  the native family does not claim. Deleting them belongs to the Python retirement
  stage. This plan defers no acceptance obligation to that stage.
- **Running daemon.** A crate change goes live only after `uv run gobby cutover`.
  A routing flip edits `front_door.routes` in `~/.gobby/bootstrap.yaml` and restarts
  gdaemon with the same binaries. That restart gets a `global` announcement and a
  quiet window first.
- **Heavy work.** Every granted cargo run sets `CARGO_BUILD_JOBS=3`. Scope runs with
  `-p` (`gobby-core`, `gobby-daemon`, `gobby-runtime`).
- **Dependencies.** No new external crate enters `Cargo.lock`. axum (0.8,
  `default-features = false`) enters gcore only behind the new `route-family`
  feature. Crypto uses `openssl` (a direct gcore dependency, vendored) and `fernet`
  0.2.2 (already locked).
- **Secrets never leave the process.** Neither the compare log nor any error body
  carries a response body value. Mismatch details name a JSON pointer only.
- **Monolith ceiling.** Every targeted Rust file stays under 1,000 lines, counting
  inline `#[cfg(test)]` modules. New tests go in `tests/` files or in
  `<module>/tests.rs`.

## Decision Record
`kind: framing`

These rulings came from Orchestrator #15977 on 2026-10-13, with operator approval
under stage2-plan:now.

- **D1, config source.** Native reads the *desired* configuration:
  `config_state.revision`, then `config_store` rows over the contract defaults from
  `crates/gcore/assets/config/runtime_config_contract.json`. No migration is needed.
  Consequence: while a restart-pending or failed-live key exists, native revision
  and values can differ from Python's in-memory active snapshot. A soak window must
  have no pending activation.
  Rejected: Python persisting its active snapshot, which needs a migration plus five
  derived carriers.
- **D2, principal kinds.** Rust issues and presents **interactive** principals only.
  The rest stay forwarded to Python:
  - capability-token bearers (`gobby-agent-v1.` for agent_run, tool_chat and
    maintenance);
  - the managed challenge;
  - grants whose principal is managed.
  Python's agent lifecycle owns per-execution bootstrap files, rotation and
  `grant.json` rewriting. Managed issuance therefore moves with S2.7 (#24149); see D1
  under Deferred.
- **D3, compare policy.** The native leg runs only for GET/HEAD requests the family
  claims. A claimed request with any other method is proxied once. The family then
  checks the proxied response; for the handshake, that means verifying the grant
  Python issued. Outcomes go to `~/.gobby/logs/front-door-compare.jsonl`, and
  `gdaemon compare-report` summarizes them.
- **D4, `/api/config` scope.** Only `GET /api/config/effective` is native. Every
  other `/api/config/*` route stays proxied under the claimed prefix; see D2 under
  Deferred.
- **Seam location.** The `RouteFamily` trait moves to gcore behind feature
  `route-family`, generic over the router state, so a family crate can implement it
  without a gdaemon dependency cycle.
  - It gains `claims(&Parts) -> bool` (default `true`) and `check_proxied(...)`
    (default `Skipped`).
  - The routing table consults `claims` before choosing native, proxy or compare.
    An unclaimed request is proxied unchanged.
  Rejected: wrapping the family inside gdaemon, as gterminals does. That leaves later
  family crates unable to export the seam #21559 asks for.
- **Family identity.** One family, `runtime`, with prefixes `/api/runtime` and
  `/api/config`. It claims a request only when the request carries the front door's
  API-key identity, meaning AuthState inserted `x-gobby-key-id`. Cookie,
  break-glass, agent-bearer and unauthenticated requests are forwarded, so Python's
  auth semantics and error bodies stay authoritative for them.
- **Flip mechanics.** Routes are read once at gdaemon start. Flip and rollback are a
  bootstrap edit plus a gdaemon restart with the same binaries, which needs no
  redeploy. Rejected: hot reload of the route table, since nothing requires it.
- **Process-lifetime family.** gdaemon builds the runtime family once in
  `serve::run`, on the shared pool, and leaks it with `Box::leak`. It then has the
  same `'static` lifetime as `FAMILIES`, and `RouteTable::new` keeps its signature
  for every existing test caller.
- **Envelope initialization.** Native code initializes the secret envelope (key-file
  posture only) when no `secret_key_material` row exists, as Python's
  `_initialize_envelope` does with an insert-if-absent race. Without this, a fresh
  install whose backend never started could not issue.

**Granularity:** Eleven deliverables, one per independently testable outcome:

- seam (1.1);
- pool (1.2);
- compare mode (2.1);
- crate and effective config (3.1);
- presentation (3.2);
- secret store (3.3);
- handshake (3.4);
- proxied-grant check (3.5);
- corpus (4.1);
- e2e (4.2);
- docs (4.3).

Shared Targets force a linear order. `serve.rs` is shared by 1.2, 2.1 and 3.1;
`crates/gruntime/src/lib.rs` is shared by 3.1 through 3.5. Deliverables 3.1 and
3.4 exceed six target files because a new crate's manifest, module root and tests
land together. Splitting them would leave an unbuildable crate between leaves.

## P1: Family seam and shared pool
`kind: framing`

### 1.1 `RouteFamily` seam in gcore with per-request claims [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/Cargo.toml`
- `crates/gcore/src/lib.rs::*` — scope-reason: declare the feature-gated `route_family` module beside the existing module list; no existing symbol changes
- `crates/gcore/src/route_family.rs`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: the trait leaves this file; `Entry`, `RouteTable::new`, `RouteTable::dispatch`, `FAMILIES`, `TerminalFamily` and the in-file tests adopt the gcore trait and the claims gate
- `crates/gdaemon/src/front_door/health.rs::*` — scope-reason: `HealthFamily` implements the gcore trait; only the import and impl header change
- `crates/gdaemon/tests/http_contracts.rs::*` — scope-reason: `parity_violations` and `precheck_rejects_missing_or_mismatched_parity` name the trait with its state parameter
- `crates/gterminals/src/lib.rs::*` — scope-reason: the module doc comment says `RouteFamily` lives in gdaemon; correct it to gcore
- `Cargo.lock`

Move `pub trait RouteFamily` out of `crates/gdaemon/src/front_door/routes.rs` into a
new `crates/gcore/src/route_family.rs`, compiled under a new gcore feature
`route-family = ["dep:axum"]`. Add axum to gcore as
`axum = { version = "0.8", default-features = false, optional = true }`. Enable the
feature from `crates/gdaemon/Cargo.toml`'s `gobby-core` dependency, which today
enables `postgres` and `postgres-pool`.

The trait:

```rust
pub trait RouteFamily<S>: Send + Sync {
    fn name(&self) -> &'static str;
    fn prefixes(&self) -> &'static [&'static str];
    fn router(&self) -> axum::Router<S>;
    /// False sends the request to the proxy unchanged, whatever the backend.
    fn claims(&self, _parts: &axum::http::request::Parts) -> bool { true }
    /// Compare mode, claimed non-GET/HEAD request: check the proxied response against
    /// the request that produced it. `request` is the body bytes compare already buffered.
    fn check_proxied<'a>(&'a self, _parts: &'a Parts, _request: &'a [u8], _status: StatusCode, _response: &'a [u8])
        -> Pin<Box<dyn Future<Output = ProxiedCheck> + Send + 'a>> { Box::pin(async { ProxiedCheck::Skipped }) }
}
pub enum ProxiedCheck { Skipped, Verified, Mismatch(String) } // Mismatch carries a JSON pointer or reason, never values
```

A family with its own state returns `Router::new().route(..).with_state(own_state)`
for any `S`. This works because axum 0.8's `with_state` yields `Router<S2>`.

`routes.rs` changes:

- It re-exports the trait (`pub use gobby_core::route_family::RouteFamily;`).
- `FAMILIES` becomes `&[&'static dyn RouteFamily<FrontDoorState>]`.
- `RouteTable::new` takes `&[&'static dyn RouteFamily<FrontDoorState>]`. Existing
  callers pass `FAMILIES`.
- `Entry` stores the `&'static dyn` family next to its compiled router.
- `RouteTable::dispatch`, for `Native` and `Compare`, splits the request into parts
  and calls `entry.family.claims(&parts)`. `false` goes to `proxy::forward`
  unchanged.

`HealthFamily` (`crates/gdaemon/src/front_door/health.rs`) keeps
`Router<FrontDoorState>` and only re-points its import. The gdaemon stub
`TerminalFamily` stays.

Research context:
- Today the trait is `RouteFamily { name, prefixes, router() -> Router<FrontDoorState> }`
  (`routes.rs::RouteFamily`, lines 26–34). `RouteTable::new` applies
  `.fallback(proxy_fallback).with_state(state.clone())` to each family router.
  Unclaimed paths and methods already fall back to the proxy
  (`unclaimed_methods_and_paths_under_a_native_family_are_proxied`). This
  deliverable adds header-level claiming on top of that path-level fallback.
- Consumers swept with `gcode grep -w -E 'RouteFamily|FAMILIES|RouteTable|unimplemented_families' crates/`.
  `RouteTable::new(FAMILIES, …)` callers keep compiling unchanged:
  - `crates/gdaemon/src/serve.rs`;
  - `crates/gdaemon/tests/front_door.rs` (5 sites);
  - `tests/lease.rs`;
  - `tests/nodes.rs` (2 sites);
  - the `routes.rs` in-file tests.

  `tests/http_contracts.rs` spells `&[&dyn RouteFamily]` and must add
  `<FrontDoorState>`.
- gcore has no axum today. The ghook and gcode graphs must not gain it, so check
  with `cargo tree -p gobby-hooks -i axum`, which must report nothing.
- Planned checks:
  - `cargo nextest run -p gobby-daemon -E 'test(routes) | test(http_contracts)'`;
  - `cargo clippy -p gobby-core --features route-family`;
  - `cargo clippy -p gobby-daemon`.

**Acceptance:**

- 1.1.1 - gcore exposes `RouteFamily<S>` and `ProxiedCheck` behind `route-family`, with defaults `claims = true` and `check_proxied = Skipped`, and `cargo tree -p gobby-hooks -i axum` is empty. file: `crates/gcore/src/route_family.rs`.
- 1.1.2 - Under a `Native` or `Compare` backend, a request the family does not claim reaches the proxy unchanged and never the family router. test: `crates/gdaemon/src/front_door/routes.rs::unclaimed_requests_are_proxied_under_every_backend`.
- 1.1.3 - The existing routing behaviors still hold. test: `crates/gdaemon/src/front_door/routes.rs::health_family_honors_proxy_native_and_compare`.
- 1.1.4 - The corpus parity precheck compiles against the generic trait and passes. test: `crates/gdaemon/tests/http_contracts.rs::every_native_family_has_corpus_cases`.

### 1.2 One gdaemon pool for key resolution, the lease and hub node routes [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` builds the shared pool once and passes it to the key resolver, `RuntimeState::prepare` and `HubNodes`; two cfg-gated functions share the name `run`
- `crates/gdaemon/src/front_door/auth.rs::PostgresKeyResolver::new`
- `crates/gdaemon/src/lease/mod.rs::ActiveDaemonLease::new`
- `crates/gdaemon/src/state.rs::RuntimeState::prepare`
- `crates/gdaemon/tests/front_door.rs::postgres_resolver_and_resolverless_door_fail_closed`
- `crates/gdaemon/tests/nodes.rs::*` — scope-reason: two harness sites build `PostgresKeyResolver` from the test database URL
- `crates/gdaemon/tests/lease.rs::*` — scope-reason: every `ActiveDaemonLease::new` site passes a pool
- `crates/gdaemon/tests/lifecycle.rs::*` — scope-reason: the lease helper passes a pool
- `crates/gdaemon/tests/run_modes.rs::*` — scope-reason: `RuntimeState::prepare` calls pass `None` for the pool
- `crates/gdaemon/examples/backend_supervisor_fixture.rs::*` — scope-reason: the fixture lease passes a pool

#21559 requires the key resolver and the lease to move onto one shared gcore pool,
with their transaction boundaries preserved. In non-node mode, `serve::run` builds
one `gobby_core::postgres_pool::Pool` from `bootstrap.database_url` when the URL is
present, before the resolver is built:

- `max_size` 8;
- `application_name` `gobby-gdaemon`;
- `acquire_timeout` 5 s.

Wire the pool through:

- `PostgresKeyResolver::new(pool: Pool)` replaces `new(database_url)`. Keep the
  per-call `pool.get()`, the 5 s query timeout and the `last_touch` throttle.
- `RuntimeState::prepare(bootstrap, home, secret, pool: Option<Pool>)` keeps both
  refusals. With `None` and no database URL it still fails with "native runtime
  ownership requires bootstrap database_url". It passes the pool to
  `ActiveDaemonLease::new(pool, machine_id, deployment_token, mode)`.
- `ActiveDaemonLease::connect` keeps `deadpool_postgres::Object::take`,
  `RESET ROLE; SET statement_timeout = '5s'` and the lease application name. The
  taken connection leaves the pool, so the advisory-lock session stays dedicated.
- The hub `HubNodes { db }` uses the same pool. Its own `Pool::build`
  ("gobby-gdaemon-nodes", max 4) goes away.

`ActiveServices` (`state_services.rs`) keeps its tenure-scoped pools. It is outside
#21559's constraint.

Research context:
- Pools today:
  - `auth.rs::PostgresKeyResolver::new` builds max 2, "gobby-gdaemon-key-resolver";
  - `lease/mod.rs::ActiveDaemonLease::new` builds max 2, "gobby-gdaemon-lease-connect";
  - `serve.rs::run` builds max 4, "gobby-gdaemon-nodes", after `RuntimeState::prepare`.

  The gcore pool's `SessionHook::establish` runs `SET ROLE gobby_daemon_runtime` on
  every pooled session. The lease resets it on its taken connection.
- Consumers of the changed constructors:
  - `ActiveDaemonLease::new`: `state.rs`, `tests/lease.rs` (lines 179 and 433),
    `tests/lifecycle.rs:35` and `examples/backend_supervisor_fixture.rs:25`.
  - `PostgresKeyResolver::new`: `serve.rs:69`, `tests/front_door.rs:238` and
    `tests/nodes.rs` (lines 800 and 1935).
  - `RuntimeState::prepare`: `serve.rs:123` and `tests/run_modes.rs` (lines 64
    and 81).

  The node-mode refusal tests must still refuse before any connection. Build their
  pool lazily from a syntactically valid URL; `Pool::build` does not connect.
  `postgres_resolver_and_resolverless_door_fail_closed` builds its pool from an
  unreachable URL such as `postgresql://test@127.0.0.1:1/test` instead of an invalid
  DSN.
- Planned checks: `cargo nextest run -p gobby-daemon --test lease --test lifecycle --test run_modes --test front_door --test nodes`.

**Acceptance:**

- 1.2.1 - `serve::run` builds exactly one pool, used by the resolver, the lease and hub node routes, and neither `auth.rs` nor `lease/mod.rs` calls `Pool::build`. file: `crates/gdaemon/src/serve.rs`.
- 1.2.2 - The lease still holds a dedicated advisory-lock session that drop and failed transactions release. test: `crates/gdaemon/tests/lease.rs::failed_transaction_and_drop_release_the_dedicated_session`.
- 1.2.3 - Epoch and signing secret still rotate together on acquisition. test: `crates/gdaemon/tests/lease.rs::epoch_and_secret_rotate_together`.
- 1.2.4 - A resolver on an unreachable pool fails closed. test: `crates/gdaemon/tests/front_door.rs::postgres_resolver_and_resolverless_door_fail_closed`.

## P2: Compare mode
`kind: framing`

### 2.1 Compare policy, compare log and `gdaemon compare-report` [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/routes.rs::*` — scope-reason: `compare` and `dispatch` apply the method policy, value equality for JSON and the proxied check, and record every outcome
- `crates/gdaemon/src/front_door/compare.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: `FrontDoorState` gains `compare_log: Option<Arc<CompareLog>>`, which `FrontDoorState::new` sets to `None`; the module declares `compare`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` opens the compare log under the Gobby home and `serve_with_runtime` sets it on every listener's state
- `crates/gdaemon/src/main.rs::*` — scope-reason: add `Command::CompareReport { since }` and dispatch it in `main`
- `crates/gdaemon/tests/compare.rs`

Compare-mode policy for a claimed request (after the 1.1 claims gate):

- **GET or HEAD:**
  1. Run both legs as today (`tokio::join!`) and return the proxied response.
  2. They match when the statuses are equal, the `content-type` and
     `cache-control` headers are equal, and the bodies are equal. When both are
     `application/json`, bodies compare by parsed `serde_json::Value`, which
     ignores key order; otherwise they compare byte for byte.
  3. Record `match` or `diff`. A diff's detail is the first differing JSON pointer,
     or `status`, `header:<name>` or `bytes`.
  4. A native leg that errors or fails to collect is recorded as `native_error`
     and never changes the returned response.
- **Any other method:**
  1. Keep the request body bytes `compare` already buffers, then proxy once with a
     clone of them (`Bytes` clones share the buffer; the body is never read twice).
  2. Collect the proxied body and call
     `entry.family.check_proxied(&parts, &request_body, status, &response_body)`.
  3. Record `verified`, `mismatch` (detail from the check) or `skipped`.
  4. Return the proxied response rebuilt from the collected bytes.

An unclaimed request is proxied and not recorded.

`compare.rs` owns the compare log:

- **Record.** One JSON object per line:
  `{"ts": <unix seconds>, "family", "method", "path", "outcome", "native_status", "proxied_status", "detail"}`.
  The path carries no query string and the record carries no body values.
- **Writer.** `CompareLog::open(home)` appends to
  `<home>/logs/front-door-compare.jsonl`, creating `logs/` if needed. It writes
  each line with a single `write_all` on an `O_APPEND` file behind a `Mutex`. A
  failed write logs once to stderr and never fails the request.
- **Report.** `pub fn report(path, since: Option<i64>) -> Result<Report>` counts
  outcomes per family within the window and records `first_ts` and `last_ts`. A
  malformed line is an error.

`gdaemon compare-report [--since <unix-seconds>]`:

- reads `<gobby_home()>/logs/front-door-compare.jsonl`;
- prints the report as JSON;
- exits 1 when the window holds any `diff`, `native_error` or `mismatch`, and also
  exits 1 for an empty window, since an empty window proves nothing;
- exits 0 otherwise.

`serve::run` opens the log once, where `home` is in scope, and
`serve_with_runtime` sets it on each `FrontDoorState`.

Research context:
- Today `routes.rs::compare` already joins both legs and returns the proxied
  response. It compares status plus raw bytes and `eprintln!`s diffs only. It runs
  the native leg for every method. For a POST handshake that would issue a second
  interactive credential, racing Python's
  `issue_or_reuse_interactive_principal`, which D3 forbids. It also records nothing
  on a match, so a soak has no denominator.
- `FrontDoorState::new(backend, BackendState, auth)` has about a dozen test callers
  across `crates/gdaemon/tests/` and `routes.rs`. They stay unchanged because the
  new field defaults to `None`, and a `None` log records nothing.
- `main.rs` uses clap `Command { Schema, Serve }`, and `gobby_core::gobby_home()`
  resolves `GOBBY_HOME`.
- Planned checks:
  - `cargo nextest run -p gobby-daemon --test compare -E 'test(routes)'`;
  - `cargo clippy -p gobby-daemon`.

**Acceptance:**

- 2.1.1 - A claimed GET in Compare mode returns the proxied response and records `match`, or `diff` with a pointer and no values. test: `crates/gdaemon/tests/compare.rs::get_compare_records_match_and_diff_without_values`.
- 2.1.2 - JSON bodies that differ only in key order record `match`. test: `crates/gdaemon/tests/compare.rs::json_bodies_compare_by_value`.
- 2.1.3 - A claimed POST in Compare mode never reaches the native router, the backend and `check_proxied` both receive the original request bytes, and the family check's outcome is recorded. test: `crates/gdaemon/tests/compare.rs::non_get_requests_run_only_the_proxied_leg`.
- 2.1.4 - A failing native leg records `native_error` and the caller still receives the proxied response. test: `crates/gdaemon/tests/compare.rs::native_failure_returns_the_proxied_response`.
- 2.1.5 - The report counts per family within `--since` and fails on any diff, native_error or mismatch, and on an empty window. test: `crates/gdaemon/tests/compare.rs::report_fails_on_mismatch_and_empty_window`.

## P3: `gobby-runtime` family crate
`kind: framing`

### 3.1 `gobby-runtime` crate and native `GET /api/config/effective` [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `Cargo.toml`
- `Cargo.lock`
- `crates/gruntime/Cargo.toml`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/config.rs`
- `crates/gruntime/tests/config.rs`
- `crates/gcore/src/config/runtime_contract.rs::*` — scope-reason: `ExactKeySpec` parses `default`, and a new public accessor returns the machine-exported exact keys with their defaults and secrecy
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` builds the runtime family on the shared pool, leaks it once, and composes it after `FAMILIES` for `unimplemented_families` and every listener's `RouteTable::new`

Create the crate and register it:

- Create `crates/gruntime` with package `gobby-runtime`, lib `gobby_runtime` and
  `publish = false`. It takes `version`, `edition`, `rust-version`, `license` and
  `repository` from `[workspace.package]` and `[lints] workspace = true`.
- Add `crates/gruntime` to the root `members`. `[workspace.package]` already exists.
- Dependencies:
  - `gobby-core` with features `postgres`, `postgres-pool` and `route-family`;
  - `axum` 0.8 (`default-features = false`);
  - `serde`, `serde_json`, `tokio` and `uuid`, at the versions gdaemon already uses.
- `crates/gdaemon/Cargo.toml` depends on `gobby-runtime` by path.

`lib.rs` exports `RuntimeFamily`:

- `RuntimeFamily::new(pool: Pool, home: PathBuf)`;
- `name()` returns `"runtime"`;
- `prefixes()` returns `&["/api/runtime", "/api/config"]`;
- `router<S>()` returns its routes with an internal `Arc<RuntimeState { pool, home }>`
  applied;
- `claims(parts)` requires an `x-gobby-key-id` header. Under `/api/config` it is
  true only for `GET /api/config/effective`. 3.2 and 3.4 extend `claims` to their
  `/api/runtime` routes and leave the 3.1 test unchanged, because that test asserts
  only `/api/config` paths and unidentified callers.

The front door strips client-sent identity headers and inserts them only for a
resolved API key (`auth.rs::AuthState::authenticate`, `IDENTITY_HEADERS`).

`config.rs`:

- `desired_snapshot(&Pool) -> Snapshot { revision, values }` reads, in one
  statement:

  ```sql
  SELECT state.revision, store.key, store.value
  FROM config_state state LEFT JOIN config_store store ON TRUE
  WHERE state.id = TRUE ORDER BY store.key
  ```

  This mirrors `src/gobby/storage/config_repository.py::ConfigRepository._read_coherent`,
  including its two edge rules. Zero rows means `config_state` is absent, which is
  an error (the handler's 500). With an empty `config_store` the join yields one
  row with a null key, and rows with a null key are dropped, so the snapshot is
  the revision plus contract defaults.
  `values` are the contract defaults overlaid with decoded non-null-key rows. Decoding follows
  `config_repository.py::decode_config_value`, as in `_complete_values`.
- `machine_values(&Snapshot)` mirrors
  `src/gobby/servers/routes/configuration_effective.py::_machine_config_values`:
  1. Walk keys in sorted order and keep `is_machine_config_key(key)`.
  2. Skip null values and any key whose secrecy is not `none`.
  3. Stringify: a string is kept as is; anything else uses Python `json.dumps`
     defaults (`", "` and `": "` separators, `ensure_ascii`).
  4. Expand `${VAR}` and `${VAR:-default}` from gdaemon's environment with
     `src/gobby/config/_loading.py::expand_env_vars` semantics.
  5. Drop values still containing `$secret:` or `${…}`.

The handler returns 200 with `{"revision": <config_state.revision>, "config": {…}}`,
`Cache-Control: no-store` and `application/json`. A database failure returns 500
`{"detail": "Internal server error"}`, matching the Python route.

In `serve::run`, build the family when the shared pool exists. Make it `'static`
with `Box::leak(Box::new(RuntimeFamily::new(pool.clone(), home.to_owned())))` and
pass `FAMILIES` plus it to `unimplemented_families` and `RouteTable::new`.

Research context:
- Under D1, values are desired, not active. Python serves the active
  `ConfigRuntime` snapshot (`src/gobby/config/runtime.py::ConfigRuntime._initialize`
  sets active from `stored.values`).
- Nine exact keys are machine-exported today: `ai.embeddings.dim`, `.model`,
  `.query_prefix`, `.routing` and `.timeout_seconds`; `databases.falkordb.host` and
  `.port`; `databases.qdrant.url`; and `indexing.respect_gitignore`. All have secrecy
  `none`, and no pattern key is exported, so `is_machine_config_key` suffices.
  `runtime_contract.rs::ExactKeySpec` parses only `key`, `machine_export` and
  `secrecy` today, so `default` must be added.
- The `crates/AGENTS.md` family-crate convention applies: one exported family, rows
  and repositories on the gcore pool.
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test config`, which is DB-backed through
    the test-database URL the existing gdaemon DB tests use;
  - `cargo clippy -p gobby-runtime`.

**Acceptance:**

- 3.1.1 - `crates/gruntime` builds as workspace member `gobby-runtime` (publish false, workspace version) and gdaemon links it. file: `crates/gruntime/Cargo.toml`.
- 3.1.2 - `GET /api/config/effective` with an API-key identity returns Python's projection of the desired configuration with `no-store`. test: `crates/gruntime/tests/config.rs::effective_config_matches_python_projection`.
- 3.1.3 - Under `/api/config` the family claims only identified `GET /api/config/effective`; every other `/api/config` path or method, and an unidentified caller on any path, is forwarded. test: `crates/gruntime/tests/config.rs::only_identified_effective_reads_are_claimed`.
- 3.1.5 - With an empty `config_store` the snapshot is the revision plus contract defaults, and a missing `config_state` row returns 500. test: `crates/gruntime/tests/config.rs::empty_store_serves_defaults_and_missing_state_fails`.
- 3.1.4 - Values that still hold `$secret:` or `${…}` after expansion are omitted. test: `crates/gruntime/tests/config.rs::unresolved_values_are_omitted`.

### 3.2 Grant signing and presentation in gcore; native `GET /api/runtime/config` [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/grant/mod.rs::*` — scope-reason: declare and re-export the `presentation` module
- `crates/gcore/src/grant/presentation/mod.rs`
- `crates/gcore/src/grant/presentation/tests.rs`
- `crates/gcore/src/schema/runner.rs::auth_schema_for`
- `crates/gcore/src/schema/mod.rs`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/config.rs`
- `crates/gruntime/src/grant.rs`
- `crates/gruntime/tests/grant.rs`

`gcore::grant::presentation` holds the server half #21559 names:

- `sign_grant(grant, secret) -> Result<GrantBundle>` sets `payload_checksum`
  (existing `payload_checksum`) and `signature`. The signature is the hex
  HMAC-SHA256 with key `secret.as_bytes()` over `canonical_payload_bytes`, using
  `openssl::sign::Signer` with `PKey::hmac`.
- `signature_matches` compares with `openssl::memcmp::eq`.
- `decode_grant_header(&str)` decodes base64url without padding, then parses with
  `parse_grant_json`.
- `Rejection` codes: `invalid_signature`, `wrong_deployment`, `wrong_schema`,
  `wrong_api_contract`, `stale_epoch`, `expired`, `revoked`, `wrong_capability`,
  `claims_mismatch`, `credential_issuance_failed`, `credential_before_proof`.
- `Rejection::status()` returns 409 for `stale_epoch`; 401 for `expired` and
  `credential_before_proof`; 403 otherwise. `Rejection::body()` returns
  `{"code", "error": code, "message"}`.
- `verify_static(grant, &PresentationContext { token, fencing_epoch, signing_secret }, now)`
  runs Python's order: signature, checksum, deployment token, schema identity
  (`expected_schema_identity`), api_contract and version, fencing epoch, expiry. The
  caller then checks revocation, then capability.
- Make `schema::runner::auth_schema_for` `pub` and re-export it from
  `schema/mod.rs`.

`gruntime/src/grant.rs`:

- `fence(&Pool, token)` runs
  `SELECT fencing_epoch, grant_signing_secret FROM deployment_runtime WHERE deployment_token = $1`
  per request, as `src/gobby/runtime_grants/lease_view.py` does. The token comes
  from `gobby_core::grant::deployment_token(home)`.
- `interactive_revoked(...)` runs
  `SELECT * FROM <auth>.lookup_interactive_principal($token, $daemon_machine, $project, $generation)`.
  No row or a non-null `revoked_at` means revoked, as in
  `src/gobby/storage/interactive_credentials.py::InteractiveCredentialMixin.interactive_generation_revoked`.
  `<auth>` is `auth_schema_for(current_schema())`. The daemon machine comes from
  `gobby_core::machine::read_machine_id_from_home(home)`.
- The `GET /api/runtime/config` handler returns
  `{"config_revision": snapshot.revision, "settings": machine_values(snapshot)}` with
  `no-store`. Rejections use `Rejection::status()` and `Rejection::body()`.

This deliverable extends 3.1's `claims` and leaves `crates/gruntime/tests/config.rs`
unchanged. `claims` for `GET /api/runtime/config` is true only when the request has
`x-gobby-key-id` and an `X-Gobby-Runtime-Grant` header that decodes to a grant with
principal kind `interactive`. Managed grants, undecodable headers and missing
headers go to Python, which keeps its 400 `malformed grant` and 401 answers.

Research context:
- Python reference:
  - `src/gobby/runtime_grants/service.py::GrantService.present` (check order);
  - `src/gobby/runtime_grants/signing.py` (canonical JSON minus checksum and
    signature; sha256 checksum; HMAC signature);
  - `src/gobby/servers/routes/runtime_handshake.py::grant_error_response` (status
    mapping);
  - `src/gobby/servers/routes/runtime_config.py::create_runtime_config_router`
    (route body);
  - `src/gobby/runtime_grants/handshake.py::decode_grant_header` (padding).
- Revocation:
  - The in-memory `GrantRevocationStore` checksum set has no production caller;
    `GrantService.revoke` is unused.
  - Interactive revocation is DB truth through `lookup_interactive_principal`;
    `runner_init/servers.py` sets `_principal_revoked` for exactly that.
  - Managed revocation is in-memory in Python, which is one reason managed grants
    are not claimed (D2).
- The Rust client half lives in `crates/gcore/src/grant/bundle.rs`:
  `canonical_payload_bytes`, `payload_checksum`, `GRANT_VERSION`,
  `EXPECTED_API_CONTRACT`, `expected_schema_identity` and `parse_grant_json`.
  `GrantError::from_presentation_http` already parses the rejection body shape.
- `auth_schema_for` consumers stay unchanged:
  - `crates/gcore/src/schema/runner_tests.rs` and `crates/gcore/src/schema/verify.rs`
    make visibility-only use of it;
  - `crates/gcore/src/schema/runner.rs` calls it itself.
- Signing parity uses a Python-generated vector. Produce one grant with
  `uv run python -c` calling `gobby.runtime_grants.signing.sign_grant` and a fixed
  secret, then commit it inline as test constants.
- Planned checks:
  - `cargo nextest run -p gobby-core -E 'test(presentation)'`;
  - `cargo nextest run -p gobby-runtime --test grant`.

Consumers unchanged:
- `crates/gcore/src/schema/runner_tests.rs` — no-edit-reason: it already imports `auth_schema_for` through `super::runner`; widening visibility changes nothing.
- `crates/gcore/src/schema/verify.rs` — no-edit-reason: it calls `auth_schema_for(schema)` with the same signature.

**Acceptance:**

- 3.2.1 - `sign_grant` reproduces a Python-signed grant's checksum and signature byte for byte, and `signature_matches` accepts it. test: `crates/gcore/src/grant/presentation/tests.rs::signature_matches_python_vector`.
- 3.2.2 - Each rejection code maps to its status and `{code, error, message}` body. test: `crates/gcore/src/grant/presentation/tests.rs::rejection_matrix_maps_codes_to_statuses`.
- 3.2.3 - `verify_static` reports the first failing check in Python's order. test: `crates/gcore/src/grant/presentation/tests.rs::checks_run_in_python_order`.
- 3.2.4 - `GET /api/runtime/config` with a valid interactive grant returns `{config_revision, settings}`, a stale epoch returns 409, and a revoked generation returns 403 `revoked`. test: `crates/gruntime/tests/grant.rs::runtime_config_presents_interactive_grants`.
- 3.2.5 - Managed-principal grants, undecodable headers and missing headers are not claimed. test: `crates/gruntime/tests/grant.rs::managed_and_malformed_grants_are_not_claimed`.

### 3.3 Secret-store port: envelope, secret reads and credential sealing [category: code] (depends: 3.2)
`kind: deliverable`

Targets:
- `crates/gruntime/Cargo.toml`
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/secrets.rs`
- `crates/gruntime/tests/secrets.rs`
- `crates/gruntime/tests/fixtures/secret_seal_vector.json`
- `tests/storage/test_secret_seal_vector.py`

`secrets.rs` reads and writes Python's envelope format through `openssl` and
`fernet`:

- **Key material.** Load the `secret_key_material` row: `kek_posture`, `kek_salt`,
  `kek_kdf_n`, `kek_kdf_r`, `kek_kdf_p`, `wrapped_dek`.
- **KEK.**
  - Posture `key_file` uses the Fernet key in `<home>/.secret_kek`, read as
    `src/gobby/storage/secrets.py::_read_kek_file` reads it.
  - Posture `scrypt_passphrase` derives
    `urlsafe_b64(scrypt(GOBBY_SECRET_KEK_PASSPHRASE, salt, n, r, p, 32))` with
    `openssl::pkcs5::scrypt`.
- **DEK.** `Fernet(KEK).decrypt(wrapped_dek)`.
- **Secret reads.** `secret(name)` looks up the global row as
  `SecretStore._lookup_secret_row` does and returns
  `Fernet(DEK).decrypt(encrypted_value)`.
- **Sealing.**
  - Seal key: HKDF-SHA256 with ikm `urlsafe_b64decode(DEK)`, no salt, info
    `gobby.secret-store.seal.v1`, length 32.
  - `seal(plaintext, aad)` returns `b64(nonce12 ‖ AES-256-GCM(ciphertext ‖ tag))`.
  - `open_sealed` rejects tokens shorter than 28 bytes and any AAD mismatch.
- **Missing row, key-file posture.** Initialize as
  `SecretStore._initialize_envelope` and `_get_or_create_kek_file_key` do:
  1. Create the KEK file with exclusive-create mode 0600, or read the winner's file.
  2. Generate a DEK.
  3. Insert the key material only if absent.
  4. On a lost race, re-read the row and unwrap the winner.

  A `scrypt_passphrase` envelope is never initialized natively.
- **Unwrap failure.** Becomes `SecretError::Unavailable`, which handlers map to 503
  `secret_key_unavailable`.

The shared vector (`tests/fixtures/secret_seal_vector.json`) holds:

- a key-file KEK;
- the wrapped DEK row;
- a Python-sealed token with its AAD and plaintext;
- a Rust-sealed token with its AAD and plaintext;
- a Python-encrypted `secrets` value.

Python generates its half; the Rust test generates and commits its half once. Each
language opens the other's token.

Research context:
- The Python source of truth is `src/gobby/storage/secrets.py`:
  - `SecretStore._kek_fernet`, `_unwrap_dek`, `_seal_aes_key`, `seal`,
    `open_sealed` and `get`;
  - `_derive_scrypt_fernet_key` with `SCRYPT_N = 2**14`, `SCRYPT_R = 8` and
    `SCRYPT_P = 1`;
  - `_AES_GCM_NONCE_LEN = 12` and `_AES_GCM_MIN_TOKEN_LEN = 28`;
  - `POSTURE_KEY_FILE = "key_file"`.
- `fernet` 0.2.2 is a non-optional gcore dependency that no Rust code uses today
  (`crates/gcore/Cargo.toml` comment). `openssl` 0.10 (vendored) provides HKDF
  (`pkey_ctx`), AES-GCM (`symm::encrypt_aead`) and scrypt. Neither adds a crate to
  `Cargo.lock`.
- `gobby_daemon_runtime`, the pool's session role, has SELECT, INSERT, UPDATE and
  DELETE on `secret_key_material` (baseline grant).
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test secrets`;
  - `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_secret_seal_vector.py -v`.

**Acceptance:**

- 3.3.1 - Rust opens the Python-sealed token and decrypts the Python-encrypted secret from the committed vector. test: `crates/gruntime/tests/secrets.rs::opens_python_sealed_vector`.
- 3.3.2 - Python opens the Rust-sealed token from the committed vector. test: `tests/storage/test_secret_seal_vector.py::test_python_opens_rust_sealed_vector`.
- 3.3.3 - DEK unwrap works for the key-file and scrypt postures against Python-written rows. test: `crates/gruntime/tests/secrets.rs::unwraps_both_postures`.
- 3.3.4 - Two concurrent key-file initializations converge on one DEK and a 0600 KEK file. test: `crates/gruntime/tests/secrets.rs::concurrent_envelope_initialization_converges`.
- 3.3.5 - A wrong AAD or a truncated token fails to open. test: `crates/gruntime/tests/secrets.rs::open_rejects_wrong_aad_and_truncation`.

### 3.4 Native interactive handshake and grant capabilities [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/handshake.rs`
- `crates/gruntime/src/capabilities.rs`
- `crates/gruntime/tests/handshake.rs`
- `crates/gruntime/tests/capabilities.rs`
- `crates/gruntime/tests/fixtures/capability_vectors.json`
- `tests/runtime_grants/test_capability_vectors.py`

This deliverable extends 3.2's `claims` and leaves `crates/gruntime/tests/config.rs`
unchanged. `claims` for `POST /api/runtime/handshake` is true only when the request carries
`x-gobby-key-id` and no `gobby-agent-v1.` bearer. The handler mirrors
`src/gobby/servers/routes/runtime_handshake.py::handshake` together with
`src/gobby/runtime_grants/handshake.py::HandshakeService.issue_for_operator`:

1. Parse `{machine_id, project_id, session_id?, code_overlay_project_id?}` and reject
   unknown fields. An invalid body returns 422 `{"detail": "invalid handshake request"}`.
   This is a documented divergence from FastAPI's validation list; gcore never sends
   such a body.
2. Require `machine_id` to equal the `x-gobby-machine-id` identity header.
3. Admit the project with `SELECT 1 FROM projects WHERE id = $1 AND deleted_at IS NULL`.
4. Normalize the overlay claim as `_normalized_overlay_claim` does: it must be a
   UUID and must differ from the project.

   Steps 2–4 reject with 403 `claims_mismatch`.
5. Run `SELECT <auth>.heartbeat_daemon($daemon_machine, interval '2 minutes')`.
6. In one transaction:
   - Call `<auth>.issue_or_reuse_interactive_principal(token, daemon_machine, project, session, now + 1h, password, overlay)`.
     The password is 32 random bytes as base64url without padding.
   - When `reused`, load and open the sealed material.
   - Otherwise seal the password and store it.
   - Follow `interactive_credentials.py::_load_interactive_password`,
     `_store_interactive_password` and `_interactive_aad` exactly.
7. Build the DSN from the bootstrap `database_url`, with user and password replaced,
   in libpq key=value form, as `_interactive_dsn` does.
8. Issue the grant:
   - `PostgresDirect { dsn, role_name, credential_generation, valid_until }`;
   - TTL `min(3600, valid_until − now)` and at least 1;
   - capabilities from the desired snapshot;
   - `GrantBundle { version 2, api_contract 1, config_revision, deployment {token, fencing_epoch}, schema_identity, principal {interactive, machine, project, execution_id: null, session_id, overlay}, capabilities, issued_at, expires_at }`;
   - `sign_grant`.
9. Respond 200
   `{"grant": …, "deployment_token": …, "fencing_epoch": …}` with `no-store`.

Failure mapping:

- Overlay SQLSTATE 23503 or 22023 maps to `claims_mismatch`, as
  `_raise_if_overlay_rejected` does.
- Any other issuance failure returns 403 `credential_issuance_failed`.
- A missing fence or an unavailable secret store returns 503.

`capabilities.rs` ports `src/gobby/runtime_grants/service.py::capabilities_from_snapshot`
against desired values and resolved secrets:

- **falkordb.** Host and port come from config. The password comes from the
  secret-reference binding for `databases.falkordb.password` (via 3.3), else a
  plaintext value. Mode is `direct`, `brokered` or `unavailable`, following
  `_falkor_capability`.
- **qdrant.** URL and `api_key` follow `_qdrant_capability` and
  `_url_requires_broker`.
- **embed.** Enabled when the embeddings model is set and resolved.
- **text_generate, tool_chat.** Always daemon.
- **vision_extract.** Enabled when any generation endpoint's input modalities
  include `image`.
- **audio_transcribe.** Enabled when voice is enabled with STT, or any
  OpenAI-compatible audio binding has transcription enabled.
- **broker_operations.** In order: embed, falkor operations, qdrant operations.

Map each typed path to its flat contract key the way
`config_repository.py::ConfigRepository._runtime_path` does.

`tests/fixtures/capability_vectors.json` holds config-value sets with the expected
capability JSON from Python. The Python test asserts the vectors still match
`capabilities_from_snapshot`, and the Rust test asserts the port matches them.

Research context:
- Python issuance:
  - `src/gobby/runner_init/servers.py::issue_grant_postgres`, interactive branch,
    with a 1 h expiry;
  - `src/gobby/storage/interactive_credentials.py::InteractiveCredentialMixin.issue_interactive`;
  - `src/gobby/storage/managed_credentials.py`, where `heartbeat` uses
    `DAEMON_LEASE_DURATION` = 2 minutes;
  - `src/gobby/runtime_grants/handshake.py::HandshakeService._issue` (TTL);
  - `src/gobby/runtime_grants/service.py::GrantService.issue` (bundle fields).
- In-process state:
  - Python's per-principal `threading.Lock` is process-local. The SQL function's
    advisory transaction lock gives the same guarantee across processes.
  - `remember_interactive_grant_expiry` and `rotate_interactive` have no production
    callers, so native issuance needs no in-memory expiry tracking.
- `gobby_daemon_runtime` holds EXECUTE on the auth functions (baseline grants).
- Planned checks:
  - `cargo nextest run -p gobby-runtime --test handshake --test capabilities`;
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_capability_vectors.py -v`.

**Acceptance:**

- 3.4.1 - An identified operator handshake returns a signed interactive grant whose payload fields equal Python's for the same database state. test: `crates/gruntime/tests/handshake.rs::interactive_handshake_issues_signed_grant`.
- 3.4.2 - A repeat handshake reuses the principal and returns the stored password, and a fresh issuance stores material that Python opens. test: `crates/gruntime/tests/handshake.rs::reused_principal_returns_the_sealed_password`.
- 3.4.3 - A machine mismatch, an unadmitted project and a bad overlay return 403 `claims_mismatch`, and other issuance failures return 403 `credential_issuance_failed`. test: `crates/gruntime/tests/handshake.rs::handshake_rejections_match_python_codes`.
- 3.4.4 - Agent-bearer, cookie-only and unauthenticated handshakes and the managed challenge are not claimed and reach Python unchanged. test: `crates/gruntime/tests/handshake.rs::non_operator_handshakes_are_not_claimed`.
- 3.4.5 - The Rust capability port matches every Python-generated vector. test: `crates/gruntime/tests/capabilities.rs::capabilities_match_python_vectors`.
- 3.4.6 - The committed vectors still match Python's `capabilities_from_snapshot`. test: `tests/runtime_grants/test_capability_vectors.py::test_vectors_match_python_capabilities`.

### 3.5 Compare check for proxied handshake grants [category: code] (depends: 3.4)
`kind: deliverable`

Targets:
- `crates/gruntime/src/lib.rs`
- `crates/gruntime/src/verify.rs`
- `crates/gruntime/tests/verify.rs`

`RuntimeFamily::check_proxied` handles a claimed `POST /api/runtime/handshake`
whose proxied status is 200. Any other status returns `Skipped`. For a 200 it
parses `{grant, deployment_token, fencing_epoch}` and returns `Verified` only when
all of the following hold:

- `verify_static` passes against the current fence;
- the top-level `deployment_token` and `fencing_epoch` equal the grant's;
- the principal equals the request's claims: kind `interactive`, and the same
  machine, project, session and overlay;
- `config_revision`, plus every capability except `postgres`, equal a fresh native
  computation from the desired snapshot (3.1, 3.4);
- `expires_at ≤ issued_at + 3600` and `expires_at ≤ postgres.valid_until`.

Otherwise it returns `Mismatch(<first differing JSON pointer>)`, never values. The
project, session and overlay claims come from the `request` bytes that 1.1's
`check_proxied` signature carries and 2.1 passes from compare's single buffered
copy; the machine comes from the `x-gobby-machine-id` header on `parts`. An
unparseable request body returns `Mismatch("/request")`.

Research context:
- D3 rules this check because a native leg would issue a second credential. Python
  and Rust sign with the same `deployment_runtime.grant_signing_secret`, so
  signature verification is a real cross-implementation check. The capability
  recomputation validates 3.4's port on live traffic.
- A D1 desired-versus-active divergence surfaces here as a `config_revision`
  mismatch. That is correct behavior: the soak window must not contain a pending
  activation.
- Planned check: `cargo nextest run -p gobby-runtime --test verify`.

**Acceptance:**

- 3.5.1 - A Python-issued grant for the request records `verified`. test: `crates/gruntime/tests/verify.rs::python_issued_grant_verifies`.
- 3.5.2 - A tampered capability, a wrong epoch, a bad signature or a principal mismatch records `mismatch` with the field pointer and no values. test: `crates/gruntime/tests/verify.rs::mismatches_name_the_field_without_values`.

## P4: Parity evidence and the template docs
`kind: framing`

### 4.1 Corpus: `runtime` family cases replayed natively [category: test] (depends: 3.5)
`kind: deliverable`

Targets:
- `tests/contracts/http/manifest.json::*` — scope-reason: the `config` and `runtime_handshake` family entries collapse into one native `runtime` family
- `tests/contracts/http/runtime_handshake.json::*` — scope-reason: its cases move to the `runtime` family with grant masks unchanged
- `tests/contracts/http/config_schema.json::*` — scope-reason: its case moves to the `runtime` family and stays proxied by prefix
- `tests/contracts/http/config_values.json::*` — scope-reason: its case moves to the `runtime` family and stays proxied by prefix
- `tests/contracts/http/config_effective.json`
- `tests/contracts/http/runtime_config.json`
- `tests/contracts/http/README.md`
- `tests/contracts/test_http_corpus.py::e2e_pre_daemon_setup`
- `crates/gdaemon/tests/http_contracts.rs::*` — scope-reason: the registered-family list adds a `RuntimeFamily` built on a lazily constructed pool, so the parity precheck sees `runtime`

Manifest and cases:

- The manifest replaces families `config` and `runtime_handshake` with
  `runtime: {parity: native, origin: python}`. `runtime_challenge` stays native with
  origin gdaemon.
- Cases `runtime_handshake`, `config_schema` and `config_values` set
  `"family": "runtime"`.
- Add `config_effective`: `GET /api/config/effective`, credential `operator`.
- Add `runtime_config`: `GET /api/runtime/config`, credential `grant`.

Record both new cases from Python, with all routes proxied:
`GOBBY_RECORD_HTTP_CONTRACTS=1 … pytest tests/contracts/test_http_corpus.py -k record`.
Commit the recorded bytes, with `runtime_config` masking `config_revision` the way
`runtime_handshake` masks its epoch.

`e2e_pre_daemon_setup` also takes `e2e_home_dir` and `e2e_config`, so it runs after
the bootstrap exists. Unless `GOBBY_RECORD_HTTP_CONTRACTS=1` is set, it appends
`routes: {runtime: native}` under `front_door` in `<home>/bootstrap.yaml`. Every case
then replays against the native family on the real e2e database.

In the Rust harness, `parity_violations` takes the registered families as `FAMILIES`
plus a `gobby_runtime::RuntimeFamily::new(Pool::build(<valid unreachable URL>)?, tmp)`.
The harness has no key resolver, so the family never claims there. Its cases replay
through the proxy stub, and the Python harness carries native parity. The README
records that split for database-backed families.

Research context:
- `tests/contracts/http_corpus.py::CREDENTIALS` already includes `grant`, and
  `BoundaryHarness.grant_headers` supplies it.
- The Rust harness's `replay_in_declared_backend_state` sets
  `routes = {family: parity}` and calls `serve`, which composes only `FAMILIES`.
- The e2e bootstrap (`tests/e2e/conftest.py::e2e_config`) writes
  `front_door: enabled: true` with no routes, so every family is proxied today.
- Planned checks:
  - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/ -v`;
  - `cargo nextest run -p gobby-daemon --test http_contracts`.

**Acceptance:**

- 4.1.1 - The manifest declares `runtime` native with origin python, owns the handshake, config and new cases, and no longer lists `config` or `runtime_handshake`. file: `tests/contracts/http/manifest.json`.
- 4.1.2 - `runtime_config` is recorded from Python and committed. file: `tests/contracts/http/runtime_config.json`.
- 4.1.3 - Every corpus case replays equal against the e2e daemon with `runtime: native`. test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.
- 4.1.4 - The Rust parity precheck passes with `runtime` registered. test: `crates/gdaemon/tests/http_contracts.rs::every_native_family_has_corpus_cases`.
- 4.1.5 - `config_effective` is recorded from Python and committed. file: `tests/contracts/http/config_effective.json`.

### 4.2 E2E: compare smoke, backend-kill fallback, flip and rollback [category: test] (depends: 4.1)
`kind: deliverable`

Targets:
- `tests/e2e/test_runtime_family.py`

The new e2e module uses `daemon_instance` and an operator client, authenticated with
the bootstrap API key the way `test_http_corpus._credentials` is. Each phase sets
`front_door.routes.runtime` in the instance's `bootstrap.yaml`, then calls
`stop()` and `restart()`. Each restart runs the same gdaemon command, so no rebuild
occurs.

1. **Compare.**
   1. With `compare` set, run an operator handshake, `GET /api/runtime/config` with
      that grant, and `GET /api/config/effective`. Each response equals Python's.
   2. `<home>/logs/front-door-compare.jsonl` holds `match` records for the GETs and
      a `verified` record for the handshake, and no `diff`, `native_error` or
      `mismatch` records.
   3. `gdaemon compare-report` (`instance.command[0]` with `GOBBY_HOME`) exits 0.
2. **Backend kill.**
   1. With `native` set, SIGKILL the Python backend child
      (`psutil.Process(instance.pid).children()`).
   2. While `/api/health` reports the backend not running, the three runtime routes
      return 200.
   3. `GET /api/config/schema` returns the typed 503.
3. **Rollback.**
   1. Set `proxy` and restart.
   2. The same kill makes the handshake return the typed 503, which proves the route
      is proxied again.

Research context:
- `tests/e2e/conftest.py::DaemonInstance.restart` re-launches the original command
  with isolated state.
- Health is native-capable and reports backend state. A refused or standby backend
  short-circuits to 503 before dispatch (`front_door/mod.rs::FrontDoor::handle`). A
  `Down` or `Starting` backend still reaches native families.
- `psutil` is a project dependency.
- Planned check: `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_runtime_family.py -v`.

**Acceptance:**

- 4.2.1 - Compare mode serves Python's responses and records `match` and `verified` outcomes with zero mismatches, and `compare-report` exits 0. test: `tests/e2e/test_runtime_family.py::test_compare_mode_records_zero_mismatches`.
- 4.2.2 - With `runtime: native`, the runtime routes serve 200 while the backend is killed, and proxied routes return the typed 503. test: `tests/e2e/test_runtime_family.py::test_native_routes_survive_backend_kill`.
- 4.2.3 - Flip and rollback take only a bootstrap edit and a gdaemon restart with the same binaries. test: `tests/e2e/test_runtime_family.py::test_route_flip_and_rollback_need_no_redeploy`.

### 4.3 Docs: family-crate template and the flip runbook [category: docs] (depends: 4.2)
`kind: deliverable`

Targets:
- `crates/AGENTS.md`
- `docs/guides/configuration.md`

In `crates/AGENTS.md`, rewrite "Daemon family crates" to describe the template as
built:

- the generic gcore `RouteFamily<S>` behind `route-family`;
- a family as a process-lifetime value built in `serve::run` on the shared pool;
- `claims` for header-level delegation;
- `check_proxied` for non-idempotent routes;
- the compare-log outcomes;
- the corpus split between the Rust stub replay and the Python native replay.

In `docs/guides/configuration.md`, document `front_door.routes.runtime` and the
operator runbook:

1. Set `compare` and restart gdaemon with an announcement.
2. Soak.
3. Run `gdaemon compare-report --since <start>`.
4. Flip to `native`.
5. Roll back by setting `proxy`.

**Acceptance:**

- 4.3.1 - "Daemon family crates" documents the seam, the claims and proxied-check hooks, the compare log and the corpus split. file: `crates/AGENTS.md`.
- 4.3.2 - The configuration guide documents the `runtime` route values and the compare, soak, flip and rollback runbook with `compare-report`. file: `docs/guides/configuration.md`.

## D1 Managed-principal handshake and grant presentation in Rust (depends: 3.4)
`kind: deferred`

Native code forwards capability-token handshakes (agent_run, tool_chat and
maintenance), the managed challenge and managed-principal grants to Python (D2).
Moving them requires the Rust side to own:

- per-execution bootstrap files;
- rotation;
- `grant.json` rewriting;
- execution revocation, which is in-memory in Python today.

All of these sit in Python's agent lifecycle and move with the S2.7 hub work.

- D1.1: Rust answers the managed challenge and issues capability-token
  (agent_run, tool_chat, maintenance) and managed-principal handshakes, with
  bootstrap files, rotation, `grant.json` rewriting and execution revocation.
- D1.2: Rust serves `GET /api/runtime/config` for managed grants, including
  managed revocation and binding liveness.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Managed issuance depends on the agent lifecycle (bootstrap files, rotation loop, grant.json rewriting, execution revocation) that the S2.7 hub work (#24149) moves; this plan serves interactive principals and forwards managed ones."
  owner: "gobby-1.0 stage 2 (S2.7)"
  original_acceptance_items:
    - D1.1
    - D1.2
```

## D2 Remaining `/api/config` routes in Rust (depends: 3.1)
`kind: deferred`

Only `GET /api/config/effective` is native (D4). The values, schema and write routes
stay proxied under the claimed prefix. Python's `ConfigRuntime` still owns live
activation, subscribers and restart-pending state.

- D2.1: Rust serves the remaining `/api/config` routes (values, schema and writes)
  after config activation leaves Python.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Config writes and the active snapshot are owned by Python's ConfigRuntime; serving them natively needs config activation to leave Python first."
  owner: "gobby-1.0 stage 2"
  original_acceptance_items:
    - D2.1
```

## E1 End-to-end verification
`kind: verification`

#21559 closes when all of the following hold on `0.5.0` with freshly built and
installed binaries:

1. **Crates.** These pass:
   - `cargo nextest run -p gobby-core -E 'test(presentation)'`;
   - `cargo nextest run -p gobby-runtime`;
   - `cargo nextest run -p gobby-daemon`, including `compare`, `http_contracts`,
     `lease` and `nodes`.
2. **Corpus and e2e.** `GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/ tests/e2e/test_runtime_family.py tests/storage/test_secret_seal_vector.py tests/runtime_grants/test_capability_vectors.py`
   passes against the isolated test hub.
3. **Soak (exit criterion 1).**
   1. On the operator's daemon, announce and restart with
      `front_door.routes.runtime: compare`.
   2. Run ordinary `gcode` and agent traffic over a representative window: at least
      24 hours, at least one config read and one interactive handshake per active
      project, and no pending config activation.
   3. Then `gdaemon compare-report --since <window start>` exits 0, with nonzero
      `match` and `verified` counts.
4. **Backend kill (exit criterion 2).**
   1. With `runtime: native` live, kill the Python backend.
   2. `gcode` still handshakes and reads effective config until the supervisor
      restarts the backend.
5. **Flip and rollback (exit criterion 3).** `runtime: native` and then
   `runtime: proxy`, each with only a gdaemon restart, behave as 4.2 shows, with no
   cutover in between.
6. **Repo gates.**
   - `uv run ruff format src/ && uv run ruff check src/ && uv run mypy src/` pass.
   - `cargo fmt --check` passes, and `cargo clippy` passes for `gobby-core`
     (with `route-family`), `gobby-runtime` and `gobby-daemon`.
   - Every hand-maintained production file stays under 1,000 lines.

## V1 Plan Changelog
`kind: framing`

- 2026-10-13: First draft by Plan Writer #16764. Decisions D1–D4 were ruled by
  Orchestrator #15977 on 2026-10-13.
- 2026-10-13: Consensus between Plan Writer #16764 and Plan Adversary #16787 on
  the revision at 3cc65d0. The adversary's six findings were accepted and repaired:
  `check_proxied` receives the buffered request bytes (PA-1); the presentation
  module uses the directory form (PA-2); 3.1's claims test covers only
  `/api/config` paths and unidentified callers, and 3.2 and 3.4 extend `claims`
  (PA-3); the desired snapshot keeps `_read_coherent`'s empty-store and
  missing-state rules, with new item 3.1.5 (PA-4); both deferrals use
  `TBD-at-expansion` ledgers with their own items D1.1, D1.2 and D2.1 (PA-5); and
  4.1.5 names `config_effective.json` (PA-6). No enhancer edits were received
  before consensus.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: '`RouteFamily` seam in gcore with per-request claims'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: gcore exposes `RouteFamily<S>` and `ProxiedCheck` behind
    `route-family`, with defaults `claims = true` and `check_proxied = Skipped`, and
    `cargo tree -p gobby-hooks -i axum` is empty. file: `crates/gcore/src/route_family.rs`.

    1.1.2: Under a `Native` or `Compare` backend, a request the family does not claim
    reaches the proxy unchanged and never the family router. test: `crates/gdaemon/src/front_door/routes.rs::unclaimed_requests_are_proxied_under_every_backend`.

    1.1.3: The existing routing behaviors still hold. test: `crates/gdaemon/src/front_door/routes.rs::health_family_honors_proxy_native_and_compare`.

    1.1.4: The corpus parity precheck compiles against the generic trait and passes.
    test: `crates/gdaemon/tests/http_contracts.rs::every_native_family_has_corpus_cases`.'
  labels:
  - covers:gdaemon-runtime-family:1.1:1.1.1
  - covers:gdaemon-runtime-family:1.1:1.1.2
  - covers:gdaemon-runtime-family:1.1:1.1.3
  - covers:gdaemon-runtime-family:1.1:1.1.4
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: One gdaemon pool for key resolution, the lease and hub node routes
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: `serve::run` builds exactly one pool, used by the resolver,
    the lease and hub node routes, and neither `auth.rs` nor `lease/mod.rs` calls
    `Pool::build`. file: `crates/gdaemon/src/serve.rs`.

    1.2.2: The lease still holds a dedicated advisory-lock session that drop and failed
    transactions release. test: `crates/gdaemon/tests/lease.rs::failed_transaction_and_drop_release_the_dedicated_session`.

    1.2.3: Epoch and signing secret still rotate together on acquisition. test: `crates/gdaemon/tests/lease.rs::epoch_and_secret_rotate_together`.

    1.2.4: A resolver on an unreachable pool fails closed. test: `crates/gdaemon/tests/front_door.rs::postgres_resolver_and_resolverless_door_fail_closed`.'
  labels:
  - covers:gdaemon-runtime-family:1.2:1.2.1
  - covers:gdaemon-runtime-family:1.2:1.2.2
  - covers:gdaemon-runtime-family:1.2:1.2.3
  - covers:gdaemon-runtime-family:1.2:1.2.4
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Compare policy, compare log and `gdaemon compare-report`
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '2.1.1: A claimed GET in Compare mode returns the proxied response
    and records `match`, or `diff` with a pointer and no values. test: `crates/gdaemon/tests/compare.rs::get_compare_records_match_and_diff_without_values`.

    2.1.2: JSON bodies that differ only in key order record `match`. test: `crates/gdaemon/tests/compare.rs::json_bodies_compare_by_value`.

    2.1.3: A claimed POST in Compare mode never reaches the native router, the backend
    and `check_proxied` both receive the original request bytes, and the family check''s
    outcome is recorded. test: `crates/gdaemon/tests/compare.rs::non_get_requests_run_only_the_proxied_leg`.

    2.1.4: A failing native leg records `native_error` and the caller still receives
    the proxied response. test: `crates/gdaemon/tests/compare.rs::native_failure_returns_the_proxied_response`.

    2.1.5: The report counts per family within `--since` and fails on any diff, native_error
    or mismatch, and on an empty window. test: `crates/gdaemon/tests/compare.rs::report_fails_on_mismatch_and_empty_window`.'
  labels:
  - covers:gdaemon-runtime-family:2.1:2.1.1
  - covers:gdaemon-runtime-family:2.1:2.1.2
  - covers:gdaemon-runtime-family:2.1:2.1.3
  - covers:gdaemon-runtime-family:2.1:2.1.4
  - covers:gdaemon-runtime-family:2.1:2.1.5
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: '`gobby-runtime` crate and native `GET /api/config/effective`'
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: "3.1.1: `crates/gruntime` builds as workspace member `gobby-runtime`\
    \ (publish false, workspace version) and gdaemon links it. file: `crates/gruntime/Cargo.toml`.\n\
    3.1.2: `GET /api/config/effective` with an API-key identity returns Python's projection\
    \ of the desired configuration with `no-store`. test: `crates/gruntime/tests/config.rs::effective_config_matches_python_projection`.\n\
    3.1.3: Under `/api/config` the family claims only identified `GET /api/config/effective`;\
    \ every other `/api/config` path or method, and an unidentified caller on any\
    \ path, is forwarded. test: `crates/gruntime/tests/config.rs::only_identified_effective_reads_are_claimed`.\n\
    3.1.5: With an empty `config_store` the snapshot is the revision plus contract\
    \ defaults, and a missing `config_state` row returns 500. test: `crates/gruntime/tests/config.rs::empty_store_serves_defaults_and_missing_state_fails`.\n\
    3.1.4: Values that still hold `$secret:` or `${\u2026}` after expansion are omitted.\
    \ test: `crates/gruntime/tests/config.rs::unresolved_values_are_omitted`."
  labels:
  - covers:gdaemon-runtime-family:3.1:3.1.1
  - covers:gdaemon-runtime-family:3.1:3.1.2
  - covers:gdaemon-runtime-family:3.1:3.1.3
  - covers:gdaemon-runtime-family:3.1:3.1.5
  - covers:gdaemon-runtime-family:3.1:3.1.4
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Grant signing and presentation in gcore; native `GET /api/runtime/config`
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: `sign_grant` reproduces a Python-signed grant''s checksum
    and signature byte for byte, and `signature_matches` accepts it. test: `crates/gcore/src/grant/presentation/tests.rs::signature_matches_python_vector`.

    3.2.2: Each rejection code maps to its status and `{code, error, message}` body.
    test: `crates/gcore/src/grant/presentation/tests.rs::rejection_matrix_maps_codes_to_statuses`.

    3.2.3: `verify_static` reports the first failing check in Python''s order. test:
    `crates/gcore/src/grant/presentation/tests.rs::checks_run_in_python_order`.

    3.2.4: `GET /api/runtime/config` with a valid interactive grant returns `{config_revision,
    settings}`, a stale epoch returns 409, and a revoked generation returns 403 `revoked`.
    test: `crates/gruntime/tests/grant.rs::runtime_config_presents_interactive_grants`.

    3.2.5: Managed-principal grants, undecodable headers and missing headers are not
    claimed. test: `crates/gruntime/tests/grant.rs::managed_and_malformed_grants_are_not_claimed`.'
  labels:
  - covers:gdaemon-runtime-family:3.2:3.2.1
  - covers:gdaemon-runtime-family:3.2:3.2.2
  - covers:gdaemon-runtime-family:3.2:3.2.3
  - covers:gdaemon-runtime-family:3.2:3.2.4
  - covers:gdaemon-runtime-family:3.2:3.2.5
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: 'Secret-store port: envelope, secret reads and credential sealing'
  category: code
  task_type: feature
  depends_on:
  - '3.2'
  validation_criteria: '3.3.1: Rust opens the Python-sealed token and decrypts the
    Python-encrypted secret from the committed vector. test: `crates/gruntime/tests/secrets.rs::opens_python_sealed_vector`.

    3.3.2: Python opens the Rust-sealed token from the committed vector. test: `tests/storage/test_secret_seal_vector.py::test_python_opens_rust_sealed_vector`.

    3.3.3: DEK unwrap works for the key-file and scrypt postures against Python-written
    rows. test: `crates/gruntime/tests/secrets.rs::unwraps_both_postures`.

    3.3.4: Two concurrent key-file initializations converge on one DEK and a 0600
    KEK file. test: `crates/gruntime/tests/secrets.rs::concurrent_envelope_initialization_converges`.

    3.3.5: A wrong AAD or a truncated token fails to open. test: `crates/gruntime/tests/secrets.rs::open_rejects_wrong_aad_and_truncation`.'
  labels:
  - covers:gdaemon-runtime-family:3.3:3.3.1
  - covers:gdaemon-runtime-family:3.3:3.3.2
  - covers:gdaemon-runtime-family:3.3:3.3.3
  - covers:gdaemon-runtime-family:3.3:3.3.4
  - covers:gdaemon-runtime-family:3.3:3.3.5
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Native interactive handshake and grant capabilities
  category: code
  task_type: feature
  depends_on:
  - '3.3'
  validation_criteria: '3.4.1: An identified operator handshake returns a signed interactive
    grant whose payload fields equal Python''s for the same database state. test:
    `crates/gruntime/tests/handshake.rs::interactive_handshake_issues_signed_grant`.

    3.4.2: A repeat handshake reuses the principal and returns the stored password,
    and a fresh issuance stores material that Python opens. test: `crates/gruntime/tests/handshake.rs::reused_principal_returns_the_sealed_password`.

    3.4.3: A machine mismatch, an unadmitted project and a bad overlay return 403
    `claims_mismatch`, and other issuance failures return 403 `credential_issuance_failed`.
    test: `crates/gruntime/tests/handshake.rs::handshake_rejections_match_python_codes`.

    3.4.4: Agent-bearer, cookie-only and unauthenticated handshakes and the managed
    challenge are not claimed and reach Python unchanged. test: `crates/gruntime/tests/handshake.rs::non_operator_handshakes_are_not_claimed`.

    3.4.5: The Rust capability port matches every Python-generated vector. test: `crates/gruntime/tests/capabilities.rs::capabilities_match_python_vectors`.

    3.4.6: The committed vectors still match Python''s `capabilities_from_snapshot`.
    test: `tests/runtime_grants/test_capability_vectors.py::test_vectors_match_python_capabilities`.'
  labels:
  - covers:gdaemon-runtime-family:3.4:3.4.1
  - covers:gdaemon-runtime-family:3.4:3.4.2
  - covers:gdaemon-runtime-family:3.4:3.4.3
  - covers:gdaemon-runtime-family:3.4:3.4.4
  - covers:gdaemon-runtime-family:3.4:3.4.5
  - covers:gdaemon-runtime-family:3.4:3.4.6
  tdd: true
  source_section: '3.4'
  implementation_domain: backend
- title: Compare check for proxied handshake grants
  category: code
  task_type: feature
  depends_on:
  - '3.4'
  validation_criteria: '3.5.1: A Python-issued grant for the request records `verified`.
    test: `crates/gruntime/tests/verify.rs::python_issued_grant_verifies`.

    3.5.2: A tampered capability, a wrong epoch, a bad signature or a principal mismatch
    records `mismatch` with the field pointer and no values. test: `crates/gruntime/tests/verify.rs::mismatches_name_the_field_without_values`.'
  labels:
  - covers:gdaemon-runtime-family:3.5:3.5.1
  - covers:gdaemon-runtime-family:3.5:3.5.2
  tdd: true
  source_section: '3.5'
  implementation_domain: backend
- title: 'Corpus: `runtime` family cases replayed natively'
  category: test
  task_type: feature
  depends_on:
  - '3.5'
  validation_criteria: '4.1.1: The manifest declares `runtime` native with origin
    python, owns the handshake, config and new cases, and no longer lists `config`
    or `runtime_handshake`. file: `tests/contracts/http/manifest.json`.

    4.1.2: `runtime_config` is recorded from Python and committed. file: `tests/contracts/http/runtime_config.json`.

    4.1.3: Every corpus case replays equal against the e2e daemon with `runtime: native`.
    test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.

    4.1.4: The Rust parity precheck passes with `runtime` registered. test: `crates/gdaemon/tests/http_contracts.rs::every_native_family_has_corpus_cases`.

    4.1.5: `config_effective` is recorded from Python and committed. file: `tests/contracts/http/config_effective.json`.'
  labels:
  - covers:gdaemon-runtime-family:4.1:4.1.1
  - covers:gdaemon-runtime-family:4.1:4.1.2
  - covers:gdaemon-runtime-family:4.1:4.1.3
  - covers:gdaemon-runtime-family:4.1:4.1.4
  - covers:gdaemon-runtime-family:4.1:4.1.5
  tdd: false
  source_section: '4.1'
  assigned_agent: developer
- title: 'E2E: compare smoke, backend-kill fallback, flip and rollback'
  category: test
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: Compare mode serves Python''s responses and records
    `match` and `verified` outcomes with zero mismatches, and `compare-report` exits
    0. test: `tests/e2e/test_runtime_family.py::test_compare_mode_records_zero_mismatches`.

    4.2.2: With `runtime: native`, the runtime routes serve 200 while the backend
    is killed, and proxied routes return the typed 503. test: `tests/e2e/test_runtime_family.py::test_native_routes_survive_backend_kill`.

    4.2.3: Flip and rollback take only a bootstrap edit and a gdaemon restart with
    the same binaries. test: `tests/e2e/test_runtime_family.py::test_route_flip_and_rollback_need_no_redeploy`.'
  labels:
  - covers:gdaemon-runtime-family:4.2:4.2.1
  - covers:gdaemon-runtime-family:4.2:4.2.2
  - covers:gdaemon-runtime-family:4.2:4.2.3
  tdd: false
  source_section: '4.2'
  assigned_agent: developer
- title: 'Docs: family-crate template and the flip runbook'
  category: docs
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: '4.3.1: "Daemon family crates" documents the seam, the claims
    and proxied-check hooks, the compare log and the corpus split. file: `crates/AGENTS.md`.

    4.3.2: The configuration guide documents the `runtime` route values and the compare,
    soak, flip and rollback runbook with `compare-report`. file: `docs/guides/configuration.md`.'
  labels:
  - covers:gdaemon-runtime-family:4.3:4.3.1
  - covers:gdaemon-runtime-family:4.3:4.3.2
  tdd: false
  source_section: '4.3'
  assigned_agent: tech-writer
```
