Plan artifact: `.gobby/plans/gdaemon-api-keys-nodes.md`

# Gobby 1.0 Stage 1 slice: API keys and node registration

**Plan ID:** gdaemon-api-keys-nodes

## Overview
`kind: framing`

This is the P4 slice (S1.4) of the Stage 1 front-door plan of record,
`.gobby/plans/gdaemon-front-door.md`. The root is the existing phase epic #21555
(API keys and node registration) under #21543. It follows the #23098 slice
pattern (`gdaemon-run-modes.md`, `gdaemon-http-contract-corpus.md`): the slice is
reviewed, approved, and expanded directly under #21555, and the single P4 phase
expands its deliverables directly under that epic. Expanding the whole plan of
record would mint duplicate phase epics and stamp the unreviewed P5 section.

When the executable leaves close:
- A node bootstrap can no longer start the Python daemon, lease, or promote.
- The front door serves TLS to remote peers, and local clients keep dialing
  loopback in plaintext.
- `api_keys` exists, keys are issued through the password bootstrap route, and
  every standalone or hub install holds its own machine key in bootstrap.
- `gobby auth login` enrolls a remote bootstrap against a hub over a pinned
  self-signed certificate.

Two sections of the plan of record wait on other plans and are deferred here
(Decision 1): the shared-token cutover (plan of record 4.3, D1 here) and the
node channel and relay (plan of record 4.4, D2 here). Their designs are settled
below so the planning pass that opens them starts from decisions, not questions.

## Decision Record
`kind: framing`

1. **External prerequisites are deferred sections.** `docs/contracts/plan-coverage.md`
   §Deferrals requires work gated on another plan to be a `kind: deferred`
   section (PD disposition, 2026-09-29).
   - The shared-token cutover re-records the auth corpus cases, so it waits on
     the corpus slice's 3.1 (`.gobby/plans/gdaemon-http-contract-corpus.md`,
     root #21552). It becomes D1.
   - The node relay's hub needs the Python `hub` flag and the `(remote, true)`
     rejection from the run-modes slice's 2.1 (`.gobby/plans/gdaemon-run-modes.md`,
     root #21553), and it authenticates the channel with the key validation
     from D1. It becomes D2.
   - The four executable leaves (4.6, 4.1, 4.2, 4.5) have no external wait.
   - The enhancer and the Adversary are asked to test whether either deferral can
     be removed by local derivation (the run-modes slice's either-order corpus
     pattern is the candidate for D1). None is removed without that review.
2. **A node never runs the Python daemon (PD disposition, 2026-09-29).**
   - Today `run_gobby` (`src/gobby/runner.py`) takes `ActiveDaemonLease` for
     every bootstrap. The lease is keyed on the database, and a node's
     `database_url` is the hub's, so a node daemon parks in
     `serve_standby_until_promotion`, where an operator promote or recover would
     make the node the hub's active daemon.
   - 4.6 refuses a `datastore_mode: remote` bootstrap in code before the lease
     is constructed, and refuses `gobby start`, `gobby restart`, and
     `gobby cutover` before any process is launched. Documentation alone is
     insufficient.
   - The guard reads `datastore_mode == "remote"` directly, with no run-modes
     dependency: before run-modes 2.1 lands every remote bootstrap is a node, and
     after it lands `(remote, true)` is rejected at parse, so the check is exact
     in both orders.
   - The node's front door is a bare `gdaemon serve` (D2); P5's 5.2 records that a
     node gdaemon never spawns or supervises a Python backend (#21554 (Singleton
     lease and backend lifecycle in Rust), not this slice).
3. **One public port serves TLS to remote peers and plaintext to loopback peers
   (PD decision, 2026-09-29).**
   - `tls.mode: off` stays refused on a non-loopback `bind_host`, but every local
     client (ghook, gcode, gclient, gterm through `gobby_core::daemon_url`, and
     Python through `gobby.utils.daemon_url`) dials plain `http://`. Making each
     of them speak pinned TLS was rejected (about 20 client sites); a second
     TLS-only port was rejected (it breaks the fixed `:60887`/`:60888` decision).
   - gdaemon peeks the first byte of every accepted connection. `0x16` (a TLS
     ClientHello) is served over TLS whatever the peer. Any other first byte is
     served in plaintext only when the peer address is loopback
     (`ip.to_canonical().is_loopback()`, so IPv4-mapped IPv6 counts); otherwise
     the connection is closed.
   - **Local-endpoint contract.** Local clients always dial loopback: the dial
     host derived from `bind_host` is `127.0.0.1` for every IPv4 or named host
     and `[::1]` for an IPv6 literal. An explicit `daemon_url` still wins.
   - **Loopback reachability.** A wildcard bind (`0.0.0.0`, `::`) already accepts
     loopback. A concrete non-loopback bind adds a companion listener on the
     same port at the loopback address of the same family, so the
     local-endpoint contract always has a listener.
4. **The pinned client lives in gdaemon.** Decision 17 of the plan of record has
   crates dial loopback only, so gdaemon's relay (D2) is the only Rust hub
   dialer. The pinned `rustls::ClientConfig` builder and the fingerprint helper
   live in `crates/gdaemon/src/front_door/tls.rs`, and gcore gains no `tls.rs`.
   Python needs no helper: `ssl.create_default_context(cafile=<pem>)` loads only
   that certificate, so 4.5 and the e2e fixture use the standard library.
5. **Rust bootstrap fields arrive with their first Rust consumer.** 4.2 adds
   `api_key`, `api_key_id`, and `hub_cert` to the Python parser and writer only.
   `HubDatabaseBootstrap` gains `api_key` in D1 (the native interactive
   challenge) and `hub_cert`, the parsed `hub`, and the parsed mode in D2 (the
   relay), mirroring the run-modes slice's Decision 2. The Rust key-format
   helper moves to D1 with its first caller (`front_door/auth.rs`).
6. **Key-authenticated CLI moves with key validation.** `gobby auth key
   --rotate`, `list`, and `revoke` call routes that authenticate the caller's
   key, which only D1 makes possible on a node. 4.5 keeps `gobby auth login`
   (public password route) and `gobby auth key --show` (local bootstrap read);
   the rest joins D1.
7. **Migration number.** The highest migration on `0.5.0` is
   `455_drop_ask_artifacts.sql`, so 4.2 adds `456_add_api_keys.sql`. If another
   migration takes 456 first, the leaf takes the next free number, updates every
   carrier below, and names it in its close summary. Never a baseline edit.
8. **Obsolete #23058 (Plan-of-record P4 refresh) is not landed.** Its commit
   3792fa4 is not rebased or cherry-picked. Its unique obligations are carried
   here: the next free migration number, `crates/gcore/src/schema/assets.rs::MIGRATIONS`
   registration, the five signed goldens under `tests/runtime_grants/golden/`,
   and dropping the retired Ask test targets from the cutover sweep.

## Constraints
`kind: framing`

- **P1 has landed.** `gdaemon serve` (`crates/gdaemon/src/serve.rs`) proxies to
  Python on `127.0.0.1:<port + 100>`; the runner owns the front door through
  `src/gobby/runner_front_door.py::FrontDoorChild`, which spawns gdaemon with
  `GOBBY_PARENT_FD`; `watch_parent_fd` returns `None` when that variable is unset,
  so a bare `gdaemon serve` runs without a Python parent.
- **Key format is locked** for the hosted version: `gobby_` + 43 base62 + 6-char
  CRC32, SHA-256 at rest.
- **No backward compatibility.** 0.5.0 has not shipped. A bootstrap with the new
  fields absent parses with them absent.
- **Running daemon.** No executable leaf needs a coordinated restart. 4.2's
  startup adoption mints the local key on the next start the PD performs; D1 is
  the flag day and carries its own restart.
- **Test isolation.** pytest runs use
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  and never the full suite. Every command runs from the worktree root.
- **Rust conventions.** Load the `rust` skill before editing crates;
  `crates/CLAUDE.md` governs. Production line counts stop at `#[cfg(test)]`:
  `crates/gcore/src/bootstrap.rs` is 471 production lines, so no split is needed.

## P4: API keys and node registration (S1.4, #21555)
`kind: framing`

**Goal**: a node cannot become the hub's daemon, the front door speaks pinned
TLS to remote peers, keys are issued and bound to machines, and a remote
bootstrap enrolls against a hub.

### 4.6 Node bootstraps refuse the Python daemon [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/bootstrap.py::*` — scope-reason: adds the module function `node_daemon_refusal(bootstrap)`; no existing symbol changes
- `src/gobby/runner.py::run_gobby`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `src/gobby/cli/daemon_start.py::_start_dependency_errors`
- `src/gobby/cli/daemon_preflight.py::restart_start_refusal`
- `tests/test_runner_lease_lifecycle.py::*` — scope-reason: gains the node-refusal case for `run_gobby`
- `tests/cli/test_daemon_remote_mode.py::*` — scope-reason: `test_start_skips_services_in_remote_mode` and `test_restart_skips_services_in_remote_mode` become refusal tests; `test_compose_runtime_rejects_remote_mode` is unchanged
- `tests/cli/test_daemon_preflight.py::*` — scope-reason: gains the node-refusal case for `restart_start_refusal`
- `docs/guides/shared-stack.md`

**Research context:**

Seam (Decision 2). `run_gobby` (`src/gobby/runner.py`, lines 377-522) calls
`load_bootstrap(..., resolve_database_url=True)` (about line 432), checks
`database_url`, then constructs `ActiveDaemonLease` (about line 440). After the
lease it starts `FrontDoorChild`, calls `lease.try_acquire`, and on failure
builds `StandbyLeaseControl` and awaits `serve_standby_until_promotion`. The
standby control is the only place the promote and recover routes
(`src/gobby/daemon_lease_control.py`, `/api/admin/lease/promote|recover`) are
served before activation, and the active router's copies
(`src/gobby/servers/routes/admin/_lease.py`) exist only in an active runner.
Refusing between `load_bootstrap` and `ActiveDaemonLease` therefore covers
foreground start, service launch, `try_acquire`, and standby promote and
recover in one place.

Mechanism:
- Add `node_daemon_refusal(bootstrap: BootstrapConfig) -> str | None` to
  `src/gobby/config/bootstrap.py`. It returns
  `"datastore_mode: remote is a node; a node runs no Python daemon and never takes the hub lease. See docs/guides/shared-stack.md (Client setup)."`
  when `bootstrap.datastore_mode == "remote"` and `None` otherwise.
- `run_gobby` raises `RuntimeError(refusal)` right after `load_bootstrap`,
  before the lease, the front door, or schema verification.
- `_start_dependency_errors` (`src/gobby/cli/daemon_start.py`) already loads the
  bootstrap and returns a list of errors that `start` prints before launching.
  It returns `[refusal]` for a node, so `gobby start` refuses before any process
  is spawned and before the managed-services check.
- `restart_start_refusal` (`src/gobby/cli/daemon_preflight.py`) is the start-half
  proof shared by `gobby restart` (`src/gobby/cli/daemon.py`) and
  `gobby cutover` (`src/gobby/cli/cutover.py`). It loads the bootstrap first and
  returns the refusal, so both commands refuse with the running daemon
  untouched. Its signature is unchanged.

Shared-stack guide: Client setup today ends with `gobby start` on the client,
which parks in standby against the hub's lease. It changes to: a client runs no
Python daemon; `gobby start` refuses with the message above; the node's front
door arrives with the node relay (D2). The rest of Client setup (bootstrap,
installer) is unchanged in this leaf; D1 replaces the token copy.

Consumers unchanged:
- `src/gobby/cli/daemon.py` — no-edit-reason: calls `restart_start_refusal(ctx)` with an unchanged signature and already prints a returned refusal.
- `src/gobby/cli/cutover.py` — no-edit-reason: same.
- `tests/cli/test_cutover.py` — no-edit-reason: its bootstraps are local; verification only.
- `tests/cli/test_cli_daemon.py` — no-edit-reason: patches or drives `_start_dependency_errors` with local bootstraps; the function keeps its signature; verification only.
- `tests/providers/test_version_gate.py` — no-edit-reason: drives `run_gobby` with a local bootstrap; verification only.
- `tests/test_runner_env_scrub.py` — no-edit-reason: same.
- `tests/test_runner_lifecycle.py` — no-edit-reason: same.
- `tests/test_runner_pid_file.py` — no-edit-reason: same, and patches `_start_dependency_errors` by name.

Verification planned:
`uv run python scripts/generate_runtime_config_contract.py`, then
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_runner_lease_lifecycle.py tests/cli/test_daemon_remote_mode.py tests/cli/test_daemon_preflight.py tests/cli/test_cutover.py tests/config/test_bootstrap.py tests/config/test_runtime_config_contract.py -v`,
then `uv run ruff check` and `uv run mypy` on the changed files.

**Acceptance:**

- 4.6.1 - `run_gobby` with a `datastore_mode: remote` bootstrap raises the refusal before constructing `ActiveDaemonLease`, starting the front door, or verifying the schema. test: `tests/test_runner_lease_lifecycle.py::test_node_bootstrap_refuses_before_lease`.
- 4.6.2 - `gobby start` on a node bootstrap prints the refusal and launches no process. test: `tests/cli/test_daemon_remote_mode.py::test_start_refuses_node_bootstrap`.
- 4.6.3 - `restart_start_refusal` returns the refusal for a node bootstrap, so `gobby restart` and `gobby cutover` refuse with the running daemon untouched. test: `tests/cli/test_daemon_preflight.py::test_restart_refuses_node_bootstrap`.
- 4.6.4 - `standalone` and local bootstraps start as today. test: `tests/test_runner_lease_lifecycle.py::test_local_bootstrap_still_takes_lease`.
- 4.6.5 - The shared-stack guide's Client setup no longer starts a client daemon and names the refusal. file: `docs/guides/shared-stack.md`.

### 4.1 Front-door TLS for remote peers with loopback plaintext [category: code] (depends: 4.6)
`kind: deliverable`

Targets:
- `src/gobby/config/bootstrap.py::*` — scope-reason: `FrontDoorConfig` gains `tls` (`mode`, `cert`, `key`) and `bootstrap_from_mapping` enforces the loopback default and the non-loopback refusal; `tls.mode` defaults to `off` so constructor sites need no edit
- `crates/gcore/src/bootstrap.rs::FrontDoorBootstrap`
- `crates/gcore/src/bootstrap.rs::parse_front_door`
- `crates/gcore/src/bootstrap.rs::FRONT_DOOR_KEYS`
- `crates/gcore/src/daemon_url.rs::dial_host`
- `src/gobby/utils/daemon_url.py::normalize_dial_host`
- `src/gobby/runner.py::_healthy_daemon_running`
- `crates/gdaemon/Cargo.toml`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `bind` adds the companion loopback listener, `accept` adds the first-byte peek and loopback gate, and `run` loads or generates the certificate
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: registers the new `tls` module; no existing symbol changes
- `crates/gdaemon/src/front_door/tls.rs`
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: gains the TLS, loopback-gate, and companion-listener tests
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `tests/config/test_bootstrap.py::*` — scope-reason: the TLS block's default, refusal, and loopback-host cases
- `tests/utils/test_daemon_url.py::*` — scope-reason: concrete-host cases now dial loopback
- `tests/e2e/conftest.py::daemon_instance`
- `tests/e2e/conftest.py::DaemonInstance`
- `tests/e2e/test_daemon_lifecycle.py::*` — scope-reason: gains `test_daemon_serves_over_self_signed_tls`
- `docs/guides/configuration.md`

**Research context:**

Bootstrap block, parsed identically by `crates/gcore/src/bootstrap.rs::parse_front_door`
(which validates keys against `FRONT_DOOR_KEYS`, today `["enabled", "routes"]`)
and `src/gobby/config/bootstrap.py` (`FrontDoorConfig`, parsed in
`bootstrap_from_mapping`):

```yaml
front_door:
  tls:
    mode: off | self-signed | files    # default off on a loopback bind_host; off refused otherwise
    cert: ~/.gobby/tls/front_door.crt  # files mode
    key: ~/.gobby/tls/front_door.key
```

`bind_host` is loopback when it is `localhost` (the installer default,
`src/gobby/cli/install_setup.py`) or parses as a loopback IP. A non-loopback
bind with `mode` absent or `off` is a parse error naming `self-signed` and
`files`. `FrontDoorBootstrap` gains `tls: TlsBootstrap { mode, cert, key }`.

gdaemon (`crates/gdaemon/src/front_door/tls.rs`, registered in
`front_door/mod.rs`; `rcgen` and `tokio-rustls` are new in
`crates/gdaemon/Cargo.toml`, which today has `hyper` and `hyper-util` but no TLS
crate):
- `self-signed`: on first `serve` with `~/.gobby/tls/` empty, generate an ECDSA
  P-256 key pair and a ten-year certificate, files 0600, SANs `localhost`, the
  hostname, and every non-loopback address bound at generation. Print
  `front door certificate sha256:<fingerprint>` at every start.
- `files`: load the operator PEM pair (covers `tailscale cert`).
- Acceptor (Decision 3): `serve.rs::accept` peeks one byte of each accepted
  stream. `0x16` wraps the stream in the `tokio-rustls` acceptor; any other byte
  is served in plaintext when `peer.ip().to_canonical().is_loopback()`, and
  closed otherwise. With `mode: off` there is no acceptor and the P1 path is
  unchanged. The 1.2 WS splice runs inside either stream unchanged.
- Companion listener (Decision 3): `serve::bind` today binds `bind_host` once per
  public port. When `bind_host` is a concrete non-loopback address, it also
  binds the same port on `127.0.0.1` (IPv4) or `::1` (IPv6). A wildcard bind
  gets none.
- `pinned_client_config(pem: &[u8]) -> rustls::ClientConfig` with a root store
  holding exactly that certificate and no system roots, and
  `fingerprint(pem) -> String`. Its production caller is the D2 relay; in this
  leaf the TLS tests use it.

Local-endpoint contract (Decision 3): `crates/gcore/src/daemon_url.rs::dial_host`
passes explicit hosts through today (its doc comment says "explicit IPv4
literals" pass unchanged). It changes to return `127.0.0.1` for every IPv4 or
named host and `[::1]` for an IPv6 literal; `endpoint_to_url` keeps preferring
an explicit `daemon_url`. `src/gobby/utils/daemon_url.py::normalize_dial_host`
gets the same mapping, which also covers `DaemonClient.__init__`
(`src/gobby/utils/daemon_client.py`, `f"http://{normalize_dial_host(host)}:{port}"`).
`src/gobby/runner.py::_healthy_daemon_running` builds its own
`http://{host}:{port}/api/health` from `bootstrap.bind_host` with a private
wildcard map; it calls `normalize_dial_host` instead.

e2e: `daemon_instance` gains `tls="self-signed"` on loopback (permitted for
tests); `DaemonInstance.http_url`/`ws_url` return `https`/`wss` for it and expose
`cert_path`. Python clients in the test pin with
`ssl.create_default_context(cafile=cert_path)` (Decision 4).

Consumers unchanged:
- `crates/ghook/src/diagnostics.rs` — no-edit-reason: reports `endpoint.host` for display only.
- `crates/ghook/src/diagnose.rs` — no-edit-reason: same.
- `src/gobby/hooks/hook_manager.py` — no-edit-reason: `HookManager` is built with `daemon_host="localhost"` (`src/gobby/servers/_app_lifecycle.py`), already loopback.
- `src/gobby/runner_front_door.py` — no-edit-reason: `FrontDoorChild` readiness probes are TCP connects, which the first-byte peek does not affect.
- `src/gobby/utils/daemon_client.py` — no-edit-reason: `DaemonClient.__init__` calls `normalize_dial_host` with an unchanged signature and inherits the loopback mapping.
- `src/gobby/runner_lifecycle.py` — no-edit-reason: calls `_healthy_daemon_running(port, backend.host)`, whose host is already `127.0.0.1`; signature unchanged.
- `tests/test_runner_env_scrub.py` — no-edit-reason: patches `gobby.runner._healthy_daemon_running` by name; verification only.
- `tests/test_runner_lifecycle.py` — no-edit-reason: same.
- `tests/test_runner_pid_file.py` — no-edit-reason: same.
- `tests/e2e/test_autonomous_mode.py` — no-edit-reason: uses `daemon_instance` with `tls` defaulting to off; URLs and behavior unchanged.
- `tests/e2e/test_daemon_auth.py` — no-edit-reason: same.
- `tests/e2e/test_e2e_smoke.py` — no-edit-reason: same.
- `tests/e2e/test_external_terminal_attach.py` — no-edit-reason: same.
- `tests/e2e/test_full_workflow.py` — no-edit-reason: same.
- `tests/e2e/test_grok_session_deferral.py` — no-edit-reason: same.
- `tests/e2e/test_inter_agent_messages.py` — no-edit-reason: same.
- `tests/e2e/test_mcp_proxy_e2e.py` — no-edit-reason: same.
- `tests/e2e/test_parallel_clones.py` — no-edit-reason: same.
- `tests/e2e/test_plan_coverage_responsiveness.py` — no-edit-reason: same.
- `tests/e2e/test_restart_responsiveness.py` — no-edit-reason: same.
- `tests/e2e/test_review_learning_e2e.py` — no-edit-reason: same.
- `tests/e2e/test_runtime_boundary.py` — no-edit-reason: same.
- `tests/e2e/test_sequential_review_loop.py` — no-edit-reason: same.
- `tests/e2e/test_session_tracking.py` — no-edit-reason: same.
- `tests/e2e/test_single_active_daemon.py` — no-edit-reason: same.
- `tests/e2e/test_stateless_ambient_session.py` — no-edit-reason: same.
- `tests/e2e/test_task_close_checklist_e2e.py` — no-edit-reason: same.
- `tests/e2e/test_terminal_client_stack.py` — no-edit-reason: same.
- `tests/e2e/test_usage_reporting.py` — no-edit-reason: same.
- `tests/e2e/test_worktree_merge_live.py` — no-edit-reason: same.
- `tests/e2e/test_worktrees_e2e.py` — no-edit-reason: same.
- `tests/mcp_proxy/test_annotate_mcp.py` — no-edit-reason: same.
- `tests/terminals/test_runtime_contract.py` — no-edit-reason: same.

Verification planned:
`cargo test -p gobby-daemon --test front_door`,
`cargo test -p gobby-core daemon_url bootstrap`,
`uv run python scripts/generate_runtime_config_contract.py`,
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_bootstrap.py tests/utils/test_daemon_url.py tests/config/test_runtime_config_contract.py tests/e2e/test_daemon_lifecycle.py -v`.

**Acceptance:**

- 4.1.1 - Both parsers default `tls.mode` to `off` on a loopback bind and refuse `off` or an absent mode on a non-loopback bind. test: `tests/config/test_bootstrap.py::test_front_door_tls_default_and_refusal`.
- 4.1.2 - First `serve` in `self-signed` mode generates key and certificate 0600 and prints the fingerprint. test: `crates/gdaemon/tests/front_door.rs::self_signed_generated_on_first_serve`.
- 4.1.3 - HTTP passthrough, typed 503, and WS splice pass over TLS with the pinned client config. test: `crates/gdaemon/tests/front_door.rs::ws_splice_over_self_signed_tls`.
- 4.1.4 - The pinned client config rejects a different certificate and consults no system roots. test: `crates/gdaemon/tests/front_door.rs::pinned_client_rejects_unpinned_cert`.
- 4.1.5 - With TLS on, a loopback peer is served in plaintext and over TLS on the same port, and a non-loopback plaintext peer is closed, for IPv4, IPv6, and IPv4-mapped peers. test: `crates/gdaemon/tests/front_door.rs::plaintext_only_from_loopback_peers`.
- 4.1.6 - A wildcard bind serves loopback on its own listener, and a concrete non-loopback bind adds a same-port loopback listener of the same family. test: `crates/gdaemon/tests/front_door.rs::concrete_bind_adds_loopback_listener`.
- 4.1.7 - Local dial hosts are loopback for wildcard, named, concrete IPv4, and IPv6 binds in both languages, and an explicit `daemon_url` still wins. test: `tests/utils/test_daemon_url.py::test_dial_host_is_always_loopback`.
- 4.1.8 - The e2e fixture serves `https` with `tls="self-signed"` and the lifecycle suite passes over it. test: `tests/e2e/test_daemon_lifecycle.py::test_daemon_serves_over_self_signed_tls`.

**Granularity:** eight items, one leaf. The TLS acceptor, the loopback gate, the
companion listener, and the dial-host contract are one behavior: a TLS-enabled
front door that its own machine can still reach. Shipping the acceptor without
the gate or the dial-host change leaves a hub whose local hooks and CLIs cannot
connect; shipping the dial-host change alone has no observable effect.

### 4.2 `api_keys`, key format, issuance routes, and local-key adoption [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/456_add_api_keys.sql`
- `crates/gcore/src/schema/assets.rs::MIGRATIONS`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated derived schema carrier
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: `GOLDEN_LATEST_CHECKSUM` and `GOLDEN_ASSETS_ROOT_HASH` regenerated
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: the latest-asset filename assertion moves to 456
- `crates/gcore/tests/catalog_manifest_freshness.rs::*` — scope-reason: gains the `api_keys` migration assertion
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: derived schema carrier; it reads `schema_identity()` at run time, so it is re-run as verification with no edit
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated derived schema carrier
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: signed golden embedding the schema identity; regenerated
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: same
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: same
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: same
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: same
- `src/gobby/storage/api_keys.py`
- `src/gobby/utils/api_key_format.py`
- `src/gobby/servers/routes/api_keys.py`
- `src/gobby/servers/_app_routes.py::*` — scope-reason: mount the api_keys router
- `src/gobby/cli/install.py::_provision_local_api_token`
- `src/gobby/runner_init/storage.py::*` — scope-reason: calls `ensure_local_api_key` right after `ensure_machine_identity`
- `src/gobby/config/bootstrap.py::*` — scope-reason: `BootstrapConfig` and `bootstrap_from_mapping` gain `api_key`, `api_key_id`, and `hub_cert`, all absent by default so constructor sites need no edit
- `src/gobby/config/bootstrap_io.py::update_bootstrap_yaml`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `tests/storage/test_api_keys.py`
- `tests/utils/test_api_key_format.py`
- `tests/servers/routes/test_api_keys.py`
- `tests/cli/test_cli_install.py::*` — scope-reason: consumer of `_provision_local_api_token`; asserts the minted key lands in bootstrap
- `tests/cli/test_install_coverage.py::*` — scope-reason: patches `_provision_local_api_token`; verification only
- `tests/cli/test_install_prompts.py::*` — scope-reason: same
- `tests/cli/test_install_front_door.py::*` — scope-reason: same
- `tests/test_runner_init.py::*` — scope-reason: patches `gobby.runner_init.storage.ensure_machine_identity`; gains the `ensure_local_api_key` patch
- `tests/runner_helpers.py::*` — scope-reason: same patch list gains `ensure_local_api_key`

**Research context:**

Migration `456_add_api_keys.sql` (Decision 7), registered in
`crates/gcore/src/schema/assets.rs::MIGRATIONS` after the `455_drop_ask_artifacts.sql`
entry (`filename` plus `include_str!`):

```sql
CREATE TABLE api_keys (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id uuid NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    key_hash text NOT NULL UNIQUE,
    key_hint text NOT NULL,
    label text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX idx_api_keys_machine ON api_keys (machine_id);
ALTER TABLE machines ADD COLUMN last_heartbeat_at timestamptz;
ALTER TABLE machines ADD COLUMN node_version text;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE api_keys TO gobby_daemon_runtime;
```

Carrier set, taken from the 455 commit (d175a1b980): `catalog.manifest.json`
(`UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --features postgres --test catalog_manifest_freshness`),
`grant/bundle.rs` golden checksums, `schema_contract.rs`,
`schema_expected_identity.json`, and the five goldens under
`tests/runtime_grants/golden/`, which embed the schema identity.
`crates/gdaemon/tests/cli_contract.rs` reads `schema_identity()` at run time and
needs no edit.

Key format (`src/gobby/utils/api_key_format.py`): `generate() -> str` returns
`gobby_` + 43 base62 chars encoding 32 random bytes + 6 base62 chars of
zero-padded CRC32 over the 43-char body; `parse(key) -> str | None` checks
prefix, length, alphabet, and checksum without a database; `hash(key)` reuses
`src/gobby/storage/auth.py::hash_token` (SHA-256 hex); `hint(key)` is the last 4
body chars. The test file carries the shared vectors that D1's Rust mirror must
match (Decision 5).

`src/gobby/storage/api_keys.py::ApiKeyManager` (hub-transaction style, `%s`
placeholders): `mint(user_id, machine_id, label) -> (plaintext, ApiKey)`,
`list_for_user(user_id)`, `revoke(key_id, user_id)`,
`resolve_hash(key_hash) -> ApiKey | None` (joins `machines`,
`revoked_at IS NULL`), `touch(key_id)` throttled to once a minute. The module
also holds `ensure_local_api_key(database, machine_id, bootstrap_path)`: when
the bootstrap at that path is `datastore_mode: local` and has no `api_key`, mint
for `LocalUserManager.require_sole_user()` and the given machine with label
`local daemon`, then write `api_key` and `api_key_id` through
`update_bootstrap_yaml`; if the write fails, revoke the new key and re-raise.

Routes (`src/gobby/servers/routes/api_keys.py`, mounted in `_app_routes.py`):
- `POST /api/auth/keys/bootstrap` (public, behind the existing
  `src/gobby/servers/routes/auth.py::_LoginRateLimiter` keyed by client id): body
  `{email, password, machine_id, hostname, os, label}`; verifies with
  `AuthService.verify_password`; upserts the machine under that user through
  `LocalMachineManager.upsert_seen` (`src/gobby/storage/machines.py`), whose
  `MachineOwnershipConflictError` maps to 403; mints; returns
  `{key, key_id, hint, user_id, machine_id}` once with `Cache-Control: no-store`.
- `POST /api/auth/keys` (authenticated): mint for the caller's machine (`{label}`).
- `GET /api/auth/keys` (authenticated): the caller's keys only, fields `id`,
  `hint`, `label`, `machine_id`, `created_at`, `last_used_at`, `revoked_at`;
  never `key_hash` or plaintext.
- `DELETE /api/auth/keys/{id}` (authenticated): owner-only revoke; another
  user's key answers 404 exactly like an absent id.
Until D1, "authenticated" means today's `AuthService` (cookie or the operator
token); D1 swaps the principal source without changing the routes.

Bootstrap fields (Python only, Decision 5): `api_key`, `api_key_id`, `hub_cert`
(path). The hub origin stays the existing `hub_daemon_url`
(`_parse_mode_owner_fields` requires it for `remote` and rejects it for
`local`). `update_bootstrap_yaml` (`src/gobby/config/bootstrap_io.py`) writes
the three keys.

Adoption:
- `gobby install`: `_provision_local_api_token(auth_store)` (`src/gobby/cli/install.py`)
  keeps provisioning the token file until D1 and, when the hub database is
  reachable, also calls `ensure_local_api_key`.
- Startup: `src/gobby/runner_init/storage.py` calls
  `ensure_local_api_key(runner.database, runner.machine_id, <bootstrap path>)`
  right after `runner.machine_id = ensure_machine_identity(...)`, so an existing
  install holds its key after its next start. A node never reaches this code
  (4.6). `ensure_machine_identity` itself is unchanged.

Consumers unchanged:
- `src/gobby/runner_init/helpers.py` — no-edit-reason: `ensure_machine_identity` keeps its signature and body.
- `tests/storage/test_machines.py` — no-edit-reason: exercises `ensure_machine_identity` only; verification only.
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py` — no-edit-reason: same.
- `src/gobby/config/postgres_bootstrap.py` — no-edit-reason: calls `update_bootstrap_yaml` with an unchanged signature for unrelated fields.
- `src/gobby/ui_exposure.py` — no-edit-reason: same.
- `tests/config/test_files_home.py` — no-edit-reason: same; verification only.

Verification planned:
`UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core --features postgres --test catalog_manifest_freshness`, then without the variable,
`cargo test -p gobby-core --features postgres --test schema_contract`,
`cargo test -p gobby-core grant`,
`cargo test -p gobby-daemon --test cli_contract`,
`uv run python scripts/generate_runtime_config_contract.py`,
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_api_keys.py tests/utils/test_api_key_format.py tests/servers/routes/test_api_keys.py tests/cli/test_cli_install.py tests/cli/test_install_coverage.py tests/test_runner_init.py tests/runtime_grants/ tests/config/test_runtime_config_contract.py -v`.

**Granularity:** nine items, one leaf. The migration, `ApiKeyManager`, the
format helper, and the routes are one issuance path; none is observable without
the others. Local-key adoption is the issuance path's second caller and D1's flag
day requires every install to hold a key before it lands. The schema carriers
are regenerated outputs of the one migration.

**Acceptance:**

- 4.2.1 - Migration 456 creates `api_keys` and the two `machines` columns and is registered in `MIGRATIONS`. file: `crates/gcore/assets/schema/migrations/456_add_api_keys.sql`.
- 4.2.2 - `generate`, `parse`, and `hash` match the shared vectors and `parse` rejects a bad checksum, bad alphabet, and wrong length. test: `tests/utils/test_api_key_format.py::test_shared_vectors_and_rejections`.
- 4.2.3 - The bootstrap route verifies the password, binds the machine, and returns the plaintext once with `no-store`; a foreign-owned machine gets 403. test: `tests/servers/routes/test_api_keys.py::test_bootstrap_mints_bound_key`.
- 4.2.4 - Startup adoption mints the local machine's key into bootstrap once, and revokes it when the bootstrap write fails. test: `tests/storage/test_api_keys.py::test_ensure_local_api_key_mints_once_and_revokes_on_write_failure`.
- 4.2.5 - `gobby install` with a reachable hub writes the minted key to bootstrap. test: `tests/cli/test_cli_install.py::test_install_mints_local_api_key`.
- 4.2.6 - List and revoke are owner-scoped and redacted: a foreign revoke answers 404 like an absent id, and list responses carry neither `key_hash` nor plaintext. test: `tests/servers/routes/test_api_keys.py::test_key_management_is_owner_scoped_and_redacted`.
- 4.2.7 - The catalog manifest, `grant/bundle.rs` golden checksums, `schema_contract.rs`, and `schema_expected_identity.json` name migration 456. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
- 4.2.8 - The five signed goldens under `tests/runtime_grants/golden/` carry the new schema identity and the golden-vector tests pass. file: `tests/runtime_grants/golden/brokered_datastores.json`.
- 4.2.9 - Bootstrap parses and writes `api_key`, `api_key_id`, and `hub_cert`, and the config carrier is regenerated. file: `crates/gcore/assets/config/runtime_config_contract.json`.

### 4.5 `gobby auth login` and `gobby auth key --show` [category: code] (depends: 4.1, 4.2)
`kind: deliverable`

Targets:
- `src/gobby/cli/auth.py::auth`
- `src/gobby/cli/auth_login.py`
- `tests/cli/test_auth_login.py`
- `tests/cli/test_cli_auth.py::*` — scope-reason: consumer of the `auth` group; gains the `login` and `key` registration cases
- `tests/e2e/test_auth_login.py`
- `docs/guides/cli-commands.md`

**Research context:**

The routes and the bootstrap fields (`api_key`, `api_key_id`, `hub_cert`, writer
`update_bootstrap_yaml`) are 4.2's; this leaf consumes them unchanged. The
commands live in the new `src/gobby/cli/auth_login.py` and register under the
existing `auth` group in `src/gobby/cli/auth.py`; `gobby auth token` stays until
D1 removes it.

`gobby auth login [--hub URL] [--email] [--fingerprint sha256:...] [--label] [--insecure]`:
- Refuses before any network call, with bootstrap byte-identical, on a
  `datastore_mode: local` bootstrap, naming `docs/guides/shared-stack.md`
  (Client setup). `--hub` defaults to `hub_daemon_url`; a `--hub` whose
  normalized origin differs refuses the same way. Rejected: switching the mode
  in place (a local bootstrap's loopback `database_url` fails
  `_validate_managed_database_url` for `remote`, and `_parse_mode_owner_fields`
  drops `files_home`, stranding owner data); rewriting `hub_daemon_url` (silent
  re-homing of an enrolled node).
- Fetches the hub leaf certificate with `ssl.get_server_certificate`, prints its
  SHA-256 fingerprint, and asks for confirmation unless `--fingerprint` matches
  (a mismatch refuses). Writes the PEM to `~/.gobby/tls/hub.pem` (0600) and
  records its path as `hub_cert`.
- Posts `POST /api/auth/keys/bootstrap` over an httpx client pinned with
  `ssl.create_default_context(cafile=<hub.pem>)` (Decision 4), sending this
  machine's id, hostname, and os; writes `api_key`, `api_key_id`, and
  `hub_cert` through `update_bootstrap_yaml`.
- `--insecure` permits `http://` to a non-loopback hub; without it an `http://`
  non-loopback origin refuses.

`gobby auth key --show` prints the bootstrap key's hint and id. Rotate, list,
and revoke join D1 (Decision 6).

Verification planned:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_auth_login.py tests/cli/test_cli_auth.py tests/e2e/test_auth_login.py -v`.

**Acceptance:**

- 4.5.1 - `gobby auth login` pins by fingerprint against a self-signed hub, refuses a mismatch, and writes bootstrap. test: `tests/e2e/test_auth_login.py::test_login_pins_self_signed_hub`.
- 4.5.2 - Login refuses a `datastore_mode: local` bootstrap and a `--hub` that differs from `hub_daemon_url`, before any network call and with bootstrap byte-identical. test: `tests/cli/test_auth_login.py::test_login_refuses_local_bootstrap_and_hub_mismatch`.
- 4.5.3 - Login refuses an `http://` non-loopback hub without `--insecure`. test: `tests/cli/test_auth_login.py::test_login_refuses_plain_http_remote_without_insecure`.
- 4.5.4 - `gobby auth key --show` prints the hint and id and never the key. test: `tests/cli/test_auth_login.py::test_key_show_prints_hint_only`.

## D1 Hub-side key validation, front-door identity, and shared-token cutover (depends: 4.2, 4.5)
`kind: deferred`

Plan of record 4.3. Blocked by the corpus slice's 3.1 leaf (#21552 (HTTP
contract corpus)), because the cutover re-records the auth corpus cases at
`schema_version` 2 in the same commit. The settled design, refreshed against
`0.5.0` at 069e70d, is below; the planning pass that opens this task re-sweeps
Targets against the code at that time.

- **gdaemon validation** (`front_door/auth.rs`, hub and standalone): dispatch
  the bearer on its kind; `gobby_` is an API key, `v1.` is a managed capability
  token and passes through, any other bearer is 401 `missing_auth` without a
  database hit. Keys are `parse`d (the Rust mirror of 4.2's helper,
  `crates/gcore/src/api_key_format.rs`, registered in `crates/gcore/src/lib.rs`,
  matching 4.2's vectors), hashed, and resolved with one indexed `SELECT` on
  `api_keys` joined to `machines` through gcore's `postgres` feature. On success
  strip client `X-Gobby-User-Id`, `X-Gobby-Machine-Id`, `X-Gobby-Key-Id`,
  `X-Gobby-Front-Door` and set all four. Failures use the existing 401 body
  `{"error": msg, "code": code}`. `last_used_at` touched at most once a minute.
- **Per-boot secret** (`GOBBY_FRONT_DOOR_SECRET`, 32 random bytes) is generated
  by `src/gobby/runner_front_door.py::FrontDoorChild` and passed to the gdaemon
  child and the Python backend; `gobby start` does not spawn gdaemon. 5.2 moves
  generation into gdaemon. Targets include `runner_front_door.py` and
  `tests/test_runner_front_door.py`.
- **Python trusts the front door**: `AuthService._accepted_bearer` accepts the
  operator principal only with the matching secret (constant time); the token,
  `verify_bearer`, `refresh`, `_token_hash_snapshot`, `local_token`, and the
  `X-Gobby-Local-Token` alias are removed; WS `AuthMixin._authenticate` accepts
  the same header; `bearer_matches_grant` compares against the forwarded machine;
  the handshake binds `machine_id` to the forwarded machine.
- **Managed capability tokens** sign with `deployment_runtime.grant_signing_secret`;
  the golden vectors in `crates/gcore/src/grant/tests.rs` and
  `tests/runtime_grants/test_golden_vectors.py` are regenerated. The token moved
  since the plan of record: the signing call sites are now
  `src/gobby/agents/spawn_models.py`, `src/gobby/agents/spawn_executor_providers.py`,
  and `src/gobby/mcp_proxy/tools/spawn_agent/_request.py`;
  `spawn_agent/_implementation.py` is 881 lines, so the plan of record's
  `_selection.py` split is dropped.
- **Challenge goes native** (`front_door/challenge.rs`): `kind: interactive` is
  answered locally as `HMAC-SHA256(api_key, nonce)`; `kind: managed` reaches
  Python. `HubDatabaseBootstrap` gains `api_key` here (Decision 5).
- **Readers and sweep**: every `local_cli_token`, `X-Gobby-Local-Token`, and
  `auth.api_token_hash` reference moves to the bootstrap key or is removed. The
  sweep at 069e70d matches 76 files, including production sites the plan of
  record did not list: ghook `src/transport.rs`, `src/diagnose.rs`,
  `schemas/diagnose-output.v2.schema.json`; gclient `frame_source.rs`,
  `startup.rs`, `command.rs`; gcode `cli_error.rs`, `savings.rs`,
  `commands/embeddings_doctor.rs`, `vector/code_symbols/embedding.rs`,
  `graph/code_graph/lifecycle.rs`; gcore `ai/daemon.rs`,
  `ai/daemon/operations.rs`, `ai/generation/transport.rs`; Python
  `terminals/frame_client.py`, `terminals/native_runtime.py`,
  `runtime_grants/maintenance.py`, `cli/installers/remote_preflight.py`,
  `runner.py::run_gobby` (`StandbyLeaseControl(local_token=...)`); and
  `skills/gobby/references/admin/authentication.md` plus about 15 docs. The
  retired Ask targets (`tests/ask/http_mcp_support.py`,
  `tests/servers/routes/test_ask.py`) no longer exist and are not listed.
- **Size split**: `src/gobby/hooks/inbox.py` is 983 lines; its drain loop and
  retention passes move to `src/gobby/hooks/inbox_maintenance.py` in the same
  commit, as the plan of record specifies.
- **CLI**: `gobby auth key --rotate | list | revoke ID` (Decision 6); rotate
  mints, writes bootstrap atomically, verifies with `GET /api/auth/status`, then
  revokes the old id, and on verify failure revokes the new key and keeps the
  old. `gobby auth token` is removed; the shared-stack guide's Client setup
  stops copying the token file and runs `gobby auth login`.

Acceptance items D1.1 to D1.11, carried from the plan of record:
- D1.1 (4.3.1): gdaemon resolves a valid key, forwards identity headers with the secret, and rejects revoked, malformed, or unknown-kind bearers (the last two without a database hit).
- D1.2 (4.3.2): Python accepts the operator principal only with the matching secret on HTTP and WS; the token and alias are rejected.
- D1.3 (4.3.3): managed capability tokens sign and verify with `grant_signing_secret`; golden vectors regenerated.
- D1.4 (4.3.4): the interactive challenge is answered locally and never forwarded; the managed challenge reaches Python.
- D1.5 (4.3.5): a node user's interactive grant validates at the hub.
- D1.6 (4.3.6): no `local_cli_token`, `X-Gobby-Local-Token`, or `auth.api_token_hash` reference remains under `src/`, `crates/`, or `docs/`.
- D1.7 (4.3.7): e2e suites pass with a provisioned key; auth corpus cases re-recorded at `schema_version` 2.
- D1.8 (4.3.8): `src/gobby/hooks/inbox.py` is below 1,000 lines after the move.
- D1.9: the Rust key-format helper matches 4.2's shared vectors.
- D1.10 (4.5.2 of the plan of record): rotate verifies before revoking and rolls back on verify failure.
- D1.11: list and revoke through the key-authenticated CLI are owner-scoped.

```yaml
deferral:
  task_ref: "TBD-token-cutover"
  reason: "Re-records the auth corpus cases created by the corpus slice's 3.1 (#21552); gated on that leaf landing. Created at expansion under #21555 with a blocked-by edge to the 3.1 leaf."
  owner: "program-director"
  original_acceptance_items:
    - D1.1
    - D1.2
    - D1.3
    - D1.4
    - D1.5
    - D1.6
    - D1.7
    - D1.8
    - D1.9
    - D1.10
    - D1.11
```

## D2 Node channel, relay backend, and `/api/machines` (depends: 4.1, 4.5)
`kind: deferred`

Plan of record 4.4. Blocked by the run-modes slice's 2.1 leaf (#21553 (Run
modes)), for the hub's Python `hub` flag and the `(remote, true)` rejection, and
by D1's task, whose key validation authenticates the channel.

- **Rust mode parsing**: `HubDatabaseBootstrap` and `parse_hub_database_bootstrap`
  (`crates/gcore/src/bootstrap.rs`) gain `datastore_mode`, `hub`,
  `hub_daemon_url`, and `hub_cert`, rejecting `(remote, true)` exactly as the
  Python parser does (Decision 5).
- **Mode container**: `crates/gdaemon/src/state.rs` adds `AppState` with
  `ModeServices::{Standalone, Hub, Node}` (no `Option` mode fields), registered
  in `crates/gdaemon/src/lib.rs`; this is the run-modes slice's former 2.2
  obligation, which that slice assigned to its first consumer.
- **Relay backend**: in node mode the backend target is `hub_daemon_url` over one
  pooled hyper connection using 4.1's `pinned_client_config`; bytes forwarded,
  bearer included; the node validates nothing. A refused hub yields the typed 503
  with `"target": "<hub_daemon_url>"`. WS relays to
  `wss://<hub host:port>/ws`, which serves the same `WebSocketServer` as `:60888`.
- **Channel**: the node opens `GET /api/nodes/channel` (WS) with its key, sends
  `hello` (`node_version`, `platform`), expects `ack`, pings every 30 s, and
  reconnects with backoff (1 s doubling to 60 s). The hub's `nodes/registry.rs`
  maps `machine_id` to one `ChannelHandle` with a connection id; a new
  connection swaps atomically and a stale cleanup never removes its successor.
  Each heartbeat re-reads the key row, closes with 4401 on revocation, and writes
  `last_heartbeat_at` and `node_version` at most once a minute.
- **API**: `nodes/machines_api.rs` serves `GET /api/machines` and
  `GET /api/machines/{id}` natively with `"connected": bool`.
- **Pair test** (PD disposition): the hub is a `daemon_instance` with
  `tls="self-signed"`; the node is a bare `gdaemon serve` (no `GOBBY_PARENT_FD`,
  no Python) with a node bootstrap from a `gobby auth login` run. It asserts
  `connected`, one relayed request, one relayed WS upgrade, and that revocation
  closes the channel within 30 s and the next relayed request gets 401. The plan
  of record's "node runs no maintenance loop (2.3)" clause and its 2.3
  dependency are dropped: the node runs no Python at all (4.6).

Acceptance items D2.1 to D2.7, carried from the plan of record:
- D2.1 (4.4.1): a node relays over the pinned connection and reports the typed 503 when the hub is down.
- D2.2 (4.4.2): the channel registers, heartbeats, and is replaced by a newer connection without the stale cleanup erasing it.
- D2.3 (4.4.3): revocation closes the channel within one heartbeat interval.
- D2.4 (4.4.4): `/api/machines` lists rows with connection state.
- D2.5 (4.4.5): the hub-node pair test passes over self-signed TLS end to end, including one relayed WS upgrade.
- D2.6 (4.4.6): WS relay golden frames and close codes pass byte-equal.
- D2.7: the Rust parser rejects `(remote, true)` and derives the same mode as Python on shared cases.

```yaml
deferral:
  task_ref: "TBD-node-relay"
  reason: "Needs the run-modes slice's 2.1 (#21553) hub flag and D1's key validation; gated on both. Created at expansion under #21555 with blocked-by edges to the 2.1 leaf and the D1 task."
  owner: "program-director"
  original_acceptance_items:
    - D2.1
    - D2.2
    - D2.3
    - D2.4
    - D2.5
    - D2.6
    - D2.7
```

## D3 `gobby-mcp` crate: native MCP transports with OAuth and DCR (depends: 4.2)
`kind: deferred`

Plan of record D1, unchanged in substance. A thin node has no Python, so the
`gobby mcp-server` stdio wrapper is replaced by a workspace-private `gobby-mcp`
crate (binary `gmcp`): stdio for local CLIs and streamable HTTP through the
front door as the `mcp` family; stateless across gdaemon and backend restarts;
OAuth 2.1 with Dynamic Client Registration hosted by gdaemon on the `users` and
`api_keys` identity from 4.2, with access tokens as the second bearer kind at
D1's dispatch seam. Owned by S2.10 and S2.12. It also waits on D2 (node relay).

```yaml
deferral:
  task_ref: "TBD-gobby-mcp"
  reason: "The MCP crate, its HTTP transport, and OAuth/DCR issuance belong to the Stage 2 MCP takeover; this slice delivers the API-key identity they build on."
  owner: "gobby-1.0 stage 2"
  original_acceptance_items:
    - D2.5
```

## D4 gcode index writes from a node need hub HTTP routes (depends: 4.2)
`kind: deferred`

Plan of record D2. Decision 17 removed the datastore tunnel, so `gcode index` on
a node cannot write PostgreSQL directly and needs hub HTTP routes (was S4.5).
Owned by S2.7 and S4.1b. It also waits on D2 (node relay).

```yaml
deferral:
  task_ref: "TBD-node-index-writes"
  reason: "Requires the Stage 2 native code-index family; out of Stage 1 scope."
  owner: "gobby-1.0 stage 2"
  original_acceptance_items:
    - D2.5
```

## D5 Hook envelope carries edited-file content for disk-reading rules (depends: 4.2)
`kind: deferred`

Plan of record D3. Rules that read the edited file from disk cannot do so on a
hub when the hook came from a node; the envelope must carry the fact. Which rules
do this is unverified. Owned by S2.11. It also waits on D1 (cutover), and
inherits D1's corpus wait.

```yaml
deferral:
  task_ref: "TBD-hook-envelope-content"
  reason: "Depends on the S2.11 hook ingress design; the rule inventory is not yet verified."
  owner: "gobby-1.0 stage 2"
  original_acceptance_items:
    - D1.7
```

## V1: Plan Changelog
`kind: framing`

- 2026-09-29: Sliced out of the plan of record's P4 under #23106 (Plan P4 API
  keys and node registration front-door slice), rooted at #21555, and refreshed
  against `0.5.0` at 069e70d.
  - New first leaf 4.6 refuses the Python daemon on a node bootstrap (PD
    disposition).
  - 4.1 serves TLS to remote peers and plaintext to loopback peers on one port,
    with a loopback dial contract and companion listener (PD decision); the
    pinned client moves to gdaemon and Python uses the standard library.
  - 4.2 takes migration 456, `MIGRATIONS` registration, and the five goldens
    from obsolete #23058; Rust bootstrap fields and the Rust format helper move
    to their first consumers.
  - 4.5 keeps login and `--show`; key-authenticated commands move to D1.
  - Plan of record 4.3 and 4.4 become D1 and D2 because they wait on the #23098
    slices (PD disposition); plan of record D1 to D3 become D3 to D5.

## V2: Verification
`kind: verification`

After each leaf, run that leaf's own "Verification planned" commands. Run the
combined commands below once all four leaves have landed, and again before the
PD lands the branch:

```bash
cargo test -p gobby-daemon --test front_door
cargo test -p gobby-core --features postgres --test schema_contract --test catalog_manifest_freshness
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/test_runner_lease_lifecycle.py tests/cli/test_daemon_remote_mode.py tests/cli/test_daemon_preflight.py tests/config/test_bootstrap.py tests/utils/test_daemon_url.py tests/storage/test_api_keys.py tests/utils/test_api_key_format.py tests/servers/routes/test_api_keys.py tests/cli/test_auth_login.py tests/e2e/test_daemon_lifecycle.py tests/e2e/test_auth_login.py tests/runtime_grants/ -v
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
cargo fmt --check && cargo clippy --workspace
uv run gobby plans validate .gobby/plans/gdaemon-api-keys-nodes.md -p .
```

Run these from the worktree root. Do not run the full pytest suite.
