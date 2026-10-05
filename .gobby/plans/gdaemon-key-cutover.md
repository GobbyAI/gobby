Plan artifact: `.gobby/plans/gdaemon-key-cutover.md`

# Hub-side key validation, front-door identity, and shared-token cutover

**Plan ID:** gdaemon-key-cutover

## Overview
`kind: framing`

This plan specifies #23273 (Hub-side key validation, front-door identity, and
shared-token cutover). That task is deferred section D1 of
`.gobby/plans/gdaemon-api-keys-nodes.md` (S1.4, #21555), and it carries
section 4.3 of the plan of record `.gobby/plans/gdaemon-front-door.md` together
with the guides that 4.6 rewrites. Its leaves expand under #23273 (Orchestrator
ruling relayed by the Lane Manager gobby#15389 on 2026-10-05, following the
#23277 precedent). The parent plan's D1 text stays unchanged. In this plan,
"api-keys D1.n" names the parent's acceptance items and "D1" alone names this
plan's deferred activation section.

**The problem.** Every local client authenticates to the daemon with one shared
operator token, `~/.gobby/local_cli_token`. Its SHA-256 hash lives in the
`config_store` row `auth.api_token_hash`. The token also signs every managed
capability token (`gobby-agent-v1.<payload>.<sig>`) that spawned agents carry.
4.2 landed per-machine API keys (`api_keys`, migration 458, the `gobby_` key
format), and 4.5 landed `gobby auth login`, which writes `api_key` and
`api_key_id` into the bootstrap. Nothing validates those keys yet: the front
door forwards bearers untouched and Python still compares them with the
shared token.

**When the leaves close:**
- A host-local, loopback-only break-glass credential admits the operator when
  key verification is wedged. It lands on `0.5.0` first and alone (1.1).
- Managed capability tokens sign with a key derived from the bootstrap
  `api_key`, so they survive daemon restarts and rotate only with the key
  (1.2).
- gdaemon validates `gobby_` keys against `api_keys`, strips client identity
  headers, and forwards the resolved user, machine, and key ids with a per-boot
  front-door secret. Python trusts only that secret. The token file, its stored
  hash, the `X-Gobby-Local-Token` alias, and `gobby auth token` are gone. The
  interactive challenge is answered natively by gdaemon (1.3).
- `gobby auth key` rotates, lists, revokes, and mints keys (1.4).
- Guides and examples describe the key (1.5).
- A rollback rehearsal proves that reverting the cutover code leaves the
  operator a way in (1.6).

Live activation is deferred section D1. The PD runs it through the release
process after every leaf has passed.

## Decision Record
`kind: framing`

Orchestrator rulings relayed by the Lane Manager gobby#15389 on 2026-10-05. The
Orchestrator flags both to Josh at approval.

1. **Managed capability tokens sign with a key derived from the bootstrap API
   key (ruling (a)).** The signing key is
   `HMAC-SHA256(key=api_key, msg=b"gobby-managed-token-v1")`, computed with the
   standard library `hmac` module.
   - It is stable across daemon restarts and lease acquisitions, and it rotates
     only when the bootstrap `api_key` changes (`gobby auth key --rotate`, 1.4).
   - `deployment_runtime.grant_signing_secret` keeps its current jobs, lease
     fencing and runtime grants, and signs no capability token.
   - Rejected: signing with `grant_signing_secret`, as the plan of record and
     api-keys D1.3 specified. That secret is regenerated at every lease
     acquisition, both in Python (`src/gobby/daemon_lease.py`,
     `secrets.token_urlsafe(32)`) and in the Rust lease
     (`crates/gdaemon/src/lease/mod.rs`, #23473). Spawned agents outlive daemon
     restarts, and their `GOBBY_AGENT_API_TOKEN` (TTL up to 86,400 s) has no
     refresh path, so every surviving agent would get 401 after each restart.
   - Rejected: a new signing-secret file, because it adds a credential with its
     own lifecycle when the API key already has the right one.
   - The key is derived instead of being the raw `api_key` for domain
     separation. The interactive challenge proof is `HMAC(api_key, nonce)`. Its
     nonce is capped at 44 characters today
     (`_CHALLENGE_NONCE_MAX_CHARS`, `src/gobby/servers/routes/runtime_handshake.py`),
     so the challenge is no signing oracle for capability payloads, but the
     derivation removes the dependence on that cap.
   - Consequence: api-keys D1.3's "golden vectors regenerated" clause has no
     object. `tests/runtime_grants/test_golden_vectors.py` contains no managed
     capability token, and the `gobby-agent-v1.` tokens in
     `crates/gcore/src/grant/tests.rs` carry fixed fake signature bytes that no
     Rust code verifies. Both stay unchanged and rerun in V2.
2. **`gobby auth key --mint LABEL` (ruling (b)).** It calls
   `POST /api/auth/keys` with the caller's key, prints the new key, its hint,
   and its id once, and writes nothing locally. The minted key is revocable
   through `gobby auth key revoke ID`. This resolves a contradiction in the plan
   of record: 4.6 told shell snippets to read the key with
   `gobby auth key --show`, but 4.5.4 (landed) prints only the hint and id. The
   observability example uses a dedicated minted key, which `--rotate` does not
   invalidate.

Writer decisions, each resolving a question the parent plans left open or got
wrong against the code at `48071323c9`:

3. **The managed token prefix is `gobby-agent-v1.`** The plan of record and
   api-keys D1 say `v1.`. The constant `_AGENT_TOKEN_VERSION = "gobby-agent-v1"`
   is in `src/gobby/utils/local_token.py`,
   `src/gobby/runtime_grants/handshake.py`, and
   `crates/gcore/src/grant/handshake.rs`. gdaemon dispatches on
   `gobby-agent-v1.`.
4. **The break-glass is server-side only.**
   - Credential: a file named `break_glass` in the gobby home, mode 0600,
     holding `secrets.token_urlsafe(32)`. `run_gobby` creates it at every start
     when it is absent and never rewrites an existing one.
   - Admission: a request carrying header `X-Gobby-Break-Glass` with the file's
     value, compared in constant time, from a loopback peer. The check runs
     first in `AuthService._legacy_rejection` and `AuthService._accepted_bearer`,
     before anything that reads the database, and yields the operator principal.
     The file is refused when its mode grants any group or other bit or its
     owner is not the daemon's uid.
   - The peer is `request.client.host`. Behind the front door, that is the peer
     gdaemon observed: `observe_peer` overwrites `X-Forwarded-For`, and uvicorn
     trusts proxy headers only from `127.0.0.1` and `::1`.
   - HTTP only. Recovery needs no WebSocket.
   - No client change. Operators send the header with `curl`.
   - No API-key route change. The hub re-provisions its own key at every start
     (`src/gobby/storage/api_keys.py::ensure_local_api_key`, called from
     `src/gobby/runner_init/storage.py::open_storage_and_config`), so recovery
     never needs a break-glass mint.
   - Rejected: a client opt-in variable in `daemon_auth_headers`. It adds a
     client credential path that `curl` makes unnecessary.
5. **A key-resolver database error is 503, not 401.** gdaemon answers
   `{"error": ..., "code": "key_resolver_unavailable"}` with status 503 when the
   `api_keys` lookup fails. A revoked, unknown, or malformed key is 401 with the
   existing body shape. A 401 during an outage would send operators to re-login
   against a hub that cannot verify anything.
6. **gdaemon refuses to start without `GOBBY_FRONT_DOOR_SECRET`.**
   `FrontDoorChild` always passes the secret. A missing or empty secret means a
   misconfigured spawn, and gdaemon fails closed instead of forwarding identity
   that Python cannot authenticate.
7. **The native interactive challenge reads `api_key` from the bootstrap on
   every request.** After `gobby auth key --rotate`, the next challenge uses the
   new key without a restart. This follows gterm's per-hello read (4.3.11 of
   the plan of record).
8. **The API-key routes take identity only from verified front-door headers.**
   `routes/api_keys.py::_resolve_principal` asks `AuthService` for the
   front-door identity, which applies the same constant-time secret check as
   `authenticate`. It never reads `X-Gobby-User-Id` raw.
9. **No migration.** This plan adds no schema revision. Rollback is a pure code
   revert, and the daemon keeps the schema (`runner_plan.rs` refuses a database
   newer than the runner). The head schema on `0.5.0` is 459
   (`459_session_usage_bigint.sql`), which satisfies api-keys D1.13's "schema
   458" floor. The rehearsal (1.6) runs at the head schema.
10. **The forged-identity corpus case forges only the project header.** After
    the cutover gdaemon strips a client-sent `X-Gobby-Machine-Id`, and the Rust
    corpus harness (`crates/gdaemon/tests/http_contracts.rs`) asserts that the
    stub backend receives every request header unchanged. So
    `auth_forged_identity.json` is re-authored to forge only
    `X-Gobby-Project-Id`, which gdaemon does not strip and which
    `src/gobby/servers/grant_auth.py::identity_headers_match` still rejects with
    `forged_identity`. The stripping itself is proven in
    `crates/gdaemon/tests/front_door.rs` (1.3).
11. **`spawn_agent/_implementation.py` is not edited, so the plan of record's
    `_selection.py` split is dropped.** Its only credential use is
    `code_index_api_token=read_local_api_token`, a bearer fallback for isolated
    gcode (`spawn_executor_providers.py`), not a signature. `read_local_api_token`
    keeps its name and returns the bootstrap key after 1.3. api-keys D1 says the
    signing sites moved to `spawn_models.py`, `spawn_executor_providers.py`, and
    `_request.py`. At `48071323c9` they are `agents/constants.py`,
    `agents/spawn.py`, `agents/code_index.py`, `ai/_managed_tool_chat_lease.py`,
    `runtime_grants/launch.py`, `runtime_grants/maintenance.py`,
    `runtime_grants/handshake.py`, `runner_init/servers.py`, and
    `servers/routes/runtime_handshake.py` (1.2).
12. **The standby lease control keeps its bearer check.**
    `src/gobby/daemon_lease_control.py::create_standby_app` compares the bearer
    with `read_local_api_token()`, which `src/gobby/runner.py::run_gobby` reads
    for `StandbyLeaseControl`. After 1.3 that is the bootstrap key, which is the
    same key gdaemon validates and forwards, because gdaemon never strips
    `Authorization`. Neither file changes.
13. **Landing order.** 1.1 lands on `0.5.0` alone, first, through its own Merge
    Manager landing, so reverting the cutover never removes the break-glass.
    1.2 to 1.6 land together in D1. 1.2 does not land alone: its first restart
    changes the signing key and invalidates every outstanding capability token
    once, so it belongs in D1's quiet window.
14. **Activation is a deferred section.** Expansion manifests reject the
    `manual` category, so D1 expands as a planning task with a needs-planning
    hold and blocked-by edges to 1.1 to 1.6. The Orchestrator retypes it to
    `manual` and the PD runs it. This is the enforceable form of api-keys D1's
    production activation gate.
15. **Node mode answers `gobby_` bearers with 503.** A gdaemon with no
    `database_url` cannot resolve a key, so it answers every `gobby_` bearer
    with 503 `key_resolver_unavailable`. Enrolled nodes dial the hub's
    `hub_daemon_url` directly (4.5). Node-side validation arrives with #23274
    (Node channel, relay backend, and `/api/machines`).
16. **`daemon_auth_headers` keeps the agent identity headers, and
    `DaemonClient` is unchanged.** The plan of record drops both. The headers
    (`X-Gobby-Session-Id`, `X-Gobby-Caller-Project-Id`,
    `X-Gobby-Agent-Run-Id`, `X-Gobby-Managed-Execution-Id`) are ones gdaemon
    never strips, and `AuthService._agent_identity_matches` requires them.
    `DaemonClient` has no machine-id argument at `48071323c9`.

## Constraints
`kind: framing`

- **Isolation.** Every pytest run uses
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`
  from the worktree root, never the full suite. No leaf restarts the running
  daemon, reads or writes `~/.gobby/local_cli_token`, `~/.gobby/bootstrap.yaml`,
  or `~/.gobby/break_glass`, or touches port 60891.
- **Rust.** Load the `rust` skill before editing `crates/`, where
  `crates/CLAUDE.md` governs. `cargo build`, `clippy`, and `nextest` are heavy
  work under `.gobby/roles/_common.md`: they pause while the heavy-work hold is
  in force and need no other admission.
- **No secret values in logs or debug output.** No new code logs the API key,
  the derived signing key, the front-door secret, or the break-glass value.
  `HubDatabaseBootstrap` gains `api_key`, so its derived `Debug` becomes a
  manual impl that prints `api_key` redacted.
- **No backward compatibility.** 0.5.0 has not shipped. The token file, its
  hash, the alias, and `gobby auth token` are removed outright, with no reader
  fallback.
- **Monolith ceiling.** `src/gobby/hooks/inbox.py` (983 lines) is split in 1.3
  before its credential edit. Every other targeted production file is below
  850 lines (sizes in each section's research context).
- **Consumer sweeps.** Targets were swept on `0.5.0` at `48071323c9`
  (2026-10-05) with `gcode grep -F` for `local_cli_token`, `LOCAL_CLI_TOKEN`,
  `X-Gobby-Local-Token`, and `api_token_hash`, and with `gcode grep -w` for
  `local_token_path`, `read_local_api_token`, `operator_token`,
  `local_token_file_present`, `_provision_local_api_token`, `_frame_token`,
  `_read_local_cli_token`, `LOCAL_API_TOKEN_FILENAME`, and every signing
  helper. `docs/evidence/**`, `CHANGELOG.md`, `crates/CHANGELOG.md`, and
  `ROADMAP.md` hold dated history and are excluded.

## Parent D1 Mapping
`kind: framing`

Every api-keys D1 item maps to an item here:

| api-keys item | Here | Note |
| --- | --- | --- |
| D1.1 (4.3.1) | 1.3.1 | gdaemon dispatches on `gobby-agent-v1.`, not `v1.` (Decision 3). |
| D1.2 (4.3.2) | 1.3.2 | |
| D1.3 (4.3.3) | 1.2.1, 1.2.2, 1.2.3 | Restated by ruling (a): the derived key, not `grant_signing_secret`. No golden vector changes (Decision 1). |
| D1.4 (4.3.4) | 1.3.3 | |
| D1.5 (4.3.5) | 1.3.4 | |
| D1.6 (4.3.6) | 1.3.5, 1.5.1 | Source and contract docs in 1.3; guides in 1.5. |
| D1.7 (4.3.7) | 1.3.6, 1.3.7 | |
| D1.8 (4.3.8) | 1.3.8 | |
| D1.9 | 1.3.9 | |
| D1.10 (4.5.2 of the plan of record) | 1.4.1 | |
| D1.11 | 1.3.14, 1.4.2 | Owner scoping at the routes in 1.3; through the CLI in 1.4. |
| D1.12 | 1.1.2, 1.1.3, 1.3.12 | Before the cutover in 1.1; through the key-validating front door in 1.3. |
| D1.13 | 1.6.1 | At the head schema (Decision 9). |

Plan-of-record items without an api-keys number: 4.3.9 (the `_selection.py`
split) is dropped (Decision 11); 4.3.10, 4.3.11, 4.3.12, and 4.3.13 become
1.3.10, 1.3.11, 1.5.2, and 1.3.7; 4.6.1 and 4.6.2 become 1.5.1 and 1.5.3.

## P1: Key-validated front door and shared-token retirement
`kind: framing`

**Goal**: every authenticated request reaches Python through a front door that
has verified a per-machine key, the shared operator token no longer exists, and
the operator keeps a loopback way in when verification is wedged.

### 1.1 Loopback break-glass credential [category: code]
`kind: deliverable`

Targets:
- `src/gobby/utils/break_glass.py`
- `src/gobby/utils/local_token.py::*` — scope-reason: adds `bind_daemon_bootstrap` and `daemon_bootstrap_path`
- `src/gobby/servers/auth_service.py::*` — scope-reason: `AuthService.__init__` gains `break_glass_file`, and `_legacy_rejection` and `_accepted_bearer` admit the break-glass header before any database read
- `src/gobby/runner.py::*` — scope-reason: only `run_gobby` changes; it binds the startup bootstrap and creates the credential before the front door starts, and its signature is unchanged
- `src/gobby/agents/sandbox_policy.py::*` — scope-reason: `_credential_roots` and `_gcode_runtime_root` move to `sandbox_credentials.py` (split below), and `sensitive_roots`, `sensitive_write_roots`, and `gcode_runtime_write_exceptions` call them there
- `src/gobby/agents/sandbox_credentials.py`
- `tests/utils/test_break_glass.py`
- `tests/servers/test_break_glass.py`
- `tests/agents/test_sandbox_policy.py::*` — scope-reason: the eight cases that patch `sandbox_policy.get_gobby_home` set `GOBBY_HOME` with `monkeypatch.setenv` instead, so the moved helpers follow the test home, and the credential-root case asserts that the break-glass file and the bound startup bootstrap are denied for reads and writes
- `tests/test_runner_front_door.py::*` — scope-reason: gains the case proving `run_gobby` creates the credential before the front door starts, including with `--config` outside `GOBBY_HOME`
- `docs/guides/admin-operations.md`
- `docs/contracts/secrets.md`

**Research context:**
- `AuthService.authenticate` (`src/gobby/servers/auth_service.py`, 500 lines)
  sends non-grant routes to `_legacy_rejection` and grant routes to
  `_accepted_bearer`, then requires `X-Gobby-Grant` on grant routes.
  - `_legacy_rejection` checks the `Authorization` bearer (`verify_bearer`,
    then `_classify_agent_token`), then `X-Gobby-Local-Token`, then the
    `gobby_session` cookie, and otherwise returns `missing_auth`.
  - `_accepted_bearer` follows the same order and returns claims, `None` for the
    operator, or `False`.
  - `verify_bearer` calls `refresh()`, which reads
    `AuthStore.get_local_api_token_hash()` from the database and raises when the
    database is broken. So today a wedged database locks out the operator token
    too.
- `AuthService` is built once, in `src/gobby/servers/http.py`
  (`AuthService(lambda: self.services.database)`). The token file defaults to
  `local_token_path()`, which is `get_gobby_home() / "local_cli_token"`. Tests
  build it in `tests/servers/conftest.py` and a dozen route tests with an
  explicit `token_file`.
- `run_gobby` (`src/gobby/runner.py`, 713 lines) calls `verify_schema`, then
  `FrontDoorChild.from_bootstrap(bootstrap, Path(config_path).expanduser().parent if config_path is not None else get_gobby_home())`,
  then starts the child. Callers: `runner.py::main`,
  `tests/test_runner_lifecycle.py`, `tests/test_runner_pid_file.py`, and
  `tests/test_runner_front_door.py`. None changes, because the signature is
  unchanged.
- Credential location. Startup key provisioning writes
  `resolve_bootstrap_path(runner._config_file)`
  (`src/gobby/runner_init/storage.py`), and the front door's home is that
  file's directory. Every in-daemon reader, including `AuthService`
  (`AuthService(lambda: self.services.database)` in `src/gobby/servers/http.py`),
  defaults to `get_gobby_home()`. So with `--config` outside `GOBBY_HOME` the
  two diverge. The precedent for process-wide daemon state is
  `src/gobby/daemon_lease.py::current_lease`, which `code_index.py` reads.
- Peer address: `runner_lifecycle.py` runs uvicorn with `proxy_headers=True` and
  `forwarded_allow_ips="127.0.0.1,::1"`. gdaemon's
  `crates/gdaemon/src/front_door/mod.rs::observe_peer` strips client
  `Forwarded`, `X-Forwarded-*`, and `X-Real-IP`, then sets `X-Forwarded-For` to
  the observed peer. So `request.client.host` is the true client address with
  or without the front door. `src/gobby/config/bootstrap.py::is_loopback_host`
  already classifies `localhost` and loopback literals.
- `src/gobby/agents/sandbox_policy.py` is 935 lines.
  `_credential_roots` (`bootstrap.yaml`, `.secret_kek`, `local_cli_token`,
  `tools/srt`) and `_gcode_runtime_root` feed `sensitive_roots` and
  `sensitive_write_roots`, and `gcode_runtime_write_exceptions` also calls
  `_gcode_runtime_root`. Nothing else calls the two helpers. Eight
  `tests/agents/test_sandbox_policy.py` cases (lines 103, 118, 133, 174, 355,
  675, 706, and 726) patch `sandbox_policy.get_gobby_home`. Once the helpers
  move, `sandbox_credentials` resolves its own `get_gobby_home` global, so those
  patches stop steering `credential_roots()` and `gcode_runtime_root()`, and
  the synthetic `/opt/gobby-home` case at line 133 would derive the real
  runtime root. `gobby.paths.get_gobby_home` reads `GOBBY_HOME` on every call,
  so setting the variable steers both modules and `daemon_bootstrap_path()`.
  `tests/agents/test_sandbox_reaper.py` line 381 patches the same attribute for
  the pre-commit-spare helpers, which do not move, so it is not edited.
  `tests/agents/test_sandbox.py` and `tests/agents/test_external_write_grants.py`
  assert subsets of the returned roots, so an added root leaves them true.
- `src/gobby/agents/code_index.py`'s runtime-home reaper removes copied
  credentials (`local_cli_token`, `.secret_kek`) from isolated runtime homes.
  Nothing copies the break-glass file into a runtime home, so the reaper is not
  edited.

**Implementation:**
- `utils/local_token.py` gains the daemon's credential location, following
  `current_lease`: `bind_daemon_bootstrap(path: Path) -> None` records the
  startup bootstrap for this process, and `daemon_bootstrap_path() -> Path`
  returns it, or `bootstrap_path()` when nothing is bound. Only `run_gobby`
  binds, so client processes keep `bootstrap_path()`. Every in-daemon
  credential reader in this plan resolves through `daemon_bootstrap_path()`.
- New `src/gobby/utils/break_glass.py`:
  - `BREAK_GLASS_FILENAME = "break_glass"` and
    `BREAK_GLASS_HEADER = "X-Gobby-Break-Glass"`.
  - `break_glass_path(gobby_home: Path | None = None) -> Path` returns
    `(gobby_home or daemon_bootstrap_path().parent) / BREAK_GLASS_FILENAME`.
  - `ensure_break_glass_credential(gobby_home: Path) -> None` creates the file
    with `os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o600)` and writes
    `secrets.token_urlsafe(32)` when it is absent. An existing file is never
    rewritten. `FileExistsError` from a concurrent creator is success.
  - `break_glass_matches(path: Path, presented: str) -> bool` returns false when
    the file is missing, unreadable, empty, has any `0o077` mode bit, or is not
    owned by `os.getuid()`. Otherwise it compares with `secrets.compare_digest`.
    It logs neither value.
- `run_gobby` first calls
  `bind_daemon_bootstrap(resolve_bootstrap_path(str(config_path) if config_path is not None else None))`.
  That file's directory is the home `FrontDoorChild.from_bootstrap` receives.
  Immediately before `FrontDoorChild.from_bootstrap` it calls
  `await asyncio.to_thread(ensure_break_glass_credential, daemon_bootstrap_path().parent)`.
  A failure is logged as a
  warning naming the path and does not stop startup: break-glass is a recovery
  aid and must not become a new way to wedge the daemon.
- `AuthService.__init__` gains `break_glass_file: Path | None = None`. When it
  is `None`, each check resolves `break_glass_path()`, so the daemon reads the
  file `run_gobby` created, wherever `--config` points. A private `_break_glass_admits(request)` returns true
  only when `request.client` is not `None`, `is_loopback_host(request.client.host)`
  holds, the header is present, and `break_glass_matches` holds. It is the first
  check in `_legacy_rejection` (returns `None`, admitted) and in
  `_accepted_bearer` (returns `None`, the operator principal). On grant routes
  the grant is still required, so break-glass grants no capability a grant
  would gate. WebSocket authentication does not consult it.
- **Split `src/gobby/agents/sandbox_policy.py`** (935 lines): move
  `_credential_roots` and `_gcode_runtime_root` into the new
  `src/gobby/agents/sandbox_credentials.py` as `credential_roots()` and
  `gcode_runtime_root()`. `sensitive_roots`, `sensitive_write_roots`, and
  `gcode_runtime_write_exceptions` import them. In
  `tests/agents/test_sandbox_policy.py`, each
  `monkeypatch.setattr(sandbox_policy, "get_gobby_home", …)` becomes
  `monkeypatch.setenv("GOBBY_HOME", str(gobby_home))`. `credential_roots()` keeps its `get_gobby_home()` roots and adds
  `daemon_bootstrap_path()` and `break_glass_path()`, so with `--config`
  outside `GOBBY_HOME` the startup bootstrap and break-glass file stay denied
  even under an allowed workspace. The
  credential list then lives in one module that 1.3 edits again without
  touching `sandbox_policy.py`.
- `docs/guides/admin-operations.md` gains a "Break-glass access" section: where
  the file lives, that it is created at daemon start, that it works only from
  the daemon's host, and an example
  `curl -H "X-Gobby-Break-Glass: $(cat ~/.gobby/break_glass)" http://127.0.0.1:60887/api/...`
  against a non-grant route. `docs/contracts/secrets.md` gains a row for the
  file (location, mode 0600, never logged, never copied into runtime homes,
  denied to managed sandboxes).

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/utils/test_break_glass.py tests/servers/test_break_glass.py tests/agents/test_sandbox_policy.py tests/agents/test_sandbox.py tests/agents/test_external_write_grants.py tests/test_runner_front_door.py tests/servers/test_auth_service.py -q`,
then `uv run ruff check` and `uv run mypy` on the touched modules.

**Acceptance:**

- 1.1.1 - `ensure_break_glass_credential` creates a mode-0600 file when it is absent and leaves an existing file byte-identical, and `break_glass_matches` refuses a file with any group or other mode bit, a file owned by another uid, and a wrong value. test: `tests/utils/test_break_glass.py::test_credential_is_owner_only_and_never_rewritten`.
- 1.1.2 - With the database getter raising, a loopback request carrying the break-glass header is admitted to a non-grant route, while a non-loopback peer with the header and a loopback peer with a wrong or missing header get 401. The database getter is never called on the admitted path. test: `tests/servers/test_break_glass.py::test_break_glass_admits_only_loopback_holder_without_database`.
- 1.1.3 - On a grant route, the break-glass header yields the operator bearer principal and the request is still refused without a grant. test: `tests/servers/test_break_glass.py::test_break_glass_still_requires_grant_on_grant_routes`.
- 1.1.4 - `run_gobby` creates the credential before it starts the front door, in the startup bootstrap's directory even when `--config` lies outside a different `GOBBY_HOME`, and an `AuthService` built with defaults in that process admits that file's value. A creation failure does not stop startup. test: `tests/test_runner_front_door.py::test_run_gobby_creates_break_glass_before_front_door`.
- 1.1.5 - Managed sandboxes may neither read nor write the break-glass file or the startup bootstrap, including when the daemon bootstrap is bound to a directory outside a different `GOBBY_HOME` and inside an allowed root. test: `tests/agents/test_sandbox_policy.py::test_break_glass_file_is_a_credential_root`.
- 1.1.6 - The admin guide documents break-glass access. behavior: "Break-glass access" in `docs/guides/admin-operations.md`.

### 1.2 Managed capability tokens sign with a key derived from the bootstrap API key [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/utils/local_token.py::*` — scope-reason: adds `MANAGED_TOKEN_KEY_LABEL`, `derive_managed_signing_key`, and `read_managed_signing_key`; `_issue_managed_api_token`, the three issuers, `verify_agent_api_token`, and `classify_agent_api_token` take `signing_key: bytes` in place of `operator_token`; the `operator_token_unavailable` code becomes `signing_key_unavailable`
- `src/gobby/servers/auth_service.py::*` — scope-reason: `AuthService.__init__` gains `bootstrap_file`, the new `managed_signing_key()` caches the derived key, and `_classify_agent_token` verifies with it
- `src/gobby/runtime_grants/launch.py::*` — scope-reason: `materialize_managed_launch` takes `signing_key` and passes it to the three issuers
- `src/gobby/runtime_grants/maintenance.py::*` — scope-reason: the `HandshakeMaintenanceLaunchFactory.operator_token` field is removed; `open()` reads `read_managed_signing_key()` on every launch and fails closed with `signing_key_unavailable` before issuing a grant
- `src/gobby/runtime_grants/handshake.py::*` — scope-reason: `challenge_proof` takes the signing key for `kind: managed` while the interactive branch keeps the operator token until 1.3, `_recompute_capability_signature` takes the signing key, and the unread `HandshakeService.operator_token` field is deleted
- `src/gobby/runner_init/servers.py::*` — scope-reason: `_handshake_factory` stops reading and passing the operator token, and the maintenance launch factory is built without a key
- `src/gobby/servers/routes/runtime_handshake.py::*` — scope-reason: the challenge route passes the managed signing key for `kind: managed`
- `src/gobby/agents/constants.py::*` — scope-reason: the `get_terminal_env_vars` parameter `operator_token` becomes `signing_key`
- `src/gobby/agents/spawn.py::*` — scope-reason: both signing sites read `read_managed_signing_key()` in place of `read_local_api_token()`
- `src/gobby/agents/code_index.py::*` — scope-reason: the preflight passes `read_managed_signing_key()` to `materialize_managed_launch` and raises `signing_key_unavailable` without it
- `src/gobby/ai/_managed_tool_chat_lease.py::*` — scope-reason: passes `read_managed_signing_key()` to `materialize_managed_launch`
- `src/gobby/agents/resume_metadata.py::*` — scope-reason: one comment names the operator token as the capability signing source; it names the managed signing key
- `tests/utils/test_local_token.py::*` — scope-reason: issuer and verifier cases sign with a derived key and gain the derivation and missing-key cases
- `tests/servers/test_auth_service.py::*` — scope-reason: managed-claim cases provision a bootstrap carrying `api_key` and gain the restart and rotation cases
- `tests/servers/conftest.py::*` — scope-reason: the `AuthService` fixtures pass a temporary `bootstrap_file` carrying `api_key`
- `tests/agents/test_agent_constants.py::*` — scope-reason: the env-builder cases pass `signing_key` and verify with it
- `tests/agents/test_isolation.py::*` — scope-reason: verifies minted capabilities with the derived key and asserts `signing_key_unavailable`
- `tests/agents/test_spawn.py::*` — scope-reason: patches `read_managed_signing_key` where it patched `read_local_api_token`, and asserts the `signing_key` keyword
- `tests/agents/test_spawn_executor.py::*` — scope-reason: passes `signing_key` to the env builder
- `tests/ai/test_managed_tool_chat_lease.py::*` — scope-reason: patches `read_managed_signing_key` and verifies with the derived key
- `tests/e2e/test_stateless_ambient_session.py::*` — scope-reason: derives the signing key from the isolated daemon's bootstrap `api_key` instead of reading the token file to sign
- `tests/mcp_proxy/test_workspaces_registry.py::*` — scope-reason: signs agent tokens with the derived key its `AuthService` reads
- `tests/runner_init/test_grant_issuance.py::*` — scope-reason: verifies issued capabilities with the derived key
- `tests/runtime_grants/test_maintenance_principal.py::*` — scope-reason: `_recompute_capability_signature` and `verify_agent_api_token` take the derived key
- `tests/runtime_grants/test_maintenance_launch.py::*` — scope-reason: the factory is built without a key and signs with the key read at `open()`; gains 1.2.7
- `tests/servers/routes/test_agents_routes.py::*` — scope-reason: signs agent tokens with the derived key its `AuthService` reads
- `tests/servers/routes/test_api_keys.py::*` — scope-reason: its managed-capability rejection case signs with the derived key
- `tests/servers/routes/test_runtime_config.py::*` — scope-reason: issues and verifies capabilities with the derived key
- `tests/servers/routes/test_runtime_handshake.py::*` — scope-reason: the managed challenge case expects the derived key; gains 1.2.4
- `tests/servers/test_mcp_programmatic_boundary.py::*` — scope-reason: signs agent tokens with the derived key its `AuthService` reads
- `tests/storage/test_managed_credentials.py::*` — scope-reason: issues capabilities with the derived key

**Granularity:** one leaf. Twelve production files change, but they carry one
behavior: which key signs and verifies a capability token. Every issuer,
verifier, and challenge site must switch in the same commit, or a token issued
under one key is verified under the other and every managed request 401s. The
edits outside `local_token.py` and `auth_service.py` are parameter renames and
call-site swaps.

**Research context:**
- Ruling (a), Decision 1. The token format is
  `gobby-agent-v1.<b64 payload>.<b64 sig>`;
  `src/gobby/utils/local_token.py::_issue_managed_api_token` signs with
  `hmac.new(operator_token.encode(), signed.encode(), hashlib.sha256)`.
  `runtime_grants/handshake.py::_recompute_capability_signature` recomputes it
  for the managed challenge.
- Signing call sites at `48071323c9`:
  - `agents/spawn.py` reads `read_local_api_token()` at two sites: the prelaunch
    credential (it raises "prelaunch credential requires an operator token" when
    absent) and the `operator_token=` argument to `get_terminal_env_vars`.
  - `agents/code_index.py` reads it in the preflight and raises
    `IndexInventoryError("operator_token_unavailable", …)` when absent.
  - `ai/_managed_tool_chat_lease.py` reads it and passes it to
    `materialize_managed_launch`.
  - `runtime_grants/launch.py::materialize_managed_launch` passes
    `operator_token` to the three issuers.
  - `runner_init/servers.py::_handshake_factory` builds `HandshakeService` with
    `operator_token=server.auth_service.local_token() or ""`, and builds
    `HandshakeMaintenanceLaunchFactory(operator_token=…)` the same way.
    `HandshakeService.operator_token` is never read. The maintenance factory
    stores its value and signs every launch with it in `open()`, so a key
    captured at init would outlive a 1.4 rotation and every nightly repair,
    prune, and maintenance launch would fail until restart.
  - `servers/routes/runtime_handshake.py` reads `local_token()` for the
    challenge and answers 503 "operator token unavailable" without it.
  - `AuthService._classify_agent_token` calls
    `classify_agent_api_token(token, self.local_token())`.
- Unchanged consumers: `runtime_grants/__init__.py` re-exports
  `materialize_managed_launch` and `challenge_proof` by name.
  `tests/agents/test_cargo_target.py` and
  `tests/integration/test_terminal_mode_worktrees.py` call
  `get_terminal_env_vars` without the key argument.
  `tests/hooks/test_inbox.py`, `tests/hooks/test_inbox_barrier_deadline.py`,
  `tests/events/test_wake_recovery.py`,
  `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py`,
  `tests/test_runner_lifecycle_restart_replay.py`,
  `tests/test_runner_lease_lifecycle.py`, and `tests/test_runner_front_door.py`
  patch `read_local_api_token` as a bearer, which this leaf does not touch.
  `spawn_agent/_implementation.py` uses the token as a bearer (Decision 11).
  `tests/runtime_grants/test_golden_vectors.py` and
  `crates/gcore/src/grant/tests.rs` hold no real capability signature
  (Decision 1).
- The bootstrap is read with `src/gobby/config/bootstrap_io.py::read_bootstrap_yaml`
  at `bootstrap_path()` (`get_gobby_home() / "bootstrap.yaml"`). `api_key` is
  written by `storage/api_keys.py::ensure_local_api_key` on a local hub at
  every start, and by `gobby auth login` on a node. Both write through
  `publish_bootstrap_yaml_locked`, which replaces the file by rename, so a
  rewrite always changes the inode.
- File sizes: `auth_service.py` 500, `local_token.py` 318, `code_index.py` 787,
  `runner_init/servers.py` 713. All are below 850 lines.

**Implementation:**
- `utils/local_token.py`:
  - `MANAGED_TOKEN_KEY_LABEL = b"gobby-managed-token-v1"`.
  - `derive_managed_signing_key(api_key: str) -> bytes` returns
    `hmac.new(api_key.encode(), MANAGED_TOKEN_KEY_LABEL, hashlib.sha256).digest()`.
  - `read_managed_signing_key(bootstrap: Path | None = None) -> bytes | None`
    reads `api_key` from `bootstrap` or `daemon_bootstrap_path()` (1.1) and
    derives the key. It returns `None`
    when the file is unreadable or carries no non-empty string `api_key`.
  - `_issue_managed_api_token` and the issuers sign with
    `hmac.new(signing_key, signed.encode(), hashlib.sha256)`. Verification uses
    the same key with `hmac.compare_digest`.
  - `classify_agent_api_token(token, signing_key: bytes | None)` returns
    `signing_key_unavailable` when the key is `None`.
- `AuthService.__init__` gains `bootstrap_file: Path | None = None`; `None`
  resolves `daemon_bootstrap_path()` on each call, so verification and the
  in-process signers read the same startup bootstrap.
  `managed_signing_key() -> bytes | None` stats the file
  on every call and re-reads and re-derives only when
  `(st_ino, st_mtime_ns, st_size)` changes, so a rotated key applies on the
  next request. It never depends on `refresh()`, which 1.3 deletes.
  `_classify_agent_token` passes it.
- `runtime_grants/handshake.py`: `challenge_proof(nonce, *, kind, operator_token, signing_key, claims)`
  uses `operator_token` for `interactive` and `signing_key` for `managed`.
  Delete `HandshakeService.operator_token` and stop passing it in
  `_handshake_factory`.
- Rename `operator_token` to `signing_key` in
  `materialize_managed_launch` and `get_terminal_env_vars`. The call sites in
  `spawn.py`, `code_index.py`, and `_managed_tool_chat_lease.py` call
  `read_managed_signing_key()` where they called `read_local_api_token()`, and
  keep their existing fail-closed branches with `signing_key_unavailable`
  wording.
- `HandshakeMaintenanceLaunchFactory` loses its key field and constructor
  argument. `open()`, which `open_async` delegates to, calls
  `read_managed_signing_key()` on every launch, as
  those call sites do, and passes the result to `materialize_managed_launch`.
  When it returns `None`, `open()` raises
  `HandshakeRejection(..., code="signing_key_unavailable")` before
  `issue_for_maintenance`, so no grant is issued.
  `runner_init/servers.py` builds the factory without a key.
- `routes/runtime_handshake.py` answers 503 `signing_key_unavailable` when a
  managed challenge finds no signing key. The interactive challenge keeps its
  operator-token path until 1.3 moves it into gdaemon.
- No golden vector changes. Grants keep `grant_signing_secret`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/utils/test_local_token.py tests/servers/test_auth_service.py tests/agents/test_agent_constants.py tests/agents/test_isolation.py tests/agents/test_spawn.py tests/agents/test_spawn_executor.py tests/ai/test_managed_tool_chat_lease.py tests/mcp_proxy/test_workspaces_registry.py tests/runner_init/test_grant_issuance.py tests/runtime_grants/ tests/servers/routes/test_agents_routes.py tests/servers/routes/test_api_keys.py tests/servers/routes/test_runtime_config.py tests/servers/routes/test_runtime_handshake.py tests/servers/test_mcp_programmatic_boundary.py tests/storage/test_managed_credentials.py tests/agents/test_cargo_target.py -q`,
then `uv run ruff check` and `uv run mypy` on the touched modules.
`tests/e2e/test_stateless_ambient_session.py` runs in V2.

**Acceptance:**

- 1.2.1 - A capability issued with `derive_managed_signing_key(api_key)` verifies with that key and fails with the raw API key, with a key derived from another API key, and when signed with the API key itself; the derived key equals `HMAC-SHA256(api_key, b"gobby-managed-token-v1")`. test: `tests/utils/test_local_token.py::test_managed_tokens_sign_with_derived_key`.
- 1.2.2 - A capability issued before a restart stays valid after it: a new `AuthService` over the same bootstrap, bound to a lease with a different `grant_signing_secret`, still classifies it as live managed claims. test: `tests/servers/test_auth_service.py::test_managed_token_survives_restart_and_new_lease`.
- 1.2.3 - After the bootstrap `api_key` is replaced by rename, the next request with a capability signed under the old key is rejected and one signed under the new key is accepted, with no refresh interval in between. test: `tests/servers/test_auth_service.py::test_rotated_bootstrap_key_invalidates_managed_tokens`.
- 1.2.4 - The managed challenge proof uses the derived key and the interactive proof is unchanged. test: `tests/servers/routes/test_runtime_handshake.py::test_managed_challenge_uses_derived_signing_key`.
- 1.2.5 - With no bootstrap `api_key`, issuance fails closed with `signing_key_unavailable` and every presented capability is rejected. test: `tests/utils/test_local_token.py::test_missing_bootstrap_key_refuses_issuance_and_verification`.
- 1.2.6 - With the daemon bootstrap bound outside a different `GOBBY_HOME` whose own bootstrap carries another key, a capability from `read_managed_signing_key()` verifies in an `AuthService` built with defaults, and one signed with the `GOBBY_HOME` key is rejected. test: `tests/servers/test_auth_service.py::test_managed_signing_uses_the_daemon_bootstrap`.
- 1.2.7 - A maintenance launch opened after the bootstrap `api_key` is replaced by rename, with the same factory instance and no restart, carries a capability that an `AuthService` over that bootstrap accepts. With no bootstrap `api_key`, `open()` raises `signing_key_unavailable` and issues no grant. test: `tests/runtime_grants/test_maintenance_launch.py::test_launch_signs_with_current_bootstrap_key`.

### 1.3 Shared-token cutover: gdaemon validates keys and Python trusts only the front door [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/front_door/auth.rs`
- `crates/gdaemon/src/front_door/challenge.rs`
- `crates/gdaemon/src/front_door/mod.rs::*` — scope-reason: registers `auth` and `challenge`; `FrontDoorState` carries the auth state and `FrontDoor::handle` authenticates HTTP requests and WebSocket upgrades before dispatch or `ws::splice`
- `crates/gdaemon/src/front_door/proxy.rs::*` — scope-reason: strips client identity headers and sets the resolved identity with the front-door secret
- `crates/gdaemon/src/serve.rs::*` — scope-reason: `run` reads `GOBBY_FRONT_DOOR_SECRET` and builds the key resolver from `database_url`; `serve` takes the auth state
- `crates/gdaemon/tests/front_door.rs::*` — scope-reason: gains the key, identity, challenge, resolver-error, and break-glass pass-through tests; spawned `serve` children get a secret
- `crates/gdaemon/tests/common/mod.rs::*` — scope-reason: its `serve` calls pass a test auth state
- `crates/gdaemon/tests/heartbeat.rs::*` — scope-reason: its `serve` call passes a test auth state
- `crates/gdaemon/tests/http_contracts.rs::*` — scope-reason: `start_front_door` passes a test auth state and a test bootstrap key; replays the version-2 corpus
- `crates/gcore/src/api_key_format.rs`
- `crates/gcore/src/lib.rs::*` — scope-reason: registers `api_key_format`
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: `HubDatabaseBootstrap` gains `api_key` and a manual `Debug` that redacts it
- `crates/gcore/src/local_token.rs::*` — scope-reason: the three readers become `read_api_key`, `read_api_key_for`, and `read_api_key_at` and read the bootstrap `api_key`; `LOCAL_CLI_TOKEN_FILENAME` is deleted
- `crates/gcore/src/grant/acquisition.rs::*` — scope-reason: `interactive_bearer` follows the rename, and the managed acquisition path moves to the child module `managed.rs` (split below)
- `crates/gcore/src/grant/acquisition/managed.rs`
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcore/src/ai/effective_config.rs::*` — scope-reason: follows the `read_api_key*` rename
- `crates/gcore/src/ai/effective_config/tests.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcore/src/ai/daemon.rs::*` — scope-reason: the re-export follows the `read_api_key*` rename
- `crates/gcore/src/ai/daemon/transport.rs::*` — scope-reason: the crate-local wrapper follows the `read_api_key*` rename
- `crates/gcore/src/ai/daemon/operations.rs::*` — scope-reason: follows the `read_api_key*` rename
- `crates/gcore/src/ai/daemon/tests.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcore/src/ai/generation/transport.rs::*` — scope-reason: follows the `read_api_key*` rename
- `crates/gcore/src/ai/generation/tests/daemon_agentic.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gclient/src/command.rs::*` — scope-reason: follows the `read_api_key*` rename
- `crates/gclient/src/frame_source.rs::*` — scope-reason: follows the `read_api_key*` rename
- `crates/gclient/src/startup.rs::*` — scope-reason: without `--token-file` the key comes from `read_api_key()`; the default token-file path is deleted
- `crates/gclient/tests/startup.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gclient/tests/frame_source_live.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gclient/tests/loop_liveness.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gclient/tests/mock_daemon/host.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcode/src/cli_error.rs::*` — scope-reason: the 401 remediation names `gobby auth login`
- `crates/gcode/src/commands/embeddings_doctor.rs::*` — scope-reason: follows the `read_api_key*` rename
- `crates/gcode/src/graph/code_graph/lifecycle.rs::*` — scope-reason: follows the `read_api_key*` rename
- `crates/gcode/src/savings.rs::*` — scope-reason: follows the `read_api_key*` rename
- `crates/gcode/src/config/layers.rs::*` — scope-reason: its test module writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcode/src/graph/code_graph/tests.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcode/src/vector/code_symbols/embedding.rs::*` — scope-reason: its test module writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcode/tests/concurrent_vector_grant.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcode/tests/effective_config.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcode/tests/evidence.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gcode/tests/grant_errors.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/ghook/src/diagnose.rs::*` — scope-reason: `local_token_file_present` becomes `api_key_present` and `AUTH_401_REMEDIATION` names `gobby auth login`
- `crates/ghook/src/transport.rs::*` — scope-reason: `post_and_cleanup` reads the key through `read_api_key*`, and its bearer test provisions a bootstrap `api_key`
- `crates/ghook/schemas/diagnose-output.v2.schema.json::*` — scope-reason: the `local_token_file_present` property and its required entry become `api_key_present`
- `schemas/diagnose-output.v2.schema.json::*` — scope-reason: byte-identical public mirror of the same schema change
- `crates/gterminal/Cargo.toml`
- `crates/gterminal/src/host/mod.rs::*` — scope-reason: `read_local_token` reads `api_key` from `$GOBBY_HOME/bootstrap.yaml`; the socket-directory candidate and `LOCAL_CLI_TOKEN_FILE` are deleted; `HostState::new` and `HostState::restored` lose the token argument
- `crates/gterminal/src/host/frames.rs::*` — scope-reason: `handle_connection` compares the hello token with a fresh `read_local_token` read
- `crates/gterminal/src/host/state.rs::*` — scope-reason: `HostState` drops the cached `local_token` field and its constructor parameter
- `crates/gterminal/src/host/write.rs::*` — scope-reason: its test helper `state()` constructs `HostState` without the token
- `crates/gterminal/src/host/backpressure/tests.rs::*` — scope-reason: constructs `HostState` without the token
- `crates/gterminal/src/host/native_ops/tests.rs::*` — scope-reason: constructs `HostState` without the token
- `crates/gterminal/src/host/tests.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gterminal/tests/control_protocol.rs::*` — scope-reason: the frames fixture writes a bootstrap `api_key`, and the `LOCAL_CLI_TOKEN` constant is renamed
- `crates/gterminal/tests/embed_support/mod.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gterminal/tests/frame_protocol.rs::*` — scope-reason: provisions a bootstrap `api_key` and gains 1.3.11
- `crates/gterminal/tests/handover_support/mod.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gterminal/tests/host_lifecycle.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gterminal/tests/host_support/mod.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `crates/gterminal/tests/terminal_theme.rs::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `src/gobby/runner_front_door.py::*` — scope-reason: `FrontDoorChild` generates the per-boot secret once and passes it to every gdaemon child in `GOBBY_FRONT_DOOR_SECRET`, and the missing-gdaemon `FrontDoorStartupError` stops offering `front_door.enabled: false` as an equivalent
- `src/gobby/runner_init/servers.py::*` — scope-reason: `_bind_runtime_grants` binds the front-door secret into `AuthService`, and `init_servers` wires the WebSocket identity callback
- `src/gobby/servers/auth_service.py::*` — scope-reason: adds the front-door identity check; removes `token_file`, `verify_bearer`, `verify_ws_token`, `refresh`, `_token_hash_snapshot`, `local_token`, and the `X-Gobby-Local-Token` alias
- `src/gobby/servers/middleware/auth.py::*` — scope-reason: `_LOGIN_GUIDANCE` names `gobby auth login`; `dispatch` carries the forwarded identity
- `src/gobby/servers/websocket/auth.py::*` — scope-reason: `AuthMixin._authenticate` passes the handshake headers to the identity callback
- `src/gobby/servers/websocket/server.py::*` — scope-reason: the `auth_callback` type takes headers; its two annotations change
- `src/gobby/servers/grant_auth.py::*` — scope-reason: `bearer_matches_grant` compares an interactive principal with the forwarded machine
- `src/gobby/servers/routes/runtime_handshake.py::*` — scope-reason: the interactive challenge leaves Python, and `issue_for_operator` receives the forwarded machine
- `src/gobby/servers/routes/api_keys.py::*` — scope-reason: `_resolve_principal` takes the operator principal from the verified front-door identity
- `src/gobby/servers/routes/configuration_effective.py::*` — scope-reason: drops the `verify_bearer` consumer
- `src/gobby/runtime_grants/handshake.py::*` — scope-reason: `challenge_proof` loses its interactive branch, and `HandshakeService.issue_for_operator` binds `machine_id` to the forwarded machine
- `src/gobby/utils/local_token.py::*` — scope-reason: `read_local_api_token` reads the bootstrap `api_key`; `local_token_path`, `LOCAL_API_TOKEN_FILENAME`, and `LOCAL_API_TOKEN_HASH_KEY` users are deleted
- `src/gobby/utils/daemon_client.py::*` — scope-reason: the 401 remediation names `gobby auth login`
- `src/gobby/agents/code_index.py::*` — scope-reason: the runtime-home reaper drops the `local_cli_token` name and keeps `.secret_kek`
- `src/gobby/agents/sandbox_credentials.py`
- `src/gobby/terminals/frame_client.py::*` — scope-reason: `_read_local_cli_token` becomes a `read_local_api_token()` call
- `src/gobby/terminals/native_runtime.py::*` — scope-reason: the frame-stream methods move to `native_frames.py` (split below)
- `src/gobby/terminals/native_frames.py`
- `src/gobby/cli/installers/remote_preflight.py::*` — scope-reason: `probe_hub_user_md` and `_credential_errors` authenticate with the bootstrap `api_key` and name `gobby auth login`
- `src/gobby/install/shared/skills/gobby/references/admin/authentication.md`
- `src/gobby/hooks/inbox.py::*` — scope-reason: the missing-credential warning names `gobby auth login`, and the drain loop and retention passes move out (split below)
- `src/gobby/hooks/inbox_maintenance.py`
- `src/gobby/runner_maintenance/messaging.py::*` — scope-reason: imports `drain_hook_inbox_loop` from `gobby.hooks.inbox_maintenance`
- `src/gobby/storage/auth.py::*` — scope-reason: removes the local-token hash key and accessors, `ensure_local_api_token`, `rotate_local_api_token`, and `_write_new_local_api_token`
- `src/gobby/runner_init/storage.py::*` — scope-reason: `open_storage_and_config` drops its `ensure_local_api_token` call and import; `ensure_local_api_key` stays
- `src/gobby/config/registry.py::*` — scope-reason: drops the `auth.api_token_hash` registration and adds the key to `_REMOVED_STORED_KEYS`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerated derived carrier of `src/gobby/config/`
- `src/gobby/cli/auth.py::*` — scope-reason: removes the `token` command
- `src/gobby/cli/install.py::*` — scope-reason: removes `_provision_local_api_token`; install keeps its `ensure_local_api_key` call when a hub database is reachable
- `tests/e2e/conftest.py::*` — scope-reason: `prepare_daemon_env` provisions a key row and bootstrap `api_key`, and `daemon_token` and `wait_for_daemon_websocket` read it
- `tests/servers/conftest.py::*` — scope-reason: the auth fixtures send front-door identity headers with the bound secret
- `tests/servers/test_auth_service.py::*` — scope-reason: bearer tests become front-door identity tests
- `tests/servers/test_auth_middleware.py::*` — scope-reason: same
- `tests/servers/test_http_middleware.py::*` — scope-reason: same
- `tests/servers/websocket/test_servers_websocket_auth.py::*` — scope-reason: same
- `tests/servers/test_break_glass.py`
- `tests/servers/routes/test_runtime_handshake.py::*` — scope-reason: the interactive challenge is refused and the handshake binds the forwarded machine
- `tests/servers/routes/test_runtime_config.py::*` — scope-reason: authenticates with front-door identity
- `tests/servers/routes/test_agents_routes.py::*` — scope-reason: same
- `tests/servers/routes/test_api_keys.py::*` — scope-reason: key routes resolve the principal from front-door identity
- `tests/servers/routes/test_configuration_routes.py::*` — scope-reason: drops `verify_bearer` use
- `tests/servers/routes/test_configuration_effective_routes.py::*` — scope-reason: uses front-door identity and another restricted key
- `tests/servers/routes/test_config_values_api.py::*` — scope-reason: its restricted-value case uses another registered restricted key
- `tests/servers/routes/test_hub_files_proxy.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/servers/test_mcp_programmatic_boundary.py::*` — scope-reason: authenticates with front-door identity
- `tests/servers/test_mcp_routes.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/servers/test_grant_auth.py::*` — scope-reason: `bearer_matches_grant` cases compare with the forwarded machine
- `tests/mcp_proxy/test_workspaces_registry.py::*` — scope-reason: builds `AuthService` without a token file
- `tests/storage/test_storage_auth.py::*` — scope-reason: drops the token-file and hash tests
- `tests/storage/test_managed_credentials.py::*` — scope-reason: `issue_for_operator` receives the forwarded machine
- `tests/storage/test_revisioned_config_store.py::*` — scope-reason: its restricted-key patch case uses another registered restricted key
- `tests/config/test_removed_config_fields.py::*` — scope-reason: gains 1.3.10
- `tests/runner_helpers.py::*` — scope-reason: drops the `ensure_local_api_token` patch
- `tests/test_runner_init.py::*` — scope-reason: the startup ordering case drops `ensure_local_api_token` and gains 1.3.16
- `tests/config/test_remote_ui_auth.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/utils/test_local_token.py::*` — scope-reason: the reader reads the bootstrap, with no token file
- `tests/utils/test_daemon_client.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/agents/test_agent_constants.py::*` — scope-reason: drops `local_token_path`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: drops `local_token_path`
- `tests/agents/test_isolation.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/agents/test_sandbox.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/agents/test_sandbox_policy.py::*` — scope-reason: the credential-root case no longer lists the token file
- `tests/cli/test_cli_auth.py::*` — scope-reason: drops the `gobby auth token` cases
- `tests/cli/test_cli_install.py::*` — scope-reason: drops `_provision_local_api_token`
- `tests/cli/test_install_coverage.py::*` — scope-reason: same
- `tests/cli/test_install_front_door.py::*` — scope-reason: same
- `tests/cli/test_install_prompts.py::*` — scope-reason: same
- `tests/cli/test_pack.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/cli/test_daemon_protected_runs.py::*` — scope-reason: the fixture reads the key from the bootstrap
- `tests/hooks/test_inbox.py::*` — scope-reason: imports of `_compute_sleep_seconds` and `prune_orphaned_inbox_temp_files` and the `_JITTER_RANDOM` patch move to `gobby.hooks.inbox_maintenance`; `read_local_api_token` patches keep their path
- `tests/hooks/test_inbox_temp_reaper.py::*` — scope-reason: imports and the `get_hook_inbox_dir` and `prune_orphaned_inbox_temp_files` patches move to `gobby.hooks.inbox_maintenance`
- `tests/hooks/test_envelope_marker_retention.py::*` — scope-reason: the `prune_hook_inbox` import and the `get_hook_inbox_dir` and `prune_processed_envelope_markers` patches move to `gobby.hooks.inbox_maintenance`
- `tests/terminals/acceptance/conftest.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/terminals/test_frame_client.py::*` — scope-reason: same
- `tests/terminals/test_native_runtime.py::*` — scope-reason: same, and `_frame_token` patches follow the mixin
- `tests/terminals/test_runtime_contract.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/test_runner_front_door.py::*` — scope-reason: asserts the child env carries the secret and a respawn reuses it
- `tests/e2e/test_daemon_auth.py::*` — scope-reason: authenticates with the provisioned key
- `tests/e2e/test_external_terminal_attach.py::*` — scope-reason: writes or reads the token file; provisions a bootstrap carrying `api_key` instead
- `tests/e2e/test_single_active_daemon.py::*` — scope-reason: same
- `tests/e2e/test_stateless_ambient_session.py::*` — scope-reason: same
- `tests/e2e/test_terminal_client_stack.py::*` — scope-reason: same
- `tests/e2e/composer_proof_live.py::*` — scope-reason: same
- `tests/e2e/test_composer_live_proof.py::*` — scope-reason: same
- `tests/workflows/test_placed_runbook_live.py::*` — scope-reason: same
- `tests/contracts/http/manifest.json::*` — scope-reason: `schema_version` advances to 2 and gains the native `runtime_challenge` family
- `tests/contracts/http/auth_missing_auth.json::*` — scope-reason: re-recorded at version 2; its body names `gobby auth login`
- `tests/contracts/http/auth_missing_grant.json::*` — scope-reason: re-recorded at version 2 against the key-based front door
- `tests/contracts/http/auth_forged_identity.json::*` — scope-reason: re-authored to forge only `X-Gobby-Project-Id` (Decision 10) and re-recorded at version 2
- `tests/contracts/http/runtime_handshake_challenge.json::*` — scope-reason: moves to the `runtime_challenge` family, answered natively by gdaemon at version 2
- `tests/contracts/http/health_ok.json::*` — scope-reason: re-recorded at version 2; only the version changes
- `tests/contracts/http/config_schema.json::*` — scope-reason: same
- `tests/contracts/http/config_values.json::*` — scope-reason: same
- `tests/contracts/http/tasks_list.json::*` — scope-reason: same
- `tests/contracts/http/runtime_handshake.json::*` — scope-reason: same
- `tests/contracts/http/front_door_backend_down.json::*` — scope-reason: advanced by hand to version 2, keeping `backend: down` and its body
- `tests/contracts/http/health_backend_down.json::*` — scope-reason: same
- `docs/contracts/secrets.md`
- `docs/contracts/identity-model.md`
- `docs/contracts/gterm-protocols.md`
- `docs/guides/http-endpoints.md`
- `docs/reference-audit/admin.json::*` — scope-reason: drops the `gobby auth token` operation, whose `auth.py` implementation this leaf deletes, and maps `gobby auth login` and `gobby auth key` unless #23484 has landed

**Granularity:** one leaf and one commit. The token file, its hash, and the
alias leave together, and any consumer left on the old credential would 401
between commits. The Rust reader rename and the fixture moves ride along for
the same reason: a fixture still writing the file fails once the readers read
the bootstrap. The three size splits ride in the same commit because the
credential edits land in those files and each split is a pure move. Guides and
examples are 1.5; the CLI is 1.4.

**Research context:** the plan of record's 4.3 design, re-swept at
`48071323c9`, with these corrections:
- The managed prefix is `gobby-agent-v1.` (Decision 3). `spawn_agent/_implementation.py`
  is unchanged and `_selection.py` is dropped (Decision 11).
  `agents/tmux/spawner.py` and `crates/gclient/tests/client_loop.rs` no longer
  reference the token.
- `daemon_auth_headers` keeps its agent identity headers. The plan of record
  dropped them, but they are `X-Gobby-Session-Id`,
  `X-Gobby-Caller-Project-Id`, `X-Gobby-Agent-Run-Id`, and
  `X-Gobby-Managed-Execution-Id`. gdaemon strips none of them, and
  `auth_service._agent_identity_matches` requires them. `DaemonClient` has no
  machine-id argument any more, so `hooks/factory.py` and
  `hooks/health_monitor.py` are unchanged.
- gdaemon serves both public ports (`serve.rs::run` binds `daemon_port` and
  `websocket_port`), so WebSocket upgrades also pass `FrontDoor::handle`.
  gdaemon already depends on `deadpool-postgres` and `tokio-postgres`
  (the Rust lease).
- `HubDatabaseBootstrap` (`crates/gcore/src/bootstrap.rs`) derives `Debug` and
  is used only by `bootstrap.rs` and gdaemon's `serve.rs`. gcore and gcode pin
  `serde_yaml = { package = "yaml_serde", version = "0.10.4" }`.
- `serve` callers: `serve.rs::run`, `tests/common/mod.rs` (two), `tests/heartbeat.rs`,
  and `tests/http_contracts.rs::start_front_door`. `tests/front_door.rs`
  spawns the `serve` binary twice (`spawn_serve`, `spawn_tls_serve`).
- The Rust corpus harness sends only each case's explicit headers and requires
  the stub backend to see them unchanged (Decision 10).
  `tests/contracts/http_corpus.py::load_cases` returns only Python-origin
  backend-up cases, and both loaders reject a case whose `schema_version`
  differs from the manifest's. `runtime_handshake_challenge.json` sends no
  `kind`, which the Python route defaults to `interactive`.
- Consumers unchanged: `src/gobby/servers/routes/__init__.py` and
  `src/gobby/runtime_grants/__init__.py` re-export by name;
  `src/gobby/storage/config_repository.py` and `src/gobby/storage/config_store.py`
  already delete every key in `_REMOVED_STORED_KEYS` before the fail-closed
  resolve (#22839); `tests/storage/test_config_store.py` and
  `tests/integration/config/` seed other keys;
  `src/gobby/agents/sandbox.py`, `sandbox_resolvers.py`, and
  `external_write_grants.py` call `sensitive_*_roots` unchanged;
  `tests/contracts/http_corpus.py` and `tests/contracts/test_http_corpus.py`
  read cases as data; `tests/e2e/test_qa_23120_tmux_address.py` passes
  `daemon_token` to gclient's explicit `--token-file`;
  `src/gobby/daemon_lease_control.py` and `src/gobby/runner.py` keep the
  standby bearer check (Decision 12).
- Sizes: `inbox.py` 983, `native_runtime.py` 911 (`NativeTerminalRuntime` spans
  lines 237–911), and `crates/gcore/src/grant/acquisition.rs` 997 with no
  inline test module. All other targeted production files are below 850
  lines; gterm `state.rs` and `write.rs` count 759 and 517 before
  `#[cfg(test)]`.

**Implementation:**
- **gdaemon key validation** (`front_door/auth.rs`). Dispatch on the bearer:
  - `gobby_`: `api_key_format::parse` (bad checksum is 401 `missing_auth` with no
    database hit), SHA-256 hex, then one `SELECT` on `api_keys` joined to
    `machines` where `revoked_at IS NULL`, through a pool built from
    `database_url` the way the lease builds its pool. On success, strip client
    `X-Gobby-User-Id`, `X-Gobby-Machine-Id`, `X-Gobby-Key-Id`, and
    `X-Gobby-Front-Door`, then set all four. Touch `last_used_at` at most once a
    minute per key id. A query or pool error is 503 `key_resolver_unavailable`
    (Decision 5). With no `database_url` (node mode) every `gobby_` bearer is
    503 `key_resolver_unavailable`; node validation arrives with the node
    channel (#23274, Node channel, relay backend, and `/api/machines`).
  - `gobby-agent-v1.`: pass through with client identity headers stripped.
  - Any other bearer: 401 `missing_auth` without a database hit.
  - No bearer (cookies, public paths, break-glass): pass through with client
    identity headers stripped. `Authorization` and `X-Gobby-Break-Glass` are
    forwarded untouched.
  - The same function authenticates WebSocket upgrades before `ws::splice`.
  - Failures use the existing body `{"error": msg, "code": code}`.
- **`crates/gcore/src/api_key_format.rs`** mirrors
  `src/gobby/utils/api_key_format.py` (prefix `gobby_`, 43-character body,
  6-character checksum, `hash` is SHA-256 hex) and passes 4.2's shared
  vectors.
- **Native interactive challenge** (`front_door/challenge.rs`). gdaemon answers
  `POST /api/runtime/handshake/challenge` itself when the effective kind is
  `interactive`, and keeps the Python route's contract
  (`src/gobby/servers/routes/runtime_handshake.py::create_runtime_handshake_router`):
  - gdaemon matches the challenge path before bearer dispatch. A request
    carrying `Authorization` is refused before any proof with 401
    `{"code": "credential_before_proof", "error": "credential_before_proof", "message": …}`.
  - The effective kind is `caller.kind` when `caller` is present, else `kind`,
    which defaults to `interactive`. `managed` is forwarded to Python. Any
    other kind is 403 `claims_mismatch`, as Python's `challenge_proof`
    answers it.
  - The body rejects unknown fields, and a nonce over 44 characters is
    refused. The nonce is base64url-decoded with optional padding, and an
    undecodable nonce is 400.
  - The proof is the hex `HMAC-SHA256(api_key, decoded nonce)`, with `api_key` read
    from the bootstrap on every request (Decision 7). A missing bootstrap key
    is 503 `key_resolver_unavailable`.
  - The response body `{"proof": …}` and headers (`cache-control: no-store`,
    `content-type: application/json`) match.

  gcore's `challenge_and_handshake` is unchanged: the bearer
  `interactive_bearer` hands it is now the bootstrap key, so the proof stays
  `HMAC(bearer, nonce)`. Grant acquisition stays loopback-only
  (`crates/gcore/src/grant/handshake.rs` refuses a non-loopback endpoint), so
  a node acquires grants only once #23274 (Node channel, relay backend, and
  `/api/machines`) relays them. 1.3.4 proves that the hub validates a node
  grant against the forwarded machine; node acquisition is out of scope.
- **Per-boot secret.** `FrontDoorChild.__init__` sets
  `self.secret = secrets.token_urlsafe(32)`; `_popen` adds
  `GOBBY_FRONT_DOOR_SECRET` to the child env, and a respawn reuses it.
  `serve.rs::run` refuses to start when the variable is missing or empty
  (Decision 6). `_bind_runtime_grants` passes
  `runner.front_door_child.secret` (or `None`) to
  `AuthService.bind_runtime(front_door_secret=…)`. 5.2 of the plan of record
  later moves generation into gdaemon.
- **Python trusts the front door.** `AuthService` gains
  `_front_door_identity(request)`, which returns user, machine, and key ids
  only when `X-Gobby-Front-Door` equals the bound secret (constant time) and
  all three headers are non-empty. Order in `_legacy_rejection` and
  `_accepted_bearer`: break-glass (1.1), front-door identity (operator),
  managed capability (1.2), `gobby_session` cookie. Remove `token_file`,
  `verify_bearer`, `verify_ws_token`, `refresh`, `_token_hash_snapshot`,
  `local_token`, `_read_token_file`, and the alias. `AuthDecision` carries the
  forwarded ids. WebSocket: `auth_callback` becomes
  `verify_ws_identity(headers) -> str | None`, wired in `init_servers`.
  `bearer_matches_grant` compares an interactive principal's `machine_id` with
  the forwarded machine instead of the daemon's local machine id, so a node
  user's grant validates at the hub. `issue_for_operator` binds `machine_id`
  to the forwarded machine and rejects a body naming another. The Python
  challenge route refuses `kind: interactive` with 400
  `interactive_challenge_is_native`. `routes/api_keys.py::_resolve_principal`
  reads the principal from `_front_door_identity` (Decision 8); the cookie
  path is unchanged. With `front_door.enabled: false`, Python accepts only
  break-glass, cookies, and managed capabilities.
  `FrontDoorChild.from_bootstrap`'s missing-gdaemon `FrontDoorStartupError`
  says so: it tells the operator to run `gobby install`, and that
  `front_door.enabled: false` admits only the break-glass header, cookies,
  and managed capabilities, so API-key bearers are refused.
- **Readers.** As the plan of record: gcore's three readers become
  `read_api_key`, `read_api_key_for`, and `read_api_key_at` and read `api_key`
  from the bootstrap; every caller follows. gclient keeps `--token-file` as an
  explicit key file with no default. gcode's 401 remediation and ghook's
  `AUTH_401_REMEDIATION` name `gobby auth login`. ghook's diagnose field
  becomes `api_key_present` in both schema copies, with no schema bump. Python
  `read_local_api_token` keeps its name and reads `api_key` from
  `daemon_bootstrap_path()` (1.1);
  `local_token_path` is removed with its consumers. `frame_client.py` and
  `_frame_token` call `read_local_api_token()`. gterm reads the key on every
  frames hello, `HostState` drops its cached token, and gterm gains the
  workspace's `yaml_serde` package at the version gcore pins to read the one
  field (a gcore dependency was rejected: it pulls the HTTP and grant stack
  into the terminal host). `remote_preflight.py` authenticates with the
  bootstrap key. `gobby auth token` and `_provision_local_api_token` are
  removed. `credential_roots()` drops `local_cli_token`. The reaper in
  `code_index.py` keeps `.secret_kek`.
- **Stored-row retirement.** Drop the `auth.api_token_hash` registration,
  add the key to `_REMOVED_STORED_KEYS` (the union with the existing `memory.*`,
  `tmux.*`, and `terminals.default_backend` entries), and regenerate
  `runtime_config_contract.json`. Startup reconciliation deletes the stored row
  before its fail-closed resolve. A data migration was rejected (Decision 9).
  `open_storage_and_config` calls `ensure_local_api_token` after that
  reconciliation (`src/gobby/runner_init/storage.py`), and the function writes
  both the token file and the row. The call, the function, `rotate_local_api_token`,
  and `_write_new_local_api_token` are deleted, so startup provisions only the
  API key.
- **Split `src/gobby/hooks/inbox.py`** (983 lines) before its credential edit:
  move `drain_hook_inbox_loop`, `_compute_sleep_seconds`, `_JITTER_RANDOM`,
  `prune_hook_inbox`, `_prune_hook_inbox_blocking`,
  `prune_orphaned_inbox_temp_files`, `_is_orphaned_temp_name`,
  `ORPHANED_TEMP_RETENTION_SECONDS`, and `ORPHANED_TEMP_PRUNE_MAX_ENTRIES` into
  the new `src/gobby/hooks/inbox_maintenance.py`, which imports
  `get_hook_inbox_dir` and `drain_hook_inbox_once` from `inbox.py` (no cycle).
  `runner_maintenance/messaging.py` and the three hook tests import and patch
  there. `inbox.py` ends near 845 lines.
- **Split `src/gobby/terminals/native_runtime.py`** (911 lines): move the
  frame-stream methods `_socket_dir`, `_frame_token`, `_open_frame_stream`,
  `_bind_frames`, `_forget_stream`, `close_frame_streams`, `reserve_observer`,
  `release_observer`, and `bind_observer` into a `NativeFrameStreamMixin` in
  the new `src/gobby/terminals/native_frames.py`, following the
  `NativeHostProbeMixin` precedent. `NativeTerminalRuntime` inherits it, so
  callers and `patch.object` targets keep working. The file ends near 790
  lines.
- **Split `crates/gcore/src/grant/acquisition.rs`** (997 lines): move the
  managed path, `acquire_managed`, `matches_managed_principal`,
  `managed_envelope`, `handshake_managed`, `validate_managed_refresh`,
  `handshake_managed_once`, `managed_bootstrap_path`, and
  `validate_managed_identity`, into the child module
  `crates/gcore/src/grant/acquisition/managed.rs` (`mod managed;`), which sees
  the parent's private helpers through `super::`. The file ends near 815
  lines.
- **Corpus.** The manifest and every case advance to `schema_version` 2.
  Re-record the Python-origin `up` cases with `GOBBY_RECORD_HTTP_CONTRACTS=1`
  against the isolated test hub. Re-author `auth_forged_identity.json`
  (Decision 10). Move `runtime_handshake_challenge.json` into a new
  `runtime_challenge` family (`parity: native`, `origin: gdaemon`), which the
  Python loader skips and the Rust harness replays against gdaemon's native
  answer with a test bootstrap key (the proof stays masked). Edit the two
  `down` cases by hand, keeping `backend: down` and their bodies.
- **Docs.** `secrets.md` replaces the Daemon API Token table with the API key
  (bootstrap field, SHA-256 in `api_keys.key_hash`, bearer header, rotation via
  `gobby auth key --rotate`). `identity-model.md` records the key row as the
  one new seam. `gterm-protocols.md` names the frames credential.
  `http-endpoints.md` gives credential precedence (break-glass, bearer key,
  managed capability, cookie). The skill reference `authentication.md` follows.
- **Reference audit.** `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`
  checks every `docs/reference-audit/*.json` operation against the Click
  registry and its implementation symbol. `admin.json` drops the
  `gobby auth token` operation (its `auth.py::token` implementation goes). On
  `0.5.0` at `b9afc3b` the test already fails on the unmapped
  `gobby auth login` and `gobby auth key`, added by #23272 (`gobby auth login`
  and `gobby auth key --show`). The Orchestrator filed that repair as #23484
  ("Reference audit leaves gobby auth login and gobby auth key unmapped,
  failing test_reference_contract_3_2_1 on 0.5.0"). If #23484 has landed, this
  leaf keeps its entries. Otherwise it maps both to `authentication.md`, with
  `auth_login.py` symbols `login` and `key` and the `source-inventory` and
  `cli-registry` evidence.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/ tests/storage/test_storage_auth.py tests/storage/test_managed_credentials.py tests/storage/test_revisioned_config_store.py tests/config/ tests/utils/ tests/agents/test_agent_constants.py tests/agents/test_spawn_executor.py tests/agents/test_isolation.py tests/agents/test_sandbox.py tests/agents/test_sandbox_policy.py tests/cli/ tests/hooks/ tests/terminals/ tests/mcp_proxy/test_workspaces_registry.py tests/test_runner_front_door.py tests/test_runner_init.py tests/contracts/ tests/skills/test_reference_library.py::test_reference_contract_3_2_1 -q`.
Heavy work: `cargo nextest run -p gdaemon -p gobby-core -p gclient -p gcode -p ghook -p gterminal` and
`cargo clippy --workspace --all-targets -- -D warnings`. The e2e files run in
V2.

**Acceptance:**

- 1.3.1 - gdaemon resolves a valid key to user, machine, and key ids, strips client identity headers, forwards the ids with the front-door secret, passes `gobby-agent-v1.` bearers through, and rejects revoked keys with 401 and malformed or unknown-kind bearers with 401 without a database hit, on HTTP and on WebSocket upgrades. test: `crates/gdaemon/tests/front_door.rs::valid_key_forwards_identity_headers`.
- 1.3.2 - Python accepts the operator principal only with the matching front-door secret on HTTP and WebSocket, and rejects the retired token, the `X-Gobby-Local-Token` alias, and identity headers with a wrong or missing secret. test: `tests/servers/test_auth_service.py::test_front_door_identity_requires_secret`.
- 1.3.3 - gdaemon answers the interactive challenge locally from a per-request bootstrap read, so a rewritten bootstrap key applies to the next challenge, and forwards the managed challenge to Python. It keeps the Python contract: `credential_before_proof` for a request carrying `Authorization`, padded and unpadded base64url nonces decoded before the HMAC, an overlong or undecodable nonce refused, `caller.kind` taking precedence over `kind`, and an unknown kind refused. test: `crates/gdaemon/tests/front_door.rs::interactive_challenge_answered_locally`.
- 1.3.4 - A node user's interactive grant validates at the hub against the forwarded machine. test: `tests/servers/test_grant_auth.py::test_interactive_grant_matches_forwarded_machine`.
- 1.3.5 - No reference to `local_cli_token`, `LOCAL_CLI_TOKEN`, `X-Gobby-Local-Token`, or `auth.api_token_hash` remains under `src/` or `crates/` or in the docs this leaf targets, except the `_REMOVED_STORED_KEYS` entry and `crates/CHANGELOG.md` history (literal sweep recorded in the leaf). behavior: "API key" in `docs/contracts/secrets.md`.
- 1.3.6 - The manifest and every case carry `schema_version` 2, the auth cases are re-recorded, the forged-identity case forges only `X-Gobby-Project-Id`, the two `down` cases keep `backend: down`, and every Python-origin `up` case replays equal in Python. test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.
- 1.3.7 - The Rust harness loads the version-2 corpus and replays every case, including the native `runtime_challenge` case, in its declared backend state. test: `crates/gdaemon/tests/http_contracts.rs::cases_replay_in_declared_backend_state`.
- 1.3.8 - `src/gobby/hooks/inbox.py` is below 850 lines after the move, and the moved loop still drains and prunes on its own cadence. file: `src/gobby/hooks/inbox_maintenance.py`.
- 1.3.9 - The Rust key-format helper matches 4.2's shared vectors. test: `crates/gcore/src/api_key_format.rs::matches_shared_vectors`.
- 1.3.10 - Startup on an install that still stores `auth.api_token_hash` deletes that row before fail-closed resolution, and a different unknown stored key still refuses. test: `tests/config/test_removed_config_fields.py::test_retired_auth_token_hash_row_is_deleted`.
- 1.3.11 - After the bootstrap key changes, the next frames hello with the new key is accepted and one with the old key is refused with `invalid_token`, an attached connection keeps streaming, and an empty or unreadable bootstrap key is refused. test: `crates/gterminal/tests/frame_protocol.rs::rotated_key_applies_to_next_hello`.
- 1.3.12 - With gdaemon's key resolution failing, a loopback request carrying only the break-glass header passes the front door unchanged and Python admits it with no front-door identity. test: `crates/gdaemon/tests/front_door.rs::break_glass_header_passes_through_when_resolver_fails`.
- 1.3.13 - A resolver database error answers 503 `key_resolver_unavailable`, and gdaemon refuses to start without `GOBBY_FRONT_DOOR_SECRET`. test: `crates/gdaemon/tests/front_door.rs::resolver_error_is_503_and_missing_secret_refuses_start`.
- 1.3.14 - The API-key routes take the principal from verified front-door identity, ignore unverified identity headers, and list or revoke only the caller's keys. test: `tests/servers/routes/test_api_keys.py::test_key_routes_use_verified_front_door_identity`.
- 1.3.15 - `src/gobby/terminals/native_runtime.py` and `crates/gcore/src/grant/acquisition.rs` are below 850 production lines after their moves. file: `src/gobby/terminals/native_frames.py`.
- 1.3.16 - `open_storage_and_config` provisions the API key and neither writes `local_cli_token` nor stores `auth.api_token_hash`. test: `tests/test_runner_init.py::test_startup_provisions_no_operator_token`.

### 1.4 `gobby auth key` rotates, lists, revokes, and mints keys [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/auth_login.py::*` — scope-reason: `key` becomes a click group with `--show`, `--rotate`, and `--mint LABEL`, plus the `list` and `revoke` subcommands
- `tests/cli/test_auth_login.py::*` — scope-reason: gains the rotation, list and revoke, and mint cases; the `--show` case stays
- `src/gobby/install/shared/skills/gobby/references/admin/authentication.md`
- `docs/reference-audit/admin.json::*` — scope-reason: maps the `gobby auth key` group, `gobby auth key list`, and `gobby auth key revoke`

**Research context:**
- `key` (`src/gobby/cli/auth_login.py`, 389 lines) is a `click.command` with
  only `--show`, which prints `api_key_format.hint(api_key)` and `api_key_id`
  from the bootstrap. `src/gobby/cli/auth.py` registers it with
  `auth.add_command(key)`, which takes a group unchanged. `KEY_CLEANUP` already
  names `gobby auth key list`.
- Routes (`src/gobby/servers/routes/api_keys.py`, prefix `/api/auth/keys`):
  `POST` mints a key with `label` for the caller's machine and answers
  `key`, `key_id`, `hint`, `user_id`, and `machine_id`; `GET` lists the caller's
  keys (`id`, `hint`, `label`, `machine_id`, `created_at`, `last_used_at`,
  `revoked_at`); `DELETE /{key_id}` revokes one of the caller's keys and
  answers 404 for any other. After 1.3 the caller is the bearer key's
  front-door identity. `GET /api/auth/status` answers `{"authenticated": bool}`.
- `enroll` shows the client shape: `httpx.Client(base_url=…, verify=…,
  timeout=NETWORK_TIMEOUT_SECONDS, trust_env=False)`, pinned to the bootstrap's
  `hub_cert` on a node. `_publish` and `_settle` publish under
  `exclusive_file_lock` with `publish_bootstrap_yaml_locked` and settle a
  failure by whether the bootstrap names the new key id. `_verified` checks a
  mint response's machine and key id.
- On a local hub, `storage/api_keys.py::ensure_local_api_key` keeps a bootstrap
  key that is live for the machine, so a rotated key survives the next start.

**Implementation:**
- `key` becomes `@click.group("key", invoke_without_command=True)` with the
  mutually exclusive options `--show` (unchanged), `--rotate`, and
  `--mint LABEL`. With no option and no subcommand it prints usage.
- Every call sends the bootstrap `api_key` as the bearer. A bootstrap with
  `datastore_mode: remote` dials `hub_daemon_url`, verified against `hub_cert`;
  a local one dials `src/gobby/utils/daemon_url.py::daemon_url()`. Both use
  `trust_env=False`.
- `--rotate`, in order:
  1. `POST /api/auth/keys` with the old key and label
     `socket.gethostname()`, checked with `_verified`.
  2. Under the bootstrap lock, re-read the bootstrap. If it no longer names
     the old key id, another writer changed the key: revoke the new id with
     the old key and fail. Otherwise set `api_key` and `api_key_id` in the
     re-read mapping and publish it. A failed publish settles as `_settle`
     does, and an unpublished key is revoked with the old key.
  3. `GET /api/auth/status` with the new key must answer
     `authenticated: true`. Otherwise, under the lock, re-read the bootstrap.
     If it still names the new id, put back only the old `api_key` and
     `api_key_id` and publish, keeping every other field as it now stands,
     then revoke the new id with the old key and fail. If another writer has
     replaced the key, leave the bootstrap and both keys alone and fail with
     the new id and `KEY_CLEANUP`. The old key is never revoked on this path.
  4. `DELETE /api/auth/keys/<old id>` with the new key. A failure prints the
     old id and `KEY_CLEANUP`, and the rotation stands.

  It prints the new hint and id, never the key. Its help text says that
  rotation invalidates outstanding managed capability tokens (Decision 1).
- `--mint LABEL` posts the label, prints the key, its hint, and its id once,
  with a line saying the key is not shown again, and leaves the bootstrap
  byte-identical (ruling (b)).
- `list` prints one line per key: id, hint, label, machine, created, last
  used, revoked, and a marker on the bootstrap's own key.
- `revoke KEY_ID` deletes the key. It refuses the bootstrap's own
  `api_key_id` and names `--rotate`, so the CLI cannot lock itself out. A 404
  is "not found among your keys".
- `authentication.md` documents `--rotate`, `--mint LABEL`, `list`, and
  `revoke`. Because the group takes `invoke_without_command=True`, the
  reference-library inventory counts `gobby auth key` and both subcommands as
  public commands. `admin.json` maps each one to `authentication.md`, with
  the module-level function in `auth_login.py` that defines it as an
  unqualified symbol (the audit resolves a dotted symbol through nested
  bodies, which click subcommands are not), updating the
  `gobby auth key` entry that 1.3 left. Each
  carries `source-inventory`, `cli-registry`, and a new `focused-auth-key`
  test evidence item recording this leaf's focused pytest command and result.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_auth_login.py tests/cli/test_cli_auth.py tests/skills/test_reference_library.py::test_reference_contract_3_2_1 -q`,
then `uv run ruff check` and `uv run mypy` on `src/gobby/cli/auth_login.py`.

**Acceptance:**

- 1.4.1 - `--rotate` mints with the old key, publishes, verifies the new key, and only then revokes the old one. When verification fails, only the key fields are restored, a field another writer changed meanwhile survives, the new key is revoked, and the old key is never revoked. When another writer replaced the key during the rotation, `--rotate` leaves that writer's key and the bootstrap untouched. test: `tests/cli/test_auth_login.py::test_rotate_verifies_before_revoke`.
- 1.4.2 - `list` and `revoke` send the bootstrap key as the bearer, show only the caller's keys, report a foreign id as not found, and refuse to revoke the bootstrap's own key. test: `tests/cli/test_auth_login.py::test_key_list_and_revoke_send_the_bootstrap_key`.
- 1.4.3 - `--mint LABEL` prints the new key once and leaves the bootstrap byte-identical. test: `tests/cli/test_auth_login.py::test_key_mint_prints_once_and_writes_nothing`.
- 1.4.4 - The rotation and mint options are mutually exclusive with `--show`, and `--show` still prints only the hint and id. test: `tests/cli/test_auth_login.py::test_key_show_prints_hint_only`.
- 1.4.5 - The admin reference audit maps `gobby auth key`, `gobby auth key list`, and `gobby auth key revoke` to `authentication.md` with resolvable implementation symbols, and maps no `gobby auth token`. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.

### 1.5 Guides and examples describe the API key [category: docs] (depends: 1.3, 1.4)
`kind: deliverable`

Targets:
- `docs/guides/cli-commands.md`
- `docs/guides/comm-integrations.md`
- `docs/guides/gclient-user-guide.md`
- `docs/guides/gterminal-development-guide.md`
- `docs/guides/ghook-development-guide.md`
- `docs/guides/ghook-user-guide.md`
- `docs/guides/hub-install-contract.md`
- `docs/guides/observability.md`
- `docs/guides/system-requirements.md`
- `docs/guides/telegram.md`
- `docs/guides/web-ui.md`
- `docs/guides/shared-stack.md`
- `docs/guides/admin-operations.md`
- `docs/examples/observability/README.md`
- `docs/examples/observability/compose.yaml::*` — scope-reason: the Prometheus volume mounts the operator-written key file in place of the token file
- `docs/examples/observability/prometheus.yml::*` — scope-reason: the header comment names the key file
- `docs/architecture/sandbox-hub-credential-isolation.md`
- `docs/architecture/gobby-v1.0.0-roadmap.md`
- `docs/reference-audit/admin.json::*` — scope-reason: the admin guide's audited anchor list follows the renamed rotation heading

These are the plan of record's 4.6 list plus `shared-stack.md` and
`admin-operations.md`, which the consumer sweep found, and the admin audit
that pins the guide's anchors. No runtime reads them, so they close after the
code leaves. The 1.5.1 sweep's remaining hits on `0.5.0` at `b9afc3b` belong
to 1.3: `docs/contracts/secrets.md`, `docs/contracts/identity-model.md`,
`docs/contracts/gterm-protocols.md`, `docs/guides/http-endpoints.md`, and
the `gobby auth token` operation in `admin.json`. Every other hit is excluded
below.
- Every instruction to read or copy `~/.gobby/local_cli_token` becomes the
  bootstrap `api_key` that `gobby auth login` writes. `shared-stack.md`'s node
  setup drops the `scp` of the token file for `gobby auth login`, and its
  backup advice names `bootstrap.yaml` in place of the token file.
- `cli-commands.md` drops `gobby auth token` and documents the `gobby auth key`
  group (1.4).
- `admin-operations.md`:
  - "Rotate The Local Token" becomes "Rotate The API Key". The procedure uses
    `gobby auth key --rotate` and says that rotation invalidates outstanding
    managed capability tokens, so spawned agents restart after it.
    `admin.json` replaces the `rotate-the-local-token` anchor with
    `rotate-the-api-key`, and follows any other audited heading this leaf
    renames.
  - It gains "Flag-day gterm activation" with the plan of record's seven steps:
    1. A global BEFORE notice.
    2. A quiet window with no live spawned worker or close validator.
    3. A restore manifest under `~/.gobby/` (per native terminal: terminal id,
       cwd, launch command, bound session ref, seat).
    4. The host's `host_shutdown`.
    5. Promotion of gterm with the coherent set, then a daemon restart.
    6. Owner restoration from the manifest, each owner proving its binding to
       the Orchestrator.
    7. A global DAEMON BACK notice.

    Later rotations need no drain, because gterm reads the key on every hello
    (1.3.11).
- The gclient guides describe `--token-file` as an explicit key file with no
  default. The ghook guides rename `local_token_file_present` to
  `api_key_present`.
- The observability example authenticates Prometheus with a dedicated key from
  `gobby auth key --mint prometheus`, written by the operator to an owner-only
  file that the compose volume mounts. `--rotate` does not invalidate that key
  (ruling (b)).
- The sandbox credential-isolation note drops the token file from the denied
  roots and adds `break_glass`. The roadmap's S1.5 row drops the
  `X-Gobby-Local-Token` alias.
- Excluded as dated history: `docs/evidence/**`, `docs/research/**`,
  `docs/plans/abandoned/**` (abandoned plans),
  `docs/architecture/sandbox-hub-credential-isolation-validation.md` (the
  #19562 validation record), `ROADMAP.md`, `CHANGELOG.md`, and
  `crates/CHANGELOG.md`.

**Focused verification (planned):**
`DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_reference_library.py::test_reference_contract_3_2_1 -q`,
then the 1.5.1 sweep.

**Acceptance:**

- 1.5.1 - `gcode --allow-stale grep 'local_cli_token|LOCAL_CLI_TOKEN|X-Gobby-Local-Token|api_token_hash|gobby auth token|local_token_file_present' docs/ -l -m1000`, run after 1.3 and recorded in the leaf, lists only paths under the excluded dated paths. behavior: "gobby auth login" in `docs/guides/system-requirements.md`.
- 1.5.2 - The admin guide carries the seven-step flag-day gterm activation procedure. behavior: "Flag-day gterm activation" in `docs/guides/admin-operations.md`.
- 1.5.3 - The observability example authenticates Prometheus with a dedicated minted key in an owner-only file. behavior: "gobby auth key --mint" in `docs/examples/observability/README.md`.
- 1.5.4 - The admin guide's rotation procedure uses the key command and states the capability-token consequence. behavior: "gobby auth key --rotate" in `docs/guides/admin-operations.md`.

### 1.6 Rollback rehearsal keeps the operator a way in [category: test] (depends: 1.3, 1.4, 1.5)
`kind: deliverable`

Targets:
- `docs/evidence/gdaemon-key-cutover-rollback-rehearsal.md`

This is api-keys D1.13 at the head schema (Decision 9). It runs against an
isolated copy and never touches the running daemon, `~/.gobby`, the
operator's `~/.gobby/bin`, or port 60891. A rollback check proves something
only when both the Python runtime and the native binaries come from the
source being reverted, so every step binds both.

**Research context:**
- `tests/e2e/conftest.py` holds the isolated-daemon arrangement:
  - `e2e_config` picks two free public ports whose backend partners
    (port + 100) are also free. It writes `bootstrap.yaml` and `config.yaml`
    into the per-test home (`e2e_home_dir`, `<project>/.gobby-home`) and points
    `database_url` at the session's isolated schema on the test hub
    (`tests/fixtures/postgres.py::postgres_schema`, dropped at session end).
  - `spawn_daemon_instance` runs `sys.executable -m gobby.runner --config …`
    with `prepare_daemon_env(home_dir=<home>)` and `GOBBY_HOME=<home>`, with
    plaintext loopback on the public port. The `daemon_instance` fixture
    wraps it and stops the daemon at teardown.
  - `prepare_daemon_env` prepends `<conftest's checkout>/src` to `PYTHONPATH`
    and forces `GOBBY_TEST_PROTECT=1`. It pins `GOBBY_NATIVE_BIN_DIR` before it
    moves `HOME`. Left unset, the pin is the calling process's
    `native_bin_dir()`, which `src/gobby/utils/native_bin.py` resolves from
    `GOBBY_NATIVE_BIN_DIR`, else `Path.home() / ".gobby" / "bin"`: the
    operator's install. `GOBBY_TEST_GDAEMON=checkout`
    (`tests/fixtures/gdaemon_binary.py::select_test_gdaemon`) instead links the
    checkout's `target/debug/gdaemon` over the pinned set, while `installed`
    keeps the pin.
- `tests/conftest.py::_select_schema_contract_gdaemon` applies the test schema
  through the same selection, so the schema probe follows the pin too.
- `promote_workspace_binary_set(candidates, bin_dir=…)` signs and promotes
  `gcode`, `gdaemon`, and `ghook`, and writes `.gdaemon-schema-identity.json`
  for the complete set. Without `gterm` in the bin dir the daemon still
  starts, logging that native launches are degraded; the rehearsal launches
  none.
- `GET /api/admin/status` is protected: it sits under the `/api/` prefix and
  outside `_PUBLIC_PATHS` in `src/gobby/servers/middleware/auth.py`. It is not
  a grant route, since `src/gobby/servers/grant_auth.py::match_grant_route`
  lists no entry for it.

**Implementation:**
1. Make `<scratch>` with `mktemp -d` under the session scratchpad. Clone the
   lane branch tip holding 1.2 to 1.5 into `<scratch>/clone`. A clone, not a
   worktree, so the daemon's worktree guard does not apply. Record the tip
   SHA and the SHA of every commit linked to the 1.4, 1.3, and 1.2 tasks.
2. Run every later command from `<scratch>/clone` in one environment:
   - unset `PYTHONPATH`, `VIRTUAL_ENV`, `UV_PROJECT_ENVIRONMENT`, `GOBBY_HOME`,
     and `GOBBY_CONFIG`;
   - `GOBBY_NATIVE_BIN_DIR=<scratch>/bin` and `GOBBY_TEST_GDAEMON=installed`;
   - `CARGO_TARGET_DIR=<scratch>/target`;
   - `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
     and `GOBBY_TEST_PROTECT=1`.

   `uv run` then uses the clone's own environment and `conftest.py`, and
   `prepare_daemon_env` keeps `<scratch>/bin` for every daemon start and
   schema probe. The per-test home is created inside the run, so the bin dir
   sits beside it in `<scratch>`.
3. Build with `cargo build -p gobby-daemon -p gobby-code -p gobby-hooks`
   (heavy work). Promote the three `<scratch>/target/debug` artifacts with
   `uv run python -c` calling
   `promote_workspace_binary_set({...}, bin_dir=Path("<scratch>/bin"))`.
   Record `shasum -a 256` of the three files in `<scratch>/bin` and the
   identity stamp's contents.
4. Write an uncommitted scratch test module next to the clone's
   `tests/e2e/conftest.py` and run it once with `uv run pytest <module> -q`.
   Its one test takes `daemon_instance`, so one isolated home, one schema,
   and one port pair live from the tip start through the reverted start; the
   fixtures drop the schema and remove the temporary project and home only
   after the test returns. The test writes a JSON record to `<scratch>`
   holding, for each phase:
   - `gobby.__file__`, under `<scratch>/clone/src`, and the daemon env's
     `PYTHONPATH`, which must begin with `<scratch>/clone/src`;
   - `local_native_bin_path("gdaemon")` and `installed_schema_identity()`,
     naming `<scratch>/bin/gdaemon` and `latest_version` 459;
   - the `exe()` of each gdaemon process among the runner's
     `psutil.Process(pid).children(recursive=True)`, which must be
     `<scratch>/bin/gdaemon`;
   - each check's command and status code, with every credential redacted.
5. Tip phase, inside the test: `GET /api/admin/status` with
   `Authorization: Bearer <bootstrap api_key>` answers 200,
   `<home>/break_glass` has mode 0600 and its SHA-256 is recorded, and no
   `<home>/local_cli_token` exists. Then `daemon_instance.stop()`.
6. Revert, inside the test, as recorded subprocesses: `git revert --no-edit`
   the recorded 1.4, 1.3, and 1.2 commits, newest first; the 1.5 docs commit
   stays. A conflict confined to `docs/` keeps the 1.5 text, and any other
   conflict fails the rehearsal. Record the reverted HEAD SHA. Rebuild and
   promote as in step 3, and record the new hashes and stamp. The `gdaemon`
   hash must differ from the tip's. The scratch module is untracked, so the
   revert leaves it in place.
7. Reverted start, inside the test: `subprocess.Popen` reruns
   `daemon_instance.command` with `daemon_instance.env`, its project directory
   as `cwd`, and `start_new_session=True`. It keeps the same home, ports, and
   schema, so the reverted code starts on the tip's bootstrap, stored config,
   API-key rows, and break-glass file at schema 459. The test waits with
   `wait_for_daemon_health`, an unauthenticated `/api/auth/status` probe.
   `DaemonInstance.restart()` is not used, because its websocket wait
   authenticates with the bootstrap key, which the reverted Python does not
   accept. Then it runs, as subprocesses:
   - `curl -sS -o /dev/null -w '%{http_code}' -H "X-Gobby-Break-Glass: <contents of <home>/break_glass>" http://127.0.0.1:<public port>/api/admin/status`,
     expecting 200;
   - the same command without `-H`, expecting 401.

   `<home>/break_glass` must still match the tip's SHA-256. A `finally` block
   stops the reverted daemon with `terminate_process_tree`. Non-loopback
   refusal is proven by 1.1.2 and 1.3.12.
8. Write the evidence doc under the heading "Break-glass admitted after
   revert". It holds:
   - the tip, reverted-commit, and reverted HEAD SHAs;
   - both phases' records, hashes, and stamps;
   - the break-glass SHA-256 match;
   - the commands and the status codes.
9. Clean up. After pytest exits, `pgrep -f '<scratch>/bin/'` must find
   nothing. Then remove `<scratch>`. Record both in the evidence doc.

**Acceptance:**

- 1.6.1 - After the cutover code is reverted on the same isolated home and schema the tip ran on, a daemon whose Python imports resolve under the reverted clone's `src` and whose running gdaemon is the reverted build promoted to the isolated bin dir starts at schema 459. It admits a loopback break-glass `GET /api/admin/status` with the tip's byte-identical break-glass file, and it refuses the same request without the header with 401. The evidence records the source SHAs, promoted hashes, status codes, and cleanup. behavior: "Break-glass admitted after revert" in `docs/evidence/gdaemon-key-cutover-rollback-rehearsal.md`.

## D1 Live activation of the key cutover (depends: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6)
`kind: deferred`

The PD runs this in an approved window outside quiet hours (Decisions 13 and
14). No leaf runs it.

- D1.1: 1.1 is on `0.5.0` and the daemon has restarted since, so
  `~/.gobby/break_glass` exists with mode 0600. `gobby auth key --show` names a
  live key on the hub and on every enrolled node.
- D1.2: the Merge Manager lands 1.2 to 1.6 on `0.5.0` together.
- D1.3: global BEFORE notice, quiet window, the flag-day gterm activation from
  `admin-operations.md`, promotion of the coherent set with gterm and
  gclient, and a daemon restart.
- D1.4: on the hub, the CLI, gclient, gcode, and ghook authenticate with the
  bootstrap key. Node Rust clients acquire grants only after #23274.
  Loopback break-glass is admitted. A spawned agent's capability stays valid
  across a second restart. The retired `~/.gobby/local_cli_token` is deleted.
  A global DAEMON BACK notice carries the outcome.

```yaml
deferral:
  task_ref: "TBD-at-expansion"
  reason: "Live activation restarts the production daemon and drains the gterm host in an approved window. Expansion manifests reject the manual category, so this expands as a planning task held needs-planning with blocked-by edges to 1.1 through 1.6; the Orchestrator retypes it to manual and the PD runs it."
  owner: "program-director"
  original_acceptance_items:
    - D1.1
    - D1.2
    - D1.3
    - D1.4
```

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15434 under
  Orchestrator rulings (a) and (b), relayed by the Lane Manager gobby#15389.
  Targets and consumers were swept read-only on `0.5.0` at `48071323c9`. The
  draft is narrative only, with no M1.
- 2026-10-05: Consensus between the Plan Writer gobby#15434 and the Adversary
  gobby#15401 at `fc8d56b`, with this entry.
  - The Orchestrator accepted enhancer edits E1 to E4, with E3 limited to
    scope.
  - The Adversary's findings KEY-A1 to KEY-A8 are resolved in `2752d70`,
    `87f3e25`, `b9afc3b`, `ab8e05e`, and `fc8d56b`.
  - Josh approved Decision 15 as written, through the Assistant gobby#15070,
    relayed by the Lane Manager gobby#15389.
  - Found work went to the Orchestrator: #23484 (Reference audit leaves
    gobby auth login and gobby auth key unmapped), which 1.3 handles
    conditionally. #23274 (Node channel, relay backend, and `/api/machines`)
    gained its blocked-by edge to #23273.

## V2: Verification
`kind: verification`

After every leaf has passed:

1. Run the focused suites of 1.1 to 1.4 together against the test hub.
2. Run both corpus suites:
   - `DATABASE_URL=… GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -q`;
   - `cargo test -p gdaemon --test http_contracts` (heavy work).
3. Run the e2e files that 1.2 and 1.3 target against isolated test daemons.
4. Rerun `tests/runtime_grants/test_golden_vectors.py` and
   `cargo nextest run -p gobby-core grant` (heavy work) unchanged (Decision 1).
5. After D1: a CLI call authenticates with the bootstrap key, and a spawned
   agent's capability survives a daemon restart.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Loopback break-glass credential
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `ensure_break_glass_credential` creates a mode-0600
    file when it is absent and leaves an existing file byte-identical, and `break_glass_matches`
    refuses a file with any group or other mode bit, a file owned by another uid,
    and a wrong value. test: `tests/utils/test_break_glass.py::test_credential_is_owner_only_and_never_rewritten`.

    1.1.2: With the database getter raising, a loopback request carrying the break-glass
    header is admitted to a non-grant route, while a non-loopback peer with the header
    and a loopback peer with a wrong or missing header get 401. The database getter
    is never called on the admitted path. test: `tests/servers/test_break_glass.py::test_break_glass_admits_only_loopback_holder_without_database`.

    1.1.3: On a grant route, the break-glass header yields the operator bearer principal
    and the request is still refused without a grant. test: `tests/servers/test_break_glass.py::test_break_glass_still_requires_grant_on_grant_routes`.

    1.1.4: `run_gobby` creates the credential before it starts the front door, in
    the startup bootstrap''s directory even when `--config` lies outside a different
    `GOBBY_HOME`, and an `AuthService` built with defaults in that process admits
    that file''s value. A creation failure does not stop startup. test: `tests/test_runner_front_door.py::test_run_gobby_creates_break_glass_before_front_door`.

    1.1.5: Managed sandboxes may neither read nor write the break-glass file or the
    startup bootstrap, including when the daemon bootstrap is bound to a directory
    outside a different `GOBBY_HOME` and inside an allowed root. test: `tests/agents/test_sandbox_policy.py::test_break_glass_file_is_a_credential_root`.

    1.1.6: The admin guide documents break-glass access. behavior: "Break-glass access"
    in `docs/guides/admin-operations.md`.'
  labels:
  - covers:gdaemon-key-cutover:1.1:1.1.1
  - covers:gdaemon-key-cutover:1.1:1.1.2
  - covers:gdaemon-key-cutover:1.1:1.1.3
  - covers:gdaemon-key-cutover:1.1:1.1.4
  - covers:gdaemon-key-cutover:1.1:1.1.5
  - covers:gdaemon-key-cutover:1.1:1.1.6
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Managed capability tokens sign with a key derived from the bootstrap API
    key
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: A capability issued with `derive_managed_signing_key(api_key)`
    verifies with that key and fails with the raw API key, with a key derived from
    another API key, and when signed with the API key itself; the derived key equals
    `HMAC-SHA256(api_key, b"gobby-managed-token-v1")`. test: `tests/utils/test_local_token.py::test_managed_tokens_sign_with_derived_key`.

    1.2.2: A capability issued before a restart stays valid after it: a new `AuthService`
    over the same bootstrap, bound to a lease with a different `grant_signing_secret`,
    still classifies it as live managed claims. test: `tests/servers/test_auth_service.py::test_managed_token_survives_restart_and_new_lease`.

    1.2.3: After the bootstrap `api_key` is replaced by rename, the next request with
    a capability signed under the old key is rejected and one signed under the new
    key is accepted, with no refresh interval in between. test: `tests/servers/test_auth_service.py::test_rotated_bootstrap_key_invalidates_managed_tokens`.

    1.2.4: The managed challenge proof uses the derived key and the interactive proof
    is unchanged. test: `tests/servers/routes/test_runtime_handshake.py::test_managed_challenge_uses_derived_signing_key`.

    1.2.5: With no bootstrap `api_key`, issuance fails closed with `signing_key_unavailable`
    and every presented capability is rejected. test: `tests/utils/test_local_token.py::test_missing_bootstrap_key_refuses_issuance_and_verification`.

    1.2.6: With the daemon bootstrap bound outside a different `GOBBY_HOME` whose
    own bootstrap carries another key, a capability from `read_managed_signing_key()`
    verifies in an `AuthService` built with defaults, and one signed with the `GOBBY_HOME`
    key is rejected. test: `tests/servers/test_auth_service.py::test_managed_signing_uses_the_daemon_bootstrap`.'
  labels:
  - covers:gdaemon-key-cutover:1.2:1.2.1
  - covers:gdaemon-key-cutover:1.2:1.2.2
  - covers:gdaemon-key-cutover:1.2:1.2.3
  - covers:gdaemon-key-cutover:1.2:1.2.4
  - covers:gdaemon-key-cutover:1.2:1.2.5
  - covers:gdaemon-key-cutover:1.2:1.2.6
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: 'Shared-token cutover: gdaemon validates keys and Python trusts only the
    front door'
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: '1.3.1: gdaemon resolves a valid key to user, machine, and
    key ids, strips client identity headers, forwards the ids with the front-door
    secret, passes `gobby-agent-v1.` bearers through, and rejects revoked keys with
    401 and malformed or unknown-kind bearers with 401 without a database hit, on
    HTTP and on WebSocket upgrades. test: `crates/gdaemon/tests/front_door.rs::valid_key_forwards_identity_headers`.

    1.3.2: Python accepts the operator principal only with the matching front-door
    secret on HTTP and WebSocket, and rejects the retired token, the `X-Gobby-Local-Token`
    alias, and identity headers with a wrong or missing secret. test: `tests/servers/test_auth_service.py::test_front_door_identity_requires_secret`.

    1.3.3: gdaemon answers the interactive challenge locally from a per-request bootstrap
    read, so a rewritten bootstrap key applies to the next challenge, and forwards
    the managed challenge to Python. It keeps the Python contract: `credential_before_proof`
    for a request carrying `Authorization`, padded and unpadded base64url nonces decoded
    before the HMAC, an overlong or undecodable nonce refused, `caller.kind` taking
    precedence over `kind`, and an unknown kind refused. test: `crates/gdaemon/tests/front_door.rs::interactive_challenge_answered_locally`.

    1.3.4: A node user''s interactive grant validates at the hub against the forwarded
    machine. test: `tests/servers/test_grant_auth.py::test_interactive_grant_matches_forwarded_machine`.

    1.3.5: No reference to `local_cli_token`, `LOCAL_CLI_TOKEN`, `X-Gobby-Local-Token`,
    or `auth.api_token_hash` remains under `src/` or `crates/` or in the docs this
    leaf targets, except the `_REMOVED_STORED_KEYS` entry and `crates/CHANGELOG.md`
    history (literal sweep recorded in the leaf). behavior: "API key" in `docs/contracts/secrets.md`.

    1.3.6: The manifest and every case carry `schema_version` 2, the auth cases are
    re-recorded, the forged-identity case forges only `X-Gobby-Project-Id`, the two
    `down` cases keep `backend: down`, and every Python-origin `up` case replays equal
    in Python. test: `tests/contracts/test_http_corpus.py::test_case_replays_equal`.

    1.3.7: The Rust harness loads the version-2 corpus and replays every case, including
    the native `runtime_challenge` case, in its declared backend state. test: `crates/gdaemon/tests/http_contracts.rs::cases_replay_in_declared_backend_state`.

    1.3.8: `src/gobby/hooks/inbox.py` is below 850 lines after the move, and the moved
    loop still drains and prunes on its own cadence. file: `src/gobby/hooks/inbox_maintenance.py`.

    1.3.9: The Rust key-format helper matches 4.2''s shared vectors. test: `crates/gcore/src/api_key_format.rs::matches_shared_vectors`.

    1.3.10: Startup on an install that still stores `auth.api_token_hash` deletes
    that row before fail-closed resolution, and a different unknown stored key still
    refuses. test: `tests/config/test_removed_config_fields.py::test_retired_auth_token_hash_row_is_deleted`.

    1.3.11: After the bootstrap key changes, the next frames hello with the new key
    is accepted and one with the old key is refused with `invalid_token`, an attached
    connection keeps streaming, and an empty or unreadable bootstrap key is refused.
    test: `crates/gterminal/tests/frame_protocol.rs::rotated_key_applies_to_next_hello`.

    1.3.12: With gdaemon''s key resolution failing, a loopback request carrying only
    the break-glass header passes the front door unchanged and Python admits it with
    no front-door identity. test: `crates/gdaemon/tests/front_door.rs::break_glass_header_passes_through_when_resolver_fails`.

    1.3.13: A resolver database error answers 503 `key_resolver_unavailable`, and
    gdaemon refuses to start without `GOBBY_FRONT_DOOR_SECRET`. test: `crates/gdaemon/tests/front_door.rs::resolver_error_is_503_and_missing_secret_refuses_start`.

    1.3.14: The API-key routes take the principal from verified front-door identity,
    ignore unverified identity headers, and list or revoke only the caller''s keys.
    test: `tests/servers/routes/test_api_keys.py::test_key_routes_use_verified_front_door_identity`.

    1.3.15: `src/gobby/terminals/native_runtime.py` and `crates/gcore/src/grant/acquisition.rs`
    are below 850 production lines after their moves. file: `src/gobby/terminals/native_frames.py`.

    1.3.16: `open_storage_and_config` provisions the API key and neither writes `local_cli_token`
    nor stores `auth.api_token_hash`. test: `tests/test_runner_init.py::test_startup_provisions_no_operator_token`.'
  labels:
  - covers:gdaemon-key-cutover:1.3:1.3.1
  - covers:gdaemon-key-cutover:1.3:1.3.2
  - covers:gdaemon-key-cutover:1.3:1.3.3
  - covers:gdaemon-key-cutover:1.3:1.3.4
  - covers:gdaemon-key-cutover:1.3:1.3.5
  - covers:gdaemon-key-cutover:1.3:1.3.6
  - covers:gdaemon-key-cutover:1.3:1.3.7
  - covers:gdaemon-key-cutover:1.3:1.3.8
  - covers:gdaemon-key-cutover:1.3:1.3.9
  - covers:gdaemon-key-cutover:1.3:1.3.10
  - covers:gdaemon-key-cutover:1.3:1.3.11
  - covers:gdaemon-key-cutover:1.3:1.3.12
  - covers:gdaemon-key-cutover:1.3:1.3.13
  - covers:gdaemon-key-cutover:1.3:1.3.14
  - covers:gdaemon-key-cutover:1.3:1.3.15
  - covers:gdaemon-key-cutover:1.3:1.3.16
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: '`gobby auth key` rotates, lists, revokes, and mints keys'
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '1.4.1: `--rotate` mints with the old key, publishes, verifies
    the new key, and only then revokes the old one. When verification fails, only
    the key fields are restored, a field another writer changed meanwhile survives,
    the new key is revoked, and the old key is never revoked. When another writer
    replaced the key during the rotation, `--rotate` leaves that writer''s key and
    the bootstrap untouched. test: `tests/cli/test_auth_login.py::test_rotate_verifies_before_revoke`.

    1.4.2: `list` and `revoke` send the bootstrap key as the bearer, show only the
    caller''s keys, report a foreign id as not found, and refuse to revoke the bootstrap''s
    own key. test: `tests/cli/test_auth_login.py::test_key_list_and_revoke_send_the_bootstrap_key`.

    1.4.3: `--mint LABEL` prints the new key once and leaves the bootstrap byte-identical.
    test: `tests/cli/test_auth_login.py::test_key_mint_prints_once_and_writes_nothing`.

    1.4.4: The rotation and mint options are mutually exclusive with `--show`, and
    `--show` still prints only the hint and id. test: `tests/cli/test_auth_login.py::test_key_show_prints_hint_only`.

    1.4.5: The admin reference audit maps `gobby auth key`, `gobby auth key list`,
    and `gobby auth key revoke` to `authentication.md` with resolvable implementation
    symbols, and maps no `gobby auth token`. test: `tests/skills/test_reference_library.py::test_reference_contract_3_2_1`.'
  labels:
  - covers:gdaemon-key-cutover:1.4:1.4.1
  - covers:gdaemon-key-cutover:1.4:1.4.2
  - covers:gdaemon-key-cutover:1.4:1.4.3
  - covers:gdaemon-key-cutover:1.4:1.4.4
  - covers:gdaemon-key-cutover:1.4:1.4.5
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Guides and examples describe the API key
  category: docs
  task_type: chore
  depends_on:
  - '1.3'
  - '1.4'
  validation_criteria: '1.5.1: `gcode --allow-stale grep ''local_cli_token|LOCAL_CLI_TOKEN|X-Gobby-Local-Token|api_token_hash|gobby
    auth token|local_token_file_present'' docs/ -l -m1000`, run after 1.3 and recorded
    in the leaf, lists only paths under the excluded dated paths. behavior: "gobby
    auth login" in `docs/guides/system-requirements.md`.

    1.5.2: The admin guide carries the seven-step flag-day gterm activation procedure.
    behavior: "Flag-day gterm activation" in `docs/guides/admin-operations.md`.

    1.5.3: The observability example authenticates Prometheus with a dedicated minted
    key in an owner-only file. behavior: "gobby auth key --mint" in `docs/examples/observability/README.md`.

    1.5.4: The admin guide''s rotation procedure uses the key command and states the
    capability-token consequence. behavior: "gobby auth key --rotate" in `docs/guides/admin-operations.md`.'
  labels:
  - covers:gdaemon-key-cutover:1.5:1.5.1
  - covers:gdaemon-key-cutover:1.5:1.5.2
  - covers:gdaemon-key-cutover:1.5:1.5.3
  - covers:gdaemon-key-cutover:1.5:1.5.4
  tdd: false
  source_section: '1.5'
  assigned_agent: tech-writer
- title: Rollback rehearsal keeps the operator a way in
  category: test
  task_type: chore
  depends_on:
  - '1.3'
  - '1.4'
  - '1.5'
  validation_criteria: '1.6.1: After the cutover code is reverted on the same isolated
    home and schema the tip ran on, a daemon whose Python imports resolve under the
    reverted clone''s `src` and whose running gdaemon is the reverted build promoted
    to the isolated bin dir starts at schema 459. It admits a loopback break-glass
    `GET /api/admin/status` with the tip''s byte-identical break-glass file, and it
    refuses the same request without the header with 401. The evidence records the
    source SHAs, promoted hashes, status codes, and cleanup. behavior: "Break-glass
    admitted after revert" in `docs/evidence/gdaemon-key-cutover-rollback-rehearsal.md`.'
  labels:
  - covers:gdaemon-key-cutover:1.6:1.6.1
  tdd: false
  source_section: '1.6'
  assigned_agent: backend-developer
```
