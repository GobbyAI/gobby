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
   - The shared-token cutover bumps the corpus to `schema_version` 2 and
     re-records every case, so it waits on
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
- **Running daemon.** Source validation and live activation are separate
  steps. Each leaf validates in its worktree against isolated test daemons and
  the test hub, and never restarts the running daemon. Live activation belongs
  to the PD and uses the existing release process with no new mechanism:
  - a patch bump of each changed crate;
  - `Cargo.lock` and pin updates;
  - coherent promotion through `promote_workspace_binary_set`;
  - an announced restart in a quiet window.
  4.2's migration and startup adoption take effect on that restart, where the
  local key is minted. D1 is the flag day and carries its own restart.
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
- `tests/cli/test_daemon_preflight.py::*` — scope-reason: gains the real-command node-refusal case for `gobby restart`
- `src/gobby/cli/cutover.py::cutover`
- `tests/cli/test_cutover.py::*` — scope-reason: gains the real-command node-refusal case asserting no build, promotion, stop, or launch
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
  before the lease, the front door, or schema verification. `run_gobby` claims
  the PID file (or adopts the caller's `ownership_resolution`) before
  `load_bootstrap`, and its `finally` that calls `ownership_resolution.release()`
  opens only after the lease is built. The refusal therefore calls
  `ownership_resolution.release()` itself before raising.
- `_start_dependency_errors` (`src/gobby/cli/daemon_start.py`) already loads the
  bootstrap and returns a list of errors that `start` prints before launching.
  It returns `[refusal]` for a node, so `gobby start` refuses before any process
  is spawned and before the managed-services check.
- `restart_start_refusal` (`src/gobby/cli/daemon_preflight.py`) is the start-half
  proof shared by `gobby restart` (`src/gobby/cli/daemon.py`) and
  `gobby cutover` (`src/gobby/cli/cutover.py`). It loads the bootstrap first and
  returns the refusal. `gobby restart` checks it before `_do_stop`, so restart
  refuses with the running daemon untouched. Its signature is unchanged.
- `gobby cutover` (`src/gobby/cli/cutover.py::cutover`) passes
  `restart_start_refusal` into `run_cutover` as a callback, and `run_cutover`
  calls `_build_artifacts` before it. So `cutover` itself loads the bootstrap
  and prints `node_daemon_refusal` at entry, exiting non-zero before any
  workspace build, promotion, stop, or launch subprocess. The callback stays in
  place as the shared start-half proof for local bootstraps.

Shared-stack guide: Client setup today ends with `gobby start` on the client,
which parks in standby against the hub's lease. It changes to: a client runs no
Python daemon; `gobby start` refuses with the message above; the node's front
door arrives with the node relay (D2). The rest of Client setup (bootstrap,
installer) is unchanged in this leaf; D1 replaces the token copy.

Consumers unchanged:
- `src/gobby/cli/daemon.py` — no-edit-reason: calls `restart_start_refusal(ctx)` with an unchanged signature and already prints a returned refusal before `_do_stop`.
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

- 4.6.1 - `run_gobby` with a `datastore_mode: remote` bootstrap raises the refusal before constructing `ActiveDaemonLease`, starting the front door, or verifying the schema, and releases the PID claim it holds. test: `tests/test_runner_lease_lifecycle.py::test_node_bootstrap_refuses_before_lease`.
- 4.6.2 - `gobby start` on a node bootstrap prints the refusal and launches no process. test: `tests/cli/test_daemon_remote_mode.py::test_start_refuses_node_bootstrap`.
- 4.6.3 - The real `gobby restart` command on a node bootstrap prints the refusal and never calls stop, service launch, or runner launch. test: `tests/cli/test_daemon_preflight.py::test_restart_refuses_node_bootstrap`.
- 4.6.4 - `standalone` and local bootstraps start as today. test: `tests/test_runner_lease_lifecycle.py::test_local_bootstrap_still_takes_lease`.
- 4.6.5 - The shared-stack guide's Client setup no longer starts a client daemon and names the refusal. file: `docs/guides/shared-stack.md`.
- 4.6.6 - The real `gobby cutover` command on a node bootstrap prints the refusal and never calls the workspace build, binary promotion, stop, or launch. test: `tests/cli/test_cutover.py::test_cutover_refuses_node_bootstrap_before_build`.

### 4.1 Front-door TLS for remote peers with loopback plaintext [category: code] (depends: 4.6)
`kind: deliverable`

Targets:
- `src/gobby/config/bootstrap.py::*` — scope-reason: `FrontDoorConfig` gains `tls` (`mode`, `cert`, `key`, `sans`) and `bootstrap_from_mapping` enforces the loopback default and the non-loopback refusal; `tls.mode` defaults to `off` so constructor sites need no edit
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: `FrontDoorBootstrap`, its `Default`, `FRONT_DOOR_KEYS`, `parse_front_door`, and `parse_hub_database_bootstrap` change, and the in-file `tests` module gains the TLS default, refusal, and `off` cases
- `crates/gcore/src/daemon_url.rs::*` — scope-reason: `dial_host` changes and the in-file `tests` module gains the loopback and explicit-`daemon_url` cases
- `src/gobby/utils/daemon_url.py::normalize_dial_host`
- `src/gobby/runner.py::_healthy_daemon_running`
- `crates/gdaemon/Cargo.toml`
- `Cargo.lock`
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `bind` adds the companion loopback listener, `accept` keeps the peer and moves the first-byte peek, TLS handshake, and loopback gate into the per-connection task under a deadline, and `run` loads or generates the certificate
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: registers the new `tls` module, and `FrontDoor::handle` takes the observed peer and rewrites the forwarding headers
- `src/gobby/runner_lifecycle.py::*` — scope-reason: the backend `uvicorn.Config` pins `proxy_headers=True` and `forwarded_allow_ips="127.0.0.1,::1"`; its `_healthy_daemon_running(port, backend.host)` call keeps its signature and a `127.0.0.1` host
- `crates/gdaemon/src/front_door/tls.rs`
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: gains the TLS, loopback-gate, and companion-listener tests
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `tests/config/test_bootstrap.py::*` — scope-reason: the TLS block's default, refusal, and loopback-host cases
- `tests/utils/test_daemon_url.py::*` — scope-reason: concrete-host cases now dial loopback
- `tests/e2e/conftest.py::daemon_instance`
- `tests/e2e/conftest.py::DaemonInstance`
- `tests/e2e/conftest.py::authenticated_daemon_client`
- `tests/e2e/conftest.py::authenticated_async_daemon_client`
- `tests/e2e/test_daemon_lifecycle.py::*` — scope-reason: gains `tls_daemon_instance`, `test_daemon_serves_over_self_signed_tls`, and the TLS parametrization of four existing cases
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
    sans: []                           # self-signed mode: extra DNS names or IP literals
```

`bind_host` is loopback when it is `localhost` (the installer default,
`src/gobby/cli/install_setup.py`) or parses as a loopback IP. A non-loopback
bind with `mode` absent or `off` is a parse error naming `self-signed` and
`files`. YAML writes `off` quoted (`mode: "off"`), because an unquoted `off` is
a YAML 1.1 boolean; both parsers also accept boolean `false` as `off`.
`FrontDoorBootstrap` gains `tls: TlsBootstrap { mode, cert, key, sans }`, and
`FrontDoorBootstrap::default` sets `mode: Off`.
`parse_hub_database_bootstrap` today calls `parse_front_door(map.get("front_door"))`
with only the mapping. It changes to parse `bind_host` first and pass it in as
`parse_front_door(value, bind_host)`, so the default and the non-loopback
refusal apply whether the `front_door` mapping is absent or present. Other
consumers of `FrontDoorBootstrap::default` (the `serve.rs` test bootstrap and
the existing bootstrap test at `bootstrap.rs` line 767) inherit `mode: Off`
with no edit.

gdaemon (`crates/gdaemon/src/front_door/tls.rs`, registered in
`front_door/mod.rs`; `rcgen` and `tokio-rustls` are new in
`crates/gdaemon/Cargo.toml`, which today has `hyper` and `hyper-util` but no TLS
crate):
- `self-signed`: on first `serve` with neither `~/.gobby/tls/front_door.crt`
  nor `front_door.key` present, generate an ECDSA P-256 key pair and a
  ten-year certificate, files 0600. SAN policy: `localhost`, the hostname,
  `127.0.0.1`, `::1`, `bind_host` itself when it is a concrete IP, and every
  entry of `tls.sans` (DNS names or IP literals; both parsers reject an
  unspecified address such as `0.0.0.0` or `::`). A wildcard bind adds no
  enumerated interface addresses: an operator who reaches a wildcard-bound hub
  by a numeric address lists it in `tls.sans`. The SAN set is fixed at
  generation; `serve` warns, naming each entry, when a `tls.sans` entry is
  missing from the existing certificate, and changing it means removing both
  files, restarting, and re-running login on every node (new fingerprint).
  When both files exist, every later
  start (including a `FrontDoorChild` respawn) loads and reuses them, so the
  fingerprint a node pinned stays valid. A missing half, an unparsable file, or
  a key that does not match the certificate fails `serve` with an error naming
  the path; gdaemon never regenerates over an existing file. Print
  `front door certificate sha256:<fingerprint>` at every start.
- `files`: load the operator PEM pair (covers `tailscale cert`). A missing,
  unparsable, or mismatched pair fails `serve` with an error naming the path.
- Fingerprint format, shared by Rust `fingerprint` and Python login (4.5):
  `sha256:` followed by 64 lowercase hexadecimal digits of SHA-256 over the
  leaf certificate's DER bytes.
- Acceptor (Decision 3): `serve.rs::accept` keeps the accepted `peer`
  (today it discards it) and spawns the per-connection task first; the accept
  loop itself never reads. Inside the task, the one-byte peek and, for `0x16`,
  the `tokio-rustls` handshake run under
  `tokio::time::timeout(PREAUTH_DEADLINE)`, a private 10-second constant; on
  expiry the connection is dropped. `0x16` wraps the stream in the acceptor;
  any other byte is served in plaintext when
  `peer.ip().to_canonical().is_loopback()`, and closed otherwise. With
  `mode: off` there is no peek or acceptor and the P1 path is unchanged. The
  1.2 WS splice runs inside either stream unchanged.
- Transport identity: `FrontDoor::handle(request, peer)` removes every
  client-supplied `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Proto`,
  `X-Forwarded-Host`, and `X-Real-IP` header and sets
  `X-Forwarded-For: <peer ip>` and `X-Forwarded-Proto: https|http` before
  either the WS splice or route dispatch, so every proxied and native path
  carries only the observed peer, for every TLS mode. The backend's
  `uvicorn.Config` (`src/gobby/runner_lifecycle.py`) pins
  `proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1"`, so
  `request.client` in Python is the front door's observed peer and nothing
  else. The login throttle (`src/gobby/servers/routes/auth.py::_login_client_id`,
  reused by 4.2) therefore keys on the real peer. With the front door disabled,
  Python binds `bind_host` directly and trusts forwarding headers only from a
  loopback caller, which already holds operator access.
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
tests), with the certificate files under the fixture's isolated `GOBBY_HOME`;
`DaemonInstance.http_url`/`ws_url` return `https`/`wss` for it and expose
`cert_path`. `authenticated_daemon_client` and
`authenticated_async_daemon_client` pass
`verify=ssl.create_default_context(cafile=instance.cert_path)` when `cert_path`
is set and keep today's defaults otherwise (Decision 4), so `daemon_client` and
`async_daemon_client`, which delegate to them, pin too.
`tests/e2e/test_daemon_lifecycle.py` gains a `tls_daemon_instance` fixture
(`daemon_instance` with `tls="self-signed"`) and runs the existing
`test_daemon_health_endpoint_responds`, `test_daemon_listens_on_configured_ports`,
`test_daemon_stops_gracefully_on_sigterm`, and
`test_daemon_can_restart_after_stop` against both instances through an indirect
`pytest.mark.parametrize` on the fixture name. The dedicated
`test_daemon_serves_over_self_signed_tls` asserts the served certificate's
fingerprint equals the one printed at start, then restarts the daemon and
asserts the same fingerprint and a successful pinned request. Plaintext loopback
readiness probes in the fixture stay as they are (Decision 3 serves loopback
plaintext on the TLS port).

Consumers unchanged:
- `crates/ghook/src/diagnostics.rs` — no-edit-reason: reports `endpoint.host` for display only.
- `crates/ghook/src/diagnose.rs` — no-edit-reason: same.
- `src/gobby/hooks/hook_manager.py` — no-edit-reason: `HookManager` is built with `daemon_host="localhost"` (`src/gobby/servers/_app_lifecycle.py`), already loopback.
- `src/gobby/runner_front_door.py` — no-edit-reason: `FrontDoorChild` readiness probes are TCP connects, which the first-byte peek does not affect.
- `src/gobby/utils/daemon_client.py` — no-edit-reason: `DaemonClient.__init__` calls `normalize_dial_host` with an unchanged signature and inherits the loopback mapping.
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
`cargo test -p gobby-core bootstrap`,
`cargo test -p gobby-core daemon_url`,
`uv run python scripts/generate_runtime_config_contract.py`,
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_bootstrap.py tests/utils/test_daemon_url.py tests/config/test_runtime_config_contract.py tests/e2e/test_daemon_lifecycle.py -v`.

**Acceptance:**

- 4.1.1 - The Python parser defaults `tls.mode` to `off` on a loopback bind, whether `front_door` is absent or present, refuses `off` or an absent mode on a non-loopback bind, and reads quoted `"off"` and boolean `false` as `off`. test: `tests/config/test_bootstrap.py::test_front_door_tls_default_and_refusal`.
- 4.1.9 - The Rust parser gives the same default, refusal, and `off` readings, with `bind_host` threaded from `parse_hub_database_bootstrap`. test: `crates/gcore/src/bootstrap.rs::tests::front_door_tls_default_and_refusal`.
- 4.1.2 - First `serve` in `self-signed` mode generates key and certificate 0600 and prints the fingerprint; a second `serve` reuses the pair with the same fingerprint and a pinned request succeeds. test: `crates/gdaemon/tests/front_door.rs::self_signed_generated_once_and_reused`.
- 4.1.14 - A generated certificate's SANs are exactly `localhost`, the hostname, `127.0.0.1`, `::1`, a concrete `bind_host` IP, and the `tls.sans` entries; a wildcard bind adds no other address; both parsers reject an unspecified address in `tls.sans`; and a `tls.sans` entry missing from an existing certificate draws the named warning. test: `crates/gdaemon/tests/front_door.rs::self_signed_san_policy`.
- 4.1.10 - `serve` fails naming the path on a missing half, a corrupt file, or a mismatched key in `self-signed` mode and leaves the files untouched, and `files` mode serves a valid operator pair and refuses a mismatched one. test: `crates/gdaemon/tests/front_door.rs::tls_pair_load_or_refuse`.
- 4.1.3 - HTTP passthrough, typed 503, and WS splice pass over TLS with the pinned client config. test: `crates/gdaemon/tests/front_door.rs::ws_splice_over_self_signed_tls`.
- 4.1.4 - The pinned client config rejects a different certificate and consults no system roots. test: `crates/gdaemon/tests/front_door.rs::pinned_client_rejects_unpinned_cert`.
- 4.1.5 - With TLS on, a loopback peer is served in plaintext and over TLS on the same port, and a non-loopback plaintext peer is closed, for IPv4, IPv6, and IPv4-mapped peers. test: `crates/gdaemon/tests/front_door.rs::plaintext_only_from_loopback_peers`.
- 4.1.6 - A wildcard bind serves loopback on its own listener, and a concrete non-loopback bind adds a same-port loopback listener of the same family. test: `crates/gdaemon/tests/front_door.rs::concrete_bind_adds_loopback_listener`.
- 4.1.7 - Python local dial hosts are loopback for wildcard, named, concrete IPv4, and IPv6 binds, and an explicit `daemon_url` still wins. test: `tests/utils/test_daemon_url.py::test_dial_host_is_always_loopback`.
- 4.1.11 - Rust `dial_host` and `endpoint_to_url` give the same loopback mapping and explicit-`daemon_url` precedence. test: `crates/gcore/src/daemon_url.rs::tests::dial_host_is_always_loopback`.
- 4.1.12 - Forged `Forwarded`, `X-Forwarded-For`, and `X-Real-IP` request headers never reach the backend on the proxy, native-health, or WS paths; the backend receives `X-Forwarded-For` equal to the observed peer. test: `crates/gdaemon/tests/front_door.rs::forwarding_headers_carry_only_observed_peer`.
- 4.1.13 - With TLS on, a zero-byte connection and a partial-ClientHello connection are closed after `PREAUTH_DEADLINE` while a concurrent health request on the same listener succeeds. test: `crates/gdaemon/tests/front_door.rs::stalled_preauth_connections_expire_without_blocking`.
- 4.1.8 - The e2e fixture serves `https` with `tls="self-signed"`, keeps its fingerprint across a restart, and the four parametrized lifecycle cases pass over it through the pinned shared clients. test: `tests/e2e/test_daemon_lifecycle.py::test_daemon_serves_over_self_signed_tls`.

**Granularity:** fourteen items, one leaf. The TLS acceptor, the loopback gate, the
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
- `src/gobby/config/bootstrap.py::*` — scope-reason: `BootstrapConfig` and `bootstrap_from_mapping` gain `api_key`, `api_key_id`, and `hub_cert`, all absent by default so constructor sites need no edit; `resolve_bootstrap_path` is extracted from `load_bootstrap`
- `src/gobby/storage/auth.py::*` — scope-reason: `AuthStore` gains the method `session_user_id(token)`; no existing symbol changes
- `tests/storage/test_storage_auth.py::*` — scope-reason: gains the `session_user_id` valid, expired, and unknown cases
- `tests/e2e/test_local_api_key_adoption.py`
- `tests/e2e/test_api_key_bootstrap_lockout.py`
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
`list_for_user(user_id)`, `revoke(key_id, user_id)`. These three have callers
in this leaf (the routes and adoption). Key resolution and `last_used_at`
updates are D1's, in Rust against the indexed hash; no Python
`resolve_hash` or `touch` is added. The module
also holds `ensure_local_api_key(database, machine_id, bootstrap_path)`, a
single read-modify-write transaction:
1. When `bootstrap_path.name` is not `bootstrap.yaml` (a legacy `config.yaml`
   with no sibling `bootstrap.yaml`), return before minting and log a warning
   naming the canonical `bootstrap_path()` and `gobby install` as the
   migration. No legacy file is ever written.
2. Take `exclusive_file_lock(bootstrap_path)` (the sidecar lock
   `update_bootstrap_yaml` uses) and hold it through step 5, so startup and
   `gobby install` serialize. Inside it, `read_bootstrap_yaml`; when the
   mapping is not `datastore_mode: local` or already has `api_key`, return
   (a contender that lost the race reuses the winner's key).
3. Mint for `LocalUserManager.require_sole_user()` and the given machine with
   label `local daemon`.
4. Publish `api_key` and `api_key_id` with `publish_bootstrap_yaml_locked`,
   which does not re-take the lock.
5. On a publication exception, re-read the file. `durable_replace` renames
   before its directory fsync and readback, so an exception does not prove the
   file is unchanged. If the re-read shows the new `api_key_id`, the key is
   committed: keep it live and re-raise. If it shows no new id, revoke the new
   key and re-raise. If the re-read itself fails, leave the key live, log its
   id, and re-raise. No bootstrap ever names a revoked key, and a later start
   retries adoption only when no key was published.

Routes (`src/gobby/servers/routes/api_keys.py`, mounted in `_app_routes.py`):
- `POST /api/auth/keys/bootstrap` (public, behind the existing
  `src/gobby/servers/routes/auth.py::_LoginRateLimiter` keyed by
  `_login_client_id`, which after 4.1 is the front door's observed peer): body
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

Admission. `AuthMiddleware` treats every `/api/auth` path as public
(`src/gobby/servers/middleware/auth.py::_PUBLIC_PREFIXES`), and
`AuthService.request_principal` returns `None` for both the operator token and a
valid cookie, with no user. So the three management routes share one
route-local FastAPI dependency, `require_key_principal(request) -> KeyPrincipal(user_id, machine_id)`,
in `routes/api_keys.py`; the bootstrap route alone stays public. Resolution:
- A `gobby_session` cookie resolves through the new
  `AuthStore.session_user_id(token) -> str | None` (the unexpired
  `auth_sessions` row's `user_id`); the machine is the serving daemon's own,
  `gobby.utils.machine_id.require_machine_id()`.
- The operator bearer or `X-Gobby-Local-Token` (verified by
  `AuthService.verify_bearer`) resolves to `LocalUserManager.require_sole_user()`
  and `require_machine_id()`. When the install has zero or several users,
  `require_sole_user` raises `UserIdentityStateError` and the route answers 403
  naming it; the dependency never picks a user.
- A managed agent capability token, an invalid or absent credential, or an
  expired cookie answers 401 with the existing body `{"error": msg, "code": code}`.
- Before `mint`, the dependency's machine must be owned by its user
  (`machines.owner_user_id`); a foreign machine answers 403.
D1 replaces the resolution with the verified forwarded principal inside the
same dependency; the routes do not change.

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
  `ensure_local_api_key(runner.database, runner.machine_id, resolve_bootstrap_path(runner._config_file))`
  right after `runner.machine_id = ensure_machine_identity(...)`, so an existing
  install holds its key after its next start. `resolve_bootstrap_path(config_path)`
  is new in `src/gobby/config/bootstrap.py` and is the file-choice half of
  `load_bootstrap`, which calls it (today's inline logic at
  `load_bootstrap`'s head): `default_bootstrap_path()` when `config_path` is
  `None`; for a path not named `bootstrap.yaml` (the e2e fixture launches with
  `--config config.yaml`), its sibling `bootstrap.yaml` when that exists;
  otherwise the expanded `config_path`. The write therefore lands in the file
  startup read, and a legacy `config.yaml` is never written. A node never reaches this code
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
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_api_keys.py tests/utils/test_api_key_format.py tests/servers/routes/test_api_keys.py tests/cli/test_cli_install.py tests/cli/test_install_coverage.py tests/test_runner_init.py tests/runtime_grants/ tests/config/test_runtime_config_contract.py tests/config/test_bootstrap.py tests/storage/test_storage_auth.py tests/e2e/test_local_api_key_adoption.py tests/e2e/test_api_key_bootstrap_lockout.py -v`.

**Granularity:** fourteen items, one leaf. The migration, `ApiKeyManager`, the
format helper, and the routes are one issuance path; none is observable without
the others. Local-key adoption is the issuance path's second caller and D1's flag
day requires every install to hold a key before it lands. The schema carriers
are regenerated outputs of the one migration.

**Acceptance:**

- 4.2.1 - Migration 456 creates `api_keys` and the two `machines` columns and is registered in `MIGRATIONS`. file: `crates/gcore/assets/schema/migrations/456_add_api_keys.sql`.
- 4.2.2 - `generate`, `parse`, and `hash` match the shared vectors and `parse` rejects a bad checksum, bad alphabet, and wrong length. test: `tests/utils/test_api_key_format.py::test_shared_vectors_and_rejections`.
- 4.2.3 - Through the full app and its middleware, the bootstrap route verifies the password, binds the machine, and returns the plaintext once with `no-store`; a foreign-owned machine gets 403; repeated bad passwords hit `_LoginRateLimiter` lockout keyed by `_login_client_id`, and a success resets it. test: `tests/servers/routes/test_api_keys.py::test_bootstrap_mints_bound_key`.
- 4.2.4 - Startup adoption mints the local machine's key into bootstrap once; a failure before the rename revokes the new key, and a failure injected after `os.replace` (at the directory fsync and at the readback) keeps the committed key live, so a retry mints nothing and bootstrap never names a revoked key. test: `tests/storage/test_api_keys.py::test_ensure_local_api_key_mints_once_and_follows_publication_point`.
- 4.2.12 - Two synchronized `ensure_local_api_key` callers on one bootstrap leave exactly one live key, matching the bootstrap `api_key` and `api_key_id`. test: `tests/storage/test_api_keys.py::test_concurrent_adoption_mints_one_key`.
- 4.2.13 - A legacy `config.yaml` with no sibling `bootstrap.yaml` mints nothing, writes nothing, and logs the migration warning. test: `tests/storage/test_api_keys.py::test_legacy_config_path_skips_adoption`.
- 4.2.14 - Through a real daemon behind gdaemon, repeated bad bootstrap passwords with varying forged `X-Forwarded-For` values cannot reset the caller's lockout from `127.0.0.1`, and a caller from `::1` (a distinct real peer) still has its own bucket. test: `tests/e2e/test_api_key_bootstrap_lockout.py::test_forged_forwarding_cannot_reset_lockout`.
- 4.2.10 - Through the full app, the management routes answer 401 for an absent, invalid, expired-cookie, or managed-agent credential; a cookie and the operator token each resolve to their user and this machine; the operator token answers 403 when the install has two users; and a mint for a machine owned by another user answers 403. test: `tests/servers/routes/test_api_keys.py::test_management_routes_admit_only_resolved_principals`.
- 4.2.11 - A real isolated daemon launched with `--config config.yaml` mints one key bound to its machine into the sibling `bootstrap.yaml`, leaves `config.yaml` byte-identical, and mints nothing on a second start. test: `tests/e2e/test_local_api_key_adoption.py::test_startup_adopts_local_key_once`.
- 4.2.5 - `gobby install` with a reachable hub writes the minted key to bootstrap. test: `tests/cli/test_cli_install.py::test_install_mints_local_api_key`.
- 4.2.6 - Through the full app with two distinct users, list and revoke are owner-scoped and redacted: a foreign revoke answers 404 with a body identical to an absent id's, and list responses carry neither `key_hash` nor plaintext. test: `tests/servers/routes/test_api_keys.py::test_key_management_is_owner_scoped_and_redacted`.
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

The routes and the bootstrap fields (`api_key`, `api_key_id`, `hub_cert`) are
4.2's. So are the writers `publish_bootstrap_yaml_locked` and
`exclusive_file_lock` (`src/gobby/utils/durable_file.py`). This leaf consumes
all of them unchanged. The
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
- Scheme gate, still before any network call: an `http://` origin is allowed
  for a loopback host, or for a non-loopback host with `--insecure`, and
  refuses otherwise; `--fingerprint` with an `http://` origin refuses.
- Credentials: `--email EMAIL` or an email prompt, a hidden password prompt
  (`click.prompt(..., hide_input=True)`, as `src/gobby/cli/install_identity.py`
  does), and `--label` defaulting to the hostname. The bootstrap-route body is
  `{email, password, machine_id, hostname, os, label}`, with this machine's
  `require_machine_id()`, `socket.gethostname()`, and `platform.system()`.
- HTTPS branch: fetch the leaf with `ssl.get_server_certificate`, print its
  fingerprint in the 4.1 format (`sha256:` plus 64 lowercase hex digits over
  the DER), and require a matching `--fingerprint` or an affirmative
  confirmation; a mismatch or a declined confirmation exits before the
  password is sent. The approved PEM is staged at a private temporary path
  beside `~/.gobby/tls/hub.pem` (0600) and the POST goes over an httpx client
  pinned with `ssl.create_default_context(cafile=<staged pem>)` (Decision 4).
  That context keeps hostname verification: the origin's host must be in the
  certificate's SANs (4.1's SAN policy covers `localhost`, the hostname, both
  loopback literals, a concrete bind IP, and `tls.sans`). A host outside the
  SANs fails the TLS handshake before the request body, and so the password,
  is sent; the error names `front_door.tls.sans` on the hub.
- HTTP branch: no certificate fetch and no pin; the POST goes in plaintext.

Enrollment commits only after the hub mints:
1. Authenticate and mint with the staged pin (or plaintext). A rejected
   password, a network failure, or any non-2xx exits non-zero with the staged
   PEM removed and the prior `hub.pem` and bootstrap byte-identical.
   A 2xx is validated in full before any publication: the body is a JSON
   object; `key` passes 4.2's `api_key_format.parse`; `key_id`, `user_id`,
   and `machine_id` are UUID strings; `machine_id` equals the requested
   `require_machine_id()`; `hint` equals `api_key_format.hint(key)`; and
   `key_id` differs from the prior bootstrap's `api_key_id`. On any failure,
   nothing is published, nothing is revoked, and no key or response body is
   printed. A malformed response does not prove which key it names: a
   UUID-shaped `key_id` could name the prior enrollment or another of the
   user's keys. So the error says an unverified key may have been minted,
   prints the reported `key_id` only when it is a UUID and labels it
   unverified, and names `gobby auth key list` (D1) or the hub UI for cleanup.
2. Publish as one transaction under `exclusive_file_lock(bootstrap_path)`, the
   sidecar lock that `update_bootstrap_yaml` and 4.2's adoption take. Login is
   the only writer of `hub.pem`, so this lock also serializes `hub.pem`.
   Holding the lock:
   1. Read the prior `hub.pem` bytes into memory (if any).
   2. Move the staged PEM over `hub.pem` with `durable_replace_text` (HTTPS
      only).
   3. `read_bootstrap_yaml`, set `api_key`, `api_key_id`, and `hub_cert` (the
      `hub.pem` path, or absent for HTTP), and publish with 4.2's
      `publish_bootstrap_yaml_locked`, which does not re-take the lock.
   Minting in step 1 happens before the lock is taken.
3. On any publication exception, decide by the actual publication point,
   still holding the lock. `durable_replace` renames before its directory
   fsync and readback, so an exception does not prove a file is unchanged.
   Re-read bootstrap:
   - It names the new `api_key_id`: the enrollment is committed (the PEM was
     published first). Keep the new key, print the durability error, and exit
     non-zero so the operator re-runs verification.
   - It does not: restore the prior `hub.pem` bytes with
     `durable_replace_text` (or remove `hub.pem` if there was none).
   - The re-read or the PEM restore fails: revoke nothing; print the new key
     id, which files may have changed, and the error, and exit non-zero.
   Then release the lock. Only when the lock-held decision found the new key
   uncommitted and the restore succeeded, revoke the key minted in step 1 by
   logging in with the same email and password through
   `POST /api/auth/login` for a browser session and
   `DELETE /api/auth/keys/{new id}` with it (the new API key cannot
   authenticate until D1). Exit non-zero. A failed revoke prints the new key
   id and the error.
   Because the snapshot, publication, re-read, and restore share one lock
   hold, a concurrent login never restores over another enrollment's
   published PEM: a login that waits sees the winner's files as its prior
   state. Only a validated response's `key_id` is ever revoked, and never the
   prior enrollment's key. Bootstrap never names a revoked key.
A successful re-login keeps the prior key live on the hub; revoking it is D1's
verified rotate sequence.

Network bounds: `ssl.get_server_certificate(..., timeout=10)`, and every httpx
client (enrollment, login, cleanup) uses `httpx.Timeout(10.0)`. A timeout at
any step before publication exits non-zero with the prior enrollment
untouched.

`gobby auth key --show` prints the bootstrap key's hint and id. Rotate, list,
and revoke join D1 (Decision 6).

Verification planned:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_auth_login.py tests/cli/test_cli_auth.py tests/e2e/test_auth_login.py -v`.

**Acceptance:**

- 4.5.1 - `gobby auth login` pins by fingerprint against a self-signed hub at `https://127.0.0.1` and at `https://[::1]`, refuses a mismatch, and writes bootstrap. test: `tests/e2e/test_auth_login.py::test_login_pins_self_signed_hub`.
- 4.5.7 - Against a `files`-mode hub whose certificate's SANs exclude the dialed host, login fails the handshake before sending the password and leaves the prior enrollment byte-identical. test: `tests/e2e/test_auth_login.py::test_login_refuses_host_outside_sans`.
- 4.5.8 - A 2xx with malformed JSON, a missing field, a key failing `parse`, a mismatched hint, a `machine_id` other than the requested one, or a `key_id` equal to the prior enrollment's `api_key_id` publishes nothing, prints no secret, and sends no revoke request; the prior key stays live and the prior bootstrap and `hub.pem` stay byte-identical; a valid response still enrolls. test: `tests/cli/test_auth_login.py::test_login_validates_enrollment_response`.
- 4.5.9 - Two synchronized logins A and B run while A's bootstrap publication is failed before rename: B's bootstrap, `api_key_id`, and matching `hub.pem` survive whichever order the lock grants, A restores nothing over B's files, and only A's new key is revoked. test: `tests/cli/test_auth_login.py::test_concurrent_enrollments_keep_winner`.
- 4.5.2 - Login refuses a `datastore_mode: local` bootstrap and a `--hub` that differs from `hub_daemon_url`, before any network call and with bootstrap byte-identical. test: `tests/cli/test_auth_login.py::test_login_refuses_local_bootstrap_and_hub_mismatch`.
- 4.5.3 - Login refuses an `http://` non-loopback hub without `--insecure` and `--fingerprint` with any `http://` hub, and enrolls over plain HTTP to a loopback hub and to a non-loopback hub with `--insecure`, fetching no certificate and writing no `hub_cert`. test: `tests/cli/test_auth_login.py::test_login_http_branches`.
- 4.5.5 - A declined confirmation, a fingerprint mismatch, a rejected password, a network failure, and a certificate probe against a listener that accepts and never answers each exit non-zero before any bootstrap or `hub.pem` change, the first two and the stalled probe without sending the password. test: `tests/cli/test_auth_login.py::test_login_failures_preserve_prior_enrollment`.
- 4.5.6 - A failure before the bootstrap rename restores the prior `hub.pem`, revokes only the new key through a password session, and leaves the prior key live and the prior bootstrap byte-identical; a failure injected after the bootstrap `os.replace` (at the directory fsync and at the readback) keeps the committed new key and revokes nothing; a failed restore or cleanup prints the new key id. test: `tests/cli/test_auth_login.py::test_login_compensation_follows_publication_point`.
- 4.5.4 - `gobby auth key --show` prints the hint and id and never the key. test: `tests/cli/test_auth_login.py::test_key_show_prints_hint_only`.

## D1 Hub-side key validation, front-door identity, and shared-token cutover (depends: 4.2, 4.5)
`kind: deferred`

Plan of record 4.3. Blocked by the corpus slice's 3.1 leaf (#21552 (HTTP
contract corpus)), because the cutover bumps the corpus manifest and every
manifest-listed case to `schema_version` 2 in the same commit. The approved
corpus contract requires each case's version to equal the manifest's, so every
`origin: python` case is re-recorded with the 3.1 recorder, and any
`origin: gdaemon` case already present is updated and re-verified. The settled design, refreshed against
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
- D1.7 (4.3.7): e2e suites pass with a provisioned key; the corpus manifest and every case are at `schema_version` 2, with Python-origin cases re-recorded, and both the Python loader/replay and the Rust replay pass at that version.
- D1.8 (4.3.8): `src/gobby/hooks/inbox.py` is below 1,000 lines after the move.
- D1.9: the Rust key-format helper matches 4.2's shared vectors.
- D1.10 (4.5.2 of the plan of record): rotate verifies before revoking and rolls back on verify failure.
- D1.11: list and revoke through the key-authenticated CLI are owner-scoped.

```yaml
deferral:
  task_ref: "TBD-token-cutover"
  reason: "Bumps the corpus created by the corpus slice's 3.1 (#21552) to schema_version 2 and re-records every case; gated on that leaf landing. Created at expansion under #21555 with a blocked-by edge to the 3.1 leaf."
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

## V1 Plan Changelog
`kind: verification`

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
- 2026-09-29: Enhancer pass (run f2e54cbf, gpt-6.1-sol xhigh) returned E1 to
  E10; PD accepted all ten with safeguards, applied here:
  - E1: 4.2's management routes admit through a route-local
    `require_key_principal` (cookie through `AuthStore.session_user_id`,
    operator token through `require_sole_user`, which refuses several users);
    a foreign machine answers 403. 4.2.10 tests this through the full app.
  - E2: 4.6 adds `gobby cutover` entry refusal before any build (4.6.6), tests
    the real restart command, and releases the PID claim on a `run_gobby`
    refusal.
  - E3 and E4: 4.5 login gains explicit HTTP and HTTPS branches, the
    credential prompts, and a mint-then-publish sequence that compensates only
    the newly minted key and preserves the prior enrollment (4.5.5, 4.5.6).
  - E5: 4.1 threads `bind_host` from `parse_hub_database_bootstrap`, covers
    `FrontDoorBootstrap::default`, adds Rust parser and dial-host tests
    (4.1.9, 4.1.11), and splits the invalid two-filter cargo command.
  - E6: the shared e2e clients pin to `cert_path`, and four lifecycle cases
    run over TLS.
  - E7: startup adoption writes the file `load_bootstrap` read, through the
    extracted `resolve_bootstrap_path`; 4.2.11 proves it on a real start.
  - E8: D1 bumps every corpus case to `schema_version` 2.
  - E9: the self-signed pair is reused with a stable fingerprint; a partial or
    mismatched pair fails; `files` mode gains acceptance (4.1.10).
  - E10: Python `resolve_hash` and `touch` are dropped.
  No deferred section became executable.
- 2026-09-29: Adversary review of d776f83 raised P4-01 to P4-06, all accepted:
  - P4-01: the front door keeps the observed peer, replaces every
    client-supplied forwarding header with it, and the backend pins uvicorn's
    loopback proxy trust, so the login throttle keys on the real peer (4.1.12,
    4.2.14).
  - P4-02: local-key adoption holds the bootstrap sidecar lock across recheck,
    mint, and publication (4.2.12).
  - P4-03: adoption and login compensation follow the actual publication
    point after `os.replace`; bootstrap never names a revoked key (4.2.4,
    4.5.6).
  - P4-04: the peek and TLS handshake run in the per-connection task under a
    10-second deadline (4.1.13); login's certificate probe and HTTP calls use
    10-second timeouts (4.5.5).
  - P4-05: a legacy `config.yaml` with no sibling `bootstrap.yaml` skips
    adoption with a migration warning and writes nothing (4.2.13).
  - P4-06: `Cargo.lock` joins 4.1's Targets.
- 2026-09-29: Adversary check of 3c2a388 raised P4-07 and P4-08, both accepted:
  - P4-07: the self-signed SAN policy adds both loopback literals, a concrete
    bind IP, and operator-listed `tls.sans` (no interface enumeration for
    wildcard binds); login keeps hostname verification (4.1.14, 4.5.1,
    4.5.7).
  - P4-08: login validates the full 2xx enrollment response before any
    publication and revokes an identifiable bad key (4.5.8).
- 2026-09-29: Consensus. The Adversary (gobby#14579) independently verified
  251eed5 and found P4-01 to P4-08 resolved with no blocking findings left.
  Writer and Adversary agree on the narrative scope: four executable leaves
  (4.6, 4.1, 4.2, 4.5) carrying 42 acceptance items, and D1 to D5 deferred.
  This entry makes no narrative change. The Adversary applies M1 next, before
  PD review and Josh's approval.
- 2026-09-29: PD exact-tip review of M1 fe64e4b bounced it with two findings,
  both accepted by the Adversary and the Writer. The M1 below is stale until
  the Adversary regenerates it.
  - PD-P4-01: 4.5 publication runs as one transaction under the bootstrap
    sidecar lock, through 4.2's `publish_bootstrap_yaml_locked`. The lock
    covers the prior-PEM snapshot, PEM and bootstrap publication, the
    publication-point re-read, and the restore decision. Minting precedes the
    lock, and the new-key revoke follows its release (4.5.9).
  - PD-P4-02: a malformed 2xx revokes nothing, because a UUID-shaped `key_id`
    does not prove it names the new key. A `key_id` equal to the prior
    enrollment's is invalid (4.5.8).
  - Constraints separate isolated source validation from PD-owned live
    activation through the existing release process.
- 2026-09-29: Renewed consensus. The Adversary (gobby#14579) independently
  checked the 02ef979 repair against the shared helpers and found PD-P4-01,
  PD-P4-02, and the activation wording resolved, with no new blocker. Under
  the PD's ruling, the Writer withdrew the whole stale M1 from fe64e4b,
  whose bytes stay in Git history. No M1 entry was edited in place. The
  narrative scope is four executable leaves (4.6, 4.1, 4.2, 4.5) carrying 43
  acceptance items, and D1 to D5 deferred. The Adversary derives and applies a
  fresh M1 next, before PD review and Josh's approval.

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
