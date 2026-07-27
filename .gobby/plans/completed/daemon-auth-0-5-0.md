# Required-by-default daemon auth (epic #17441)

**Plan ID:** daemon-auth-0-5-0

## Overview
`kind: framing`

Gobby's daemon is effectively unauthenticated today (verified live 2026-07-09): the opt-in web cookie auth guards only part of `/api/*` (`/api/admin/config` and `/api/sessions/<uuid>` serve full data with no credentials), the `/mcp` FastMCP mount and `/memory` router bypass the middleware entirely (it only 401s `/api/` paths), the standalone WebSocket server accepts every connection (its Bearer `auth_callback` is never wired), the stored web password is Fernet-reversible and compared with plain `!=`, and no programmatic client (ghook, DaemonClient, stdio MCP proxy) can authenticate at all. 0.5.0 ships with auth working end to end.

Key discovery: a token convention already half-exists. Rust clients (gcore AI ops in `crates/gcore/src/ai/daemon/transport.rs`, plus dead DB-broker calls in gcode/gwiki) already read `${GOBBY_HOME:-~/.gobby}/local_cli_token` and send `X-Gobby-Local-Token`; the Python daemon ignores the header and nothing provisions the file. This plan enforces and provisions that existing convention.

Architecture: one install-scoped API token, plaintext at `~/.gobby/local_cli_token` (0600), SHA-256 hash stored in the existing `config_store` table as `auth.api_token_hash` (no schema migration; a hash is not a secret, so SecretStore's Fernet/KEK path is deliberately not used; any daemon sharing the hub validates the same token). Canonical header `Authorization: Bearer <token>`; `X-Gobby-Local-Token` accepted as an alias since shipped Rust already emits it. A new `AuthService` owned by `HTTPServer` is the single verifier shared by the HTTP middleware, auth routes, the standalone WS server (via `auth_callback`), and the `/ws` browser proxy. Browsers keep email-as-username + password login (scrypt at rest) → `gobby_session` cookie; the `/ws` proxy authenticates browsers by cookie and injects the bearer token upstream to 60888. Opt-out is `auth_mode: disabled` in bootstrap.yaml; absent means `required`.

## Constraints
`kind: framing`

- 0.5.0 is unshipped: no backward-compatibility shims (guiding principle 16). One-shot data migrations (Fernet password → scrypt hash) are acceptable.
- No user model. Per-user tokens, DB-level roles for standalone binaries, and KEK-as-identity are deferred to open task #17769 (post-0.5.0, blocked by #17439). Standalone `gcode`/`gwiki` direct-Postgres access is unchanged: DSN credentials + KEK-file possession remain their 0.5.0 auth boundary, documented honestly.
- Reuse the existing conventions: `local_cli_token` filename (gcore constant), `_write_private_file` 0600 pattern (`src/gobby/storage/secrets.py:196`), `AuthStore` SHA-256 token hashing, `config_store` table, `update_bootstrap_yaml` atomic writes.
- Landing order must keep Josh's live dev daemon working at every phase boundary: provisioning and client wiring land before server acceptance; acceptance lands before the enforcement flip; installed `~/.gobby/bin/{ghook,gcode,gwiki}` binaries must be rebuilt and reinstalled before the flip restarts the daemon.
- `AuthService` reads `ConfigStore` live with a debounced refresh (5s) rather than the startup config snapshot — `$secret:`/config values resolved at daemon start go stale after rotation (known daemon behavior).
- No `test_mode` auth bypass: unit fixtures opt out explicitly via an `auth_mode` parameter; e2e daemons run `required` for real coverage.
- websockets is 16.0: the upstream-header kwarg for the `/ws` proxy is `additional_headers`.
- Lifecycle endpoints (`/api/health`, `/api/admin/health`, `/api/admin/startup-progress`) stay public: port-based lifecycle probes in `cli/daemon.py` (including `_poll_startup_progress`, which `gobby start` runs while the daemon may still be provisioning the token on a fresh install — requiring auth there is a bootstrap race), `mcp_proxy/daemon_control.py`, and ghook `planned_shutdown.rs` are intentionally credential-free. They expose liveness/startup-phase data only.

## P1: Token provisioning, bootstrap flag, and auth CLI
`kind: framing`

**Goal**: The token exists (file + DB hash) on every install and first daemon run, with `auth_mode` configurable and a `gobby auth` CLI to manage it — no enforcement change yet.

### 1.1 Add local API token storage and provisioning [category: code]
`kind: deliverable`

Target: `src/gobby/storage/auth.py`, `src/gobby/utils/local_token.py`, `src/gobby/runner_init/storage.py`, `src/gobby/cli/install.py`

Create `src/gobby/utils/local_token.py` (zero heavy imports so CLI/mcp_proxy can use it):
- `local_token_path() -> Path` — `default_gobby_home()` (from `gobby.config.bootstrap_io`) joined with `"local_cli_token"` (must match gcore's `LOCAL_CLI_TOKEN_FILENAME = "local_cli_token"`).
- `read_local_api_token() -> str | None` — read + strip; None if missing/empty.
- `daemon_auth_headers() -> dict[str, str]` — `{"Authorization": f"Bearer {token}"}` or `{}`.

In `src/gobby/storage/auth.py`:
- Promote `_hash_token` to public `hash_token(token: str) -> str` (SHA-256 hexdigest).
- `LOCAL_API_TOKEN_HASH_KEY = "auth.api_token_hash"`.
- `ensure_local_api_token(config_store: ConfigStore) -> str | None` with reconciliation: file+matching hash → no-op; file+no hash → adopt (store hash, `source="system"`); neither → generate `secrets.token_urlsafe(32)`, write file, store hash; hash only (no file) → WARN "copy ~/.gobby/local_cli_token from the hub machine or run 'gobby auth token --rotate'", do NOT regenerate; file+hash mismatch → WARN same remediation, DB wins for validation.
- `rotate_local_api_token(config_store: ConfigStore) -> str` — new token, overwrite file + hash.
- File writes via `write_private_file` — promote `_write_private_file` (`src/gobby/storage/secrets.py:196-202`, O_WRONLY|O_CREAT|O_TRUNC 0o600) to public and import it.

Wire provisioning:
- `src/gobby/runner_init/storage.py::init_storage_and_config` — call `ensure_local_api_token(runner.config_store)` immediately after `secret_store.ensure_ready(...)` (line 97). This also provisions e2e daemons in their temp `GOBBY_HOME`.
- `src/gobby/cli/install.py` — call provisioning after `_configure_secret_kek_posture` (~line 595) when the hub DB is reachable; if not, write the file only and let the daemon adopt it on first run.

**Acceptance:**

- 1.1.1 - Token helper module exists with path/read/headers functions. file: `src/gobby/utils/local_token.py`.
- 1.1.2 - `ensure_local_api_token` implements the five-case reconciliation matrix. symbol: `gobby.storage.auth.ensure_local_api_token`.
- 1.1.3 - Token file is written 0600 and hash lands in config_store key `auth.api_token_hash`. test: `tests/storage/test_auth.py::test_ensure_local_api_token_generates`.
- 1.1.4 - Hash-only case warns and does not regenerate; mismatch case warns and DB wins. test: `tests/storage/test_auth.py::test_ensure_local_api_token_hash_only_warns`.
- 1.1.5 - Daemon first-run provisions the token after secret envelope setup. symbol: `gobby.runner_init.storage.init_storage_and_config`.
- 1.1.6 - `gobby install` provisions token file + hash. test: `tests/cli/test_cli_install.py::test_install_provisions_api_token`.
- 1.1.7 - DB-unreachable install writes the 0600 token file without a hash, and first daemon storage init adopts that file into `auth.api_token_hash` without rotating it. test: `tests/cli/test_cli_install.py::test_install_db_unreachable_writes_file_only`.

### 1.2 Add auth_mode to bootstrap and daemon config [category: config]
`kind: deliverable`

Target: `src/gobby/config/bootstrap.py`, `src/gobby/config/app.py`, `src/gobby/install/shared/config/bootstrap.yaml`, `crates/gcore/src/bootstrap.rs`

- `BootstrapConfig` (`src/gobby/config/bootstrap.py:53-65`): add `auth_mode: Literal["required", "disabled"] = "required"`. Parse in `load_bootstrap`: absent → `required`; unknown value → hard error (fail loud, not fail open). Emit in `to_config_dict()` so the runtime `DaemonConfig` sees it.
- `DaemonConfig` (`src/gobby/config/app.py`): add top-level `auth_mode: str = "required"` field.
- Installed template `src/gobby/install/shared/config/bootstrap.yaml`: document the key with its default.
- `crates/gcore/src/bootstrap.rs`: confirm unknown-key tolerance (serde ignores unknown fields unless `deny_unknown_fields`); add the field only if a Rust consumer needs it (none does in 0.5.0).

**Acceptance:**

- 1.2.1 - `auth_mode` parses with required default and errors on unknown values. test: `tests/config/test_bootstrap.py::test_auth_mode_parsing`.
- 1.2.2 - `auth_mode` flows bootstrap → DaemonConfig. symbol: `gobby.config.bootstrap.BootstrapConfig`.
- 1.2.3 - Rust bootstrap reader tolerates the new key. test: `crates/gcore/src/bootstrap.rs` unit test `reads_bootstrap_with_auth_mode`.

### 1.3 Restructure gobby auth CLI into a group with token management [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/cli/auth.py`

Convert the single `auth` command into `@click.group("auth")`:
- `gobby auth credentials [--remove]` — the existing web-credential flow moved verbatim (hardening happens in 3.2).
- `gobby auth token` — status: prints token file path, exists/missing, and `sha256:<first8>…` of the stored hash, whether file and DB agree.
- `gobby auth token --show` — prints the plaintext token.
- `gobby auth token --rotate` — calls `rotate_local_api_token`; echoes that running clients pick up the new token within ~5 seconds (AuthService debounce) and the file must be re-copied to any other machines.
- `gobby install --auth-mode [required|disabled]` option on `src/gobby/cli/install.py` writing via `update_bootstrap_yaml` (`src/gobby/config/bootstrap_io.py`).

`src/gobby/cli/__init__.py` keeps registering the `auth` name; subcommand help text carries the remediation strings used in server 401 messages.

**Acceptance:**

- 1.3.1 - `gobby auth` is a group with `credentials` and `token` subcommands. symbol: `gobby.cli.auth.auth`.
- 1.3.2 - Rotate writes a new file + hash and old token no longer matches. test: `tests/cli/test_auth.py::test_auth_token_rotate`.
- 1.3.3 - `gobby install --auth-mode disabled` persists to bootstrap.yaml. test: `tests/cli/test_cli_install.py::test_install_auth_mode_flag`.

## P2: Clients send the token
`kind: framing`

**Goal**: Every daemon client presents `Authorization: Bearer` before the server ever enforces it. Python and Rust tracks are independent.

### 2.1 Thread token through Python daemon clients [category: code] (depends: P1)
`kind: deliverable`

Target: `src/gobby/utils/daemon_client.py`, `src/gobby/utils/status.py`, `src/gobby/mcp_proxy/stdio_proxy.py`, `src/gobby/mcp_proxy/session_bootstrap.py`, `src/gobby/hooks/inbox.py`, `src/gobby/cli/clones.py`, `src/gobby/cli/agents.py`, `src/gobby/cli/worktrees.py`, `src/gobby/cli/workflows/manage.py`

- `DaemonClient.__init__` (`src/gobby/utils/daemon_client.py`): `self._auth_headers = daemon_auth_headers()`; apply in `check_health`, `check_status`, `call_http_api` (lines 196-234), `call_mcp_tool`. On 401, raise/return the remediation message: token missing or stale → "run 'gobby install' or 'gobby auth token --rotate' on the hub machine and copy ~/.gobby/local_cli_token here".
- `DaemonProxy` (`src/gobby/mcp_proxy/stdio_proxy.py`): cache token in `__init__`; add `Authorization` to the headers block in `_request` (lines 144-155); on 401 re-read the file and retry once (handles rotation mid-session). Also fixes the live bug where `/api/workflows/variables/{set,get}` 401s under cookie auth.
- `src/gobby/mcp_proxy/session_bootstrap.py:61-96` — add headers to the `find_by_terminal_context` POST.
- Raw-httpx stragglers get `headers=daemon_auth_headers()`: `cli/clones.py:106,194,250,301,384`, `cli/agents.py:275`, `cli/worktrees.py:58`, `cli/workflows/manage.py:119,242`. Port-based health checks (`cli/daemon.py:283,817`, `mcp_proxy/daemon_control.py:26`) stay bare — health is public.
- `src/gobby/utils/status.py::fetch_rich_status` (line 28, backs `gobby status` via `cli/daemon.py:739`) calls `/api/admin/status`, which P4 protects: attach `daemon_auth_headers()` to its httpx call so rich status keeps working after enforcement while plain health probes remain public and bare.
- `src/gobby/hooks/inbox.py::_post_envelope` (134-160): the ASGI replay traverses AuthMiddleware, so add the daemon's own token via `read_local_api_token()`; strip any persisted `Authorization` key from stored envelope headers before adding the fresh one. Log one actionable warning per drain cycle if required-mode and the file is missing.
- Spawned-agent stdio proxies inherit `GOBBY_HOME`/`HOME` → same token file. Verify code-index isolation agents (`src/gobby/agents/code_index.py:165` exports GOBBY_HOME): if the isolated home differs from the daemon's, copy the token file alongside the bootstrap file it already writes.

**Acceptance:**

- 2.1.1 - DaemonClient sends bearer on all HTTP methods. test: `tests/utils/test_daemon_client.py::test_auth_headers_attached`.
- 2.1.2 - stdio proxy attaches bearer and retries once on 401 after re-reading the file. test: `tests/mcp_proxy/test_stdio_proxy.py::test_request_auth_retry`.
- 2.1.3 - Inbox replay strips stale Authorization and attaches the daemon token. test: `tests/hooks/test_inbox.py::test_replay_attaches_token`.
- 2.1.4 - All raw-httpx straggler call sites pass auth headers. file: `src/gobby/cli/clones.py`.
- 2.1.5 - Isolated code-index agent homes receive the token file. behavior: "token file copied when GOBBY_HOME isolation differs" in `src/gobby/agents/code_index.py`.
- 2.1.6 - `fetch_rich_status` sends the bearer header while health probes stay bare. test: `tests/utils/test_status.py::test_fetch_rich_status_sends_bearer`.

### 2.2 Add token to installed git-hook curl template [category: code] (depends: P1)
`kind: deliverable`

Target: `src/gobby/cli/installers/git_hooks.py`

In `_CODE_INDEX_REINDEX_BODY` (lines 29-66): read `${GOBBY_HOME:-$HOME/.gobby}/local_cli_token` in shell; when present add `-H "Authorization: Bearer $TOKEN"` to both curl branches of the codewiki-refresh call; omit the header when the file is missing (daemon may run `auth_mode: disabled`). Existing repos pick the new body up on next `gobby install` via the START/END marker replacement — CHANGELOG note in 5.2.

**Acceptance:**

- 2.2.1 - Generated hook body reads the token file and sends the bearer header. test: `tests/cli/installers/test_git_hooks.py::test_hook_body_includes_token`.
- 2.2.2 - Hook body omits the header cleanly when no token file exists. behavior: "curl runs without Authorization when token file missing" in `src/gobby/cli/installers/git_hooks.py`.

### 2.3 Rust clients send token; delete dead DB-broker paths [category: code] (depends: P1)
`kind: deliverable`

Target: `crates/gcore/src/local_token.rs`, `crates/gcore/src/ai/daemon/transport.rs`, `crates/ghook/src/transport.rs`, `crates/ghook/src/diagnose.rs`, `crates/gcode/src/graph/code_graph/lifecycle.rs`, `crates/gcode/src/db/resolution.rs`, `crates/gwiki/src/support/env.rs`

- New `crates/gcore/src/local_token.rs` (pub module): move `read_local_cli_token()`, `LOCAL_CLI_TOKEN_FILENAME`, and the header constant out of `crates/gcore/src/ai/daemon/transport.rs`; add `pub fn authorization_bearer(token: &str) -> String`. The AI daemon transport delegates to it (local error wrapping stays).
- `crates/ghook/src/transport.rs::post_and_cleanup` (137-203): best-effort token read; when present set `Authorization: Bearer` on the `/api/hooks/execute` POST; missing file → send bare (daemon may be `disabled`). Extend `crates/ghook/src/diagnose.rs` to report token-file presence and a 401-remediation hint.
- `crates/gcode/src/graph/code_graph/lifecycle.rs:250-279`: add the bearer header to the graph clear/rebuild POSTs.
- Delete dead code (route no longer exists server-side): `crates/gcode/src/db/resolution.rs:208-251` broker call + `validate_loopback_daemon_url` guard + fixtures at :616; `crates/gwiki/src/support/env.rs:77-128` broker call + guards + tests. Bootstrap-file/env DSN remains the only standalone DB path. After this deletion no loopback-only token guard exists anywhere; gcore sends the token to whatever URL the resolver produced (bootstrap/env are trusted local config; Tailscale is the transport story).
- Rebuild + reinstall `~/.gobby/bin/{ghook,gcode,gwiki}` (release build) — precondition for P4 on the live daemon.

**Acceptance:**

- 2.3.1 - Shared token helper crate module exists and gcore AI transport delegates to it. file: `crates/gcore/src/local_token.rs`.
- 2.3.2 - ghook attaches bearer to hook POSTs when the token file exists. test: `crates/ghook/src/transport.rs` unit test `post_includes_bearer_when_token_present`.
- 2.3.3 - gcode graph lifecycle POSTs carry the bearer header. symbol: `gcode::graph::code_graph::lifecycle::run_lifecycle_action`.
- 2.3.4 - Dead `/api/local/runtime/database-url` broker clients and loopback guards are removed from gcode and gwiki. file: `crates/gcode/src/db/resolution.rs`.

## P3: Server acceptance and login hardening
`kind: framing`

**Goal**: The daemon verifies bearer/alias/cookie through one AuthService, the web login is hardened, and WS auth is wired — with the effective default still `disabled` so this lands green against unmodified fixtures. The flip is P4.

### 3.1 Implement AuthService and construct it in HTTPServer [category: code] (depends: P2)
`kind: deliverable`

Target: `src/gobby/servers/auth_service.py`, `src/gobby/servers/http.py`

New `src/gobby/servers/auth_service.py`:

```python
class AuthService:
    def __init__(self, database_getter, mode: Literal["required", "disabled"],
                 token_file: Path | None = None): ...
    # Cached, threading.Lock-guarded, MIN_REFRESH_INTERVAL = 5.0s debounce:
    #   _token_hash (ConfigStore "auth.api_token_hash"), _web_username,
    #   _web_password_hash, _local_token_plaintext (daemon's own file copy)
    @property
    def enabled(self) -> bool: ...           # mode == "required"
    def verify_bearer(self, token: str) -> bool: ...
        # hash_token(token) vs cached hash via hmac.compare_digest;
        # on mismatch: debounced refresh() from ConfigStore, re-verify once (rotation without restart)
    async def verify_ws_token(self, token: str) -> str | None: ...  # auth_callback shape
    def is_request_authenticated(self, request) -> bool: ...
        # order: Authorization: Bearer -> X-Gobby-Local-Token -> gobby_session cookie
    def validate_session(self, token: str) -> bool: ...  # delegates AuthStore
    def verify_password(self, username: str, password: str) -> bool: ...
        # scrypt(n=2**14, r=8, p=1, dklen=32); hmac.compare_digest on both fields
    def local_token(self) -> str | None: ...  # cached file read for /ws upstream + inbox
    def refresh(self) -> None: ...
```

Reads ConfigStore directly (never the pydantic snapshot — startup-resolved values go stale after rotation). `local_token()` and `_token_hash` refresh on the SAME debounced `refresh()` path, so after rotation the `/ws` upstream bearer injection picks up the new plaintext token in the same ≤5s window as bearer verification — a stale `local_token()` cache would break browser WS proxying while HTTP tests still pass.

`HTTPServer.__init__` (`src/gobby/servers/http.py:59-132`): new kwarg `auth_mode: str | None = None`. **Phase boundary is mechanical**: in this phase, effective mode = kwarg if given, else module-level `_PHASE_DEFAULT_AUTH_MODE = "disabled"` — `services.config.auth_mode` is deliberately NOT consulted yet, so this commit cannot enforce auth no matter what bootstrap says. 4.1 is the only deliverable that deletes `_PHASE_DEFAULT_AUTH_MODE` and switches resolution to kwarg → `services.config.auth_mode` → `"required"`. No `test_mode` bypass. Fold in the dead-code deletion: remove uncalled `run_server` (`http.py:643`, zero callers, hardcodes host="0.0.0.0").

**Acceptance:**

- 3.1.1 - AuthService verifies bearer via timing-safe compare with debounced rotation refresh. test: `tests/servers/test_auth_service.py::test_verify_bearer_rotation_refresh`.
- 3.1.2 - Header precedence Bearer → X-Gobby-Local-Token → cookie. test: `tests/servers/test_auth_service.py::test_is_request_authenticated_precedence`.
- 3.1.3 - HTTPServer owns an AuthService with kwarg/config mode resolution. symbol: `gobby.servers.http.HTTPServer`.
- 3.1.4 - Dead `run_server` is removed. file: `src/gobby/servers/http.py`.
- 3.1.5 - Effective mode in this phase resolves kwarg-else-`_PHASE_DEFAULT_AUTH_MODE`("disabled") and never consults config. test: `tests/servers/test_auth_service.py::test_phase_default_ignores_config`.
- 3.1.6 - `local_token()` refreshes on the same debounced path as bearer verification. test: `tests/servers/test_auth_service.py::test_local_token_refreshes_after_rotation`.

### 3.2 Rewrite middleware + auth routes on AuthService; scrypt password storage [category: code] (depends: 3.1)
`kind: deliverable`

Target: `src/gobby/servers/middleware/auth.py`, `src/gobby/servers/routes/auth.py`, `src/gobby/config/ui.py`, `src/gobby/cli/auth.py`, `src/gobby/runner_init/storage.py`

- `src/gobby/servers/middleware/auth.py`: rewrite `dispatch` to consult `server.auth_service` (`is_request_authenticated`); keep the OLD `_PUBLIC_PREFIXES` in this phase (shrink is 4.1). Delete `is_auth_enabled`/`validate_session_cookie`/`_get_auth_credentials` from the auth routes module — killing the per-request SecretStore rebuild + Fernet decrypt and the plain `!=` compare at `src/gobby/servers/routes/auth.py:101`.
- `src/gobby/servers/routes/auth.py`: `login` uses `auth_service.verify_password`; `auth_status` returns `{auth_required, authenticated, credentials_configured}`.
- Password at rest → scrypt: `gobby auth credentials` stores `auth.password_hash` as `scrypt$16384$8$1$<salt_b64>$<hash_b64>` via plain `ConfigStore.set` (`hashlib.scrypt`, no new deps). One-shot migration in `runner_init/storage.py` next to token provisioning: legacy Fernet secret `password` present and `auth.password_hash` absent → decrypt, hash, store, delete legacy secret + `auth.password` reference; on failure warn "run 'gobby auth credentials'" (CLI path always recoverable).
- `src/gobby/config/ui.py:39-59`: delete dead `AuthConfig.password` and `session_secret`; keep `username`; `extra: "ignore"` so the `auth.api_token_hash`/`auth.password_hash` config keys don't break model hydration.
- 401 body for unauthenticated API requests: `{"error": "Authentication required. CLI clients need ~/.gobby/local_cli_token (run 'gobby install' or 'gobby auth token --rotate'). Browsers: log in."}`.

**Acceptance:**

- 3.2.1 - Login verifies via scrypt + compare_digest; per-request Fernet decrypt is gone. symbol: `gobby.servers.auth_service.AuthService.verify_password`.
- 3.2.2 - Legacy Fernet password migrates to scrypt hash on daemon start. test: `tests/servers/test_auth_service.py::test_legacy_password_migration`.
- 3.2.3 - Middleware accepts bearer, alias header, and cookie. test: `tests/servers/test_http_middleware.py::test_bearer_and_alias_accepted`.
- 3.2.4 - `auth_status` reports credentials_configured. test: `tests/servers/routes/test_auth_routes.py::test_status_credentials_configured`.
- 3.2.5 - Dead AuthConfig fields removed. file: `src/gobby/config/ui.py`.

### 3.3 Wire WebSocket auth: 60888 callback + /ws proxy cookie→bearer bridge [category: code] (depends: 3.1)
`kind: deliverable`

Target: `src/gobby/runner_init/servers.py`, `src/gobby/servers/_app_ui.py`, `src/gobby/servers/websocket/server.py`

- `runner_init/servers.py:129-136`: pass `auth_callback=http_server.auth_service.verify_ws_token` when `auth_service.enabled`, else `None`. The kwarg already exists (`websocket/server.py:75`); `AuthMixin._authenticate` (`websocket/auth.py:29-80`) needs no changes.
- `/ws` proxy (`_app_ui.py:47-72`): `BaseHTTPMiddleware` never sees websocket scopes, so enforce in the endpoint handlers — if `auth_service.enabled` and neither a valid `gobby_session` cookie (`websocket.cookies`, browsers send cookies on same-origin WS upgrade) nor a valid bearer header → `await websocket.close(code=4401)` before proxying. On success, connect upstream with `additional_headers=[("Authorization", f"Bearer {auth_service.local_token()}")]` (websockets 16.0 kwarg).
- Posture fix: `WebSocketConfig` fallback bind default `0.0.0.0` → `localhost` (`websocket/server.py:64`).

**Acceptance:**

- 3.3.1 - Standalone WS server rejects bare connections and accepts bearer when enabled. test: `tests/servers/websocket/test_auth.py::test_wired_callback_rejects_and_accepts`.
- 3.3.2 - /ws proxy closes 4401 without cookie/bearer and injects upstream bearer with them. test: `tests/servers/test_ws_proxy_auth.py::test_proxy_cookie_bridge`.
- 3.3.3 - WS fallback bind is localhost. file: `src/gobby/servers/websocket/server.py`.

### 3.4 Web UI: credentials_configured UX and fail-closed status default [category: code] (depends: 3.2)
`kind: deliverable`

Target: `web/src/hooks/useAuth.ts`, `web/src/components/auth/LoginPage.tsx`

- `useAuth.ts`: consume `credentials_configured` from `/api/auth/status`; flip the fail-open default (today status-fetch errors assume no auth required, lines 21-34) to fail-closed so the login page renders instead of a broken shell.
- `LoginPage.tsx`: when auth is required but `credentials_configured` is false, show setup instructions ("run `gobby auth credentials` on the daemon host") instead of a futile login form.

**Acceptance:**

- 3.4.1 - Status hook surfaces credentials_configured and fails closed. file: `web/src/hooks/useAuth.ts`.
- 3.4.2 - Login page renders setup guidance when no credentials are configured. behavior: "setup instructions shown when credentials_configured=false" in `web/src/components/auth/LoginPage.tsx`.

## P4: Enforcement flip
`kind: framing`

**Goal**: One small, revertable commit: required-by-default, shrunken public surface, fixture choke points updated in the same commit.

### 4.1 Flip default to required, shrink public prefixes, update fixture choke points [category: code] (depends: P3)
`kind: deliverable`

Target: `src/gobby/servers/http.py`, `src/gobby/servers/middleware/auth.py`, `tests/servers/conftest.py`, `tests/e2e/conftest.py`, `tests/servers/routes/test_communications.py`, `tests/e2e/test_crash_recovery.py`, `tests/e2e/test_daemon_lifecycle.py`, `tests/e2e/test_e2e_smoke.py`, `tests/e2e/test_full_workflow.py`, `tests/e2e/test_session_tracking.py`, `tests/e2e/test_worktrees_e2e.py`, `tests/e2e/test_mcp_proxy_e2e.py`

- `HTTPServer`: delete `_PHASE_DEFAULT_AUTH_MODE` (the temporary P3 constant) and switch effective-mode resolution to kwarg → `services.config.auth_mode` → `"required"` — this deliverable is the only place that flip happens.
- `src/gobby/servers/middleware/auth.py` `_PUBLIC_PREFIXES` shrinks to: `/` exact, `/api/auth/`, `/api/health`, `/api/admin/health`, `/api/admin/startup-progress` (exact — `gobby start` readiness polling runs while token provisioning may still be in flight on a fresh install; liveness data only, per the Constraints lifecycle-endpoint posture), `/api/comms/webhooks/` (per-channel signature validation — add a test asserting an unverified channel rejects), `/api/github/webhooks/` (HMAC-verified in the github triage route, verified during exploration), `/assets/`, `/favicon.ico`, `/logo.png`. Dropped: `/api/hooks/`, `/api/sessions/`, `/api/local/` (phantom), `/api/mcp*`, `/api/admin/status|metrics|config`, `/ws*` (ws scopes never reach this middleware anyway).
- Unauthed requests to paths starting `/api/`, `/mcp`, `/memory` → 401 JSON with the remediation body; other paths (SPA shell, Vite assets) fall through so React shows the login page. This closes the `/mcp` mount and `/memory` router bypasses.
- Same commit, fixture choke points: `tests/servers/conftest.py::create_http_server` + `http_server` fixture default `auth_mode="disabled"` with a passthrough param (keeps the 73 TestClient files green untouched); replace the `is_auth_enabled` monkeypatch in `test_communications.py:57` with AuthService-based setup; `tests/e2e/conftest.py` — add a `daemon_token()` helper reading `<gobby_home>/local_cli_token` from the temp home and attach headers in `daemon_client`, `async_daemon_client`, `CLIEventSimulator`, and both MCP test clients so e2e daemons run **required**.
- Grep tests for direct `HTTPServer(` constructions outside the conftest helpers and fix stragglers.
- Sweep `tests/e2e/**` for raw daemon HTTP calls that bypass the fixture choke points. The sweep is defined by pattern, not by list: every ad-hoc `httpx.Client`/`httpx.AsyncClient` construction and bare `httpx.get`/`httpx.post` against a daemon port must route through the `daemon_token()` helper (or an auth-aware probe wrapper in the e2e conftest) so it attaches the temp-home bearer; no per-test auth bypass. Inventory as of adversary round 2: `tests/e2e/conftest.py` (status probes at :197/:208 plus the CLIEventSimulator and both MCP test clients), `tests/e2e/test_crash_recovery.py` (7 sites), `tests/e2e/test_daemon_lifecycle.py` (4), `tests/e2e/test_full_workflow.py` (2), `tests/e2e/test_session_tracking.py` (:217/:249 → `/api/sessions`), `tests/e2e/test_worktrees_e2e.py` (:67, :225 → `/api/mcp/tools/call`), `tests/e2e/test_mcp_proxy_e2e.py` (:192/:224 → `/api/mcp/tools/call`); `tests/e2e/test_e2e_smoke.py` uses only the `daemon_client` fixture and is covered by the fixture change. Completeness is re-verified at implementation time by grepping `tests/e2e/` for direct httpx construction, not by trusting this inventory.

**Acceptance:**

- 4.1.1 - Default mode is required; disabled needs explicit bootstrap opt-out. test: `tests/servers/test_http_middleware.py::test_required_by_default`.
- 4.1.2 - Public surface is exactly the shrunken list; /mcp and /memory 401 unauthenticated. test: `tests/servers/test_http_middleware.py::test_public_prefix_matrix`.
- 4.1.3 - Unit fixtures opt out via auth_mode param; suite passes with flip in place. file: `tests/servers/conftest.py`.
- 4.1.4 - E2E fixtures attach the temp-home token and run required. file: `tests/e2e/conftest.py`.
- 4.1.5 - `_PHASE_DEFAULT_AUTH_MODE` is removed and resolution consults `services.config.auth_mode` before the required fallback. symbol: `gobby.servers.http.HTTPServer`.
- 4.1.6 - Public webhook prefixes stay signature-gated: missing/bad signature rejected and signed requests accepted for both comms and github webhooks, tested beside the public-surface matrix. test: `tests/servers/test_http_middleware.py::test_public_webhooks_signature_gated`.
- 4.1.7 - `/api/admin/startup-progress` is public in the prefix matrix so `gobby start` readiness polling works against a required-mode daemon without credentials. test: `tests/servers/test_http_middleware.py::test_public_prefix_matrix`.
- 4.1.8 - Raw e2e daemon probes attach the bearer: crash-recovery, lifecycle, smoke, full-workflow, session-tracking, worktrees, and mcp-proxy suites pass in required mode. behavior: "grep of `tests/e2e/` finds no ad-hoc httpx client or bare httpx call hitting a protected daemon endpoint without the auth-aware helper" in `tests/e2e/conftest.py`.
- 4.1.9 - Session-tracking and MCP tool-call raw clients route through the auth-aware helper. file: `tests/e2e/test_session_tracking.py`. file: `tests/e2e/test_mcp_proxy_e2e.py`. file: `tests/e2e/test_worktrees_e2e.py`.

## P5: End-to-end verification and docs
`kind: framing`

**Goal**: Prove the whole matrix against a real daemon and document the new posture.

### 5.1 E2E auth test suite [category: test] (depends: P4)
`kind: deliverable`

Target: `tests/e2e/test_daemon_auth.py`

New suite against a spawned required-mode daemon (mirrors the 2026-07-09 manual curl session):
- Unauthed: `/api/admin/health` 200; `/api/admin/config`, `/api/sessions/<uuid>`, `/api/hooks/execute`, `/mcp`, `/memory/...` → 401.
- With bearer from the temp-home token file: all → 2xx. With `X-Gobby-Local-Token`: 200. Garbage token: 401.
- WS: direct 60888 without header → handshake rejected; with bearer → accepted. `/ws` proxy without cookie → close 4401; after `/api/auth/login` (scrypt credentials seeded via ConfigStore) → proxied frames flow.
- Rotation: `gobby auth token --rotate` → old token 401 within the 5s refresh window, new token 200. WS rotation: direct 60888 rejects the old bearer and accepts the new one, and the `/ws` cookie bridge injects the refreshed upstream bearer (chat frames still flow) — all without daemon restart.
- `auth_mode: disabled` bootstrap → everything open.

**Acceptance:**

- 5.1.1 - Unauthenticated/authenticated HTTP matrix passes against a live daemon. test: `tests/e2e/test_daemon_auth.py::test_http_auth_matrix`.
- 5.1.2 - Both WS surfaces enforce and accept correctly. test: `tests/e2e/test_daemon_auth.py::test_ws_auth`.
- 5.1.3 - Rotation invalidates the old token and admits the new one without restart. test: `tests/e2e/test_daemon_auth.py::test_token_rotation`.
- 5.1.5 - After rotation both WS surfaces work with the new token and the /ws bridge injects the refreshed upstream bearer. test: `tests/e2e/test_daemon_auth.py::test_ws_rotation`.
- 5.1.4 - Disabled mode opens all surfaces. test: `tests/e2e/test_daemon_auth.py::test_auth_mode_disabled`.

### 5.2 Documentation and changelog [category: docs] (depends: P4)
`kind: deliverable`

Target: `docs/guides/shared-stack.md`, `docs/contracts/secrets.md`, `docs/contracts/identity-model.md`, `docs/guides/web-ui.md`, `docs/guides/http-endpoints.md`, `docs/guides/admin-operations.md`, `docs/guides/configuration.md`, `docs/guides/hub-install-contract.md`, `docs/guides/cli-commands.md`, `CHANGELOG.md`

- `shared-stack.md`: remove "the HTTP API is unauthenticated" claim (§129-151); add the token-copy step for additional machines; keep the honest boundary statement for standalone gcode/gwiki (DSN + KEK possession; multi-user story → #17769).
- `secrets.md` + `identity-model.md`: document the API-token contract (file, hash key, header forms, rotation semantics) alongside the KEK contract.
- `http-endpoints.md`: public-endpoint table (the shrunken list, including the lifecycle trio `/api/health`, `/api/admin/health`, `/api/admin/startup-progress`) + Authorization header requirement; external streamable-HTTP `/mcp` clients need the bearer header.
- `admin-operations.md`: rotation runbook + the manual verification script (curl matrix, `gobby task list`, hooked-repo commit, stdio-proxy tool call, web login + chat WS).
- `configuration.md` (`auth_mode`), `hub-install-contract.md` (token file is part of hub-client install), `cli-commands.md` (`gobby auth` group).
- `CHANGELOG.md` breaking notes: re-run `gobby install` (git-hook curl + token provisioning), rebuild/reinstall Rust binaries before restarting onto the flip, `gobby auth` → group, password storage now scrypt (auto-migrates).

**Acceptance:**

- 5.2.1 - Shared-stack guide reflects required-by-default auth and the token-copy step. file: `docs/guides/shared-stack.md`.
- 5.2.2 - Endpoint guide lists the exact public surface. file: `docs/guides/http-endpoints.md`.
- 5.2.3 - Changelog carries the breaking-change upgrade notes. file: `CHANGELOG.md`.

## V1 Plan Changelog
`kind: verification`

Initial draft 2026-07-09 from epic #17441 exploration (three codebase surveys + live curl verification of the current auth surface) and design synthesis.

**Round 1** `kind: enhancement`

- enhancer_run: f227e9c4-c23d-4afd-8270-e3b89dd5c78e
- enhancer_session: c7f42770-6a4c-4167-a380-15d3101d5bf8
- converged: false
- suggestions_presented: 5
- accepted:
  - E1 / better / mechanical P3→P4 default boundary via temporary `_PHASE_DEFAULT_AUTH_MODE` constant, removed only in 4.1
  - E2 / better / add missed client `utils/status.py::fetch_rich_status` (backs `gobby status`, hits protected `/api/admin/status`) to the 2.1 sweep
  - E3 / better / acceptance + test for the DB-unreachable install path (file-only write, first-run adoption without rotation)
  - E4 / better / `local_token()` refreshes on the shared debounced path; WS rotation coverage in 3.1 and 5.1
  - E5 / better / negative signature tests proving public webhook prefixes stay self-gated
- declined: none
- resolution_notes: All five folded into sections 1.1, 2.1, 3.1, 4.1, 5.1; user accepted all (2026-07-09).

**Round 2** `kind: adversary`

- adversary_run: 5581fafb-8a7d-4e35-a21c-73e85713baf4
- reviewer_session: "#8104" (9ab857c7-8f00-45d7-8dc3-0da22d519b55)
- verdict: needs_review (adversary round 1 of 3, rejected)
- blocking_findings:
  - F1-startup-progress-auth-gap / `gobby start` polls `/api/admin/startup-progress` bare while P4 protects all `/api/admin/*` except health. Resolution: route stays public — it is a lifecycle probe that runs while token provisioning may still be in flight on fresh installs (bootstrap race if protected). Added to the Constraints lifecycle-endpoint posture, the 4.1 public-prefix list, acceptance 4.1.7, and the 5.2 endpoint table.
  - F2-e2e-raw-httpx-status-gap / raw `httpx.get(.../api/admin/status)` probes in e2e suites bypass the fixture choke points and would 401 in required mode. Resolution: added an explicit `tests/e2e/**` raw-call sweep to 4.1 (route probes through the `daemon_token()` helper / auth-aware wrapper), the four affected suites to 4.1 targets, and acceptance 4.1.8.
- resolution_notes: Both findings folded into Constraints, 4.1, and 5.2 (2026-07-09). No manifest written (rejection round).

**Round 3** `kind: adversary`

- adversary_run: 481dda14-5e08-44dd-a852-d2c5c2496217
- reviewer_session: "#8106" (8356a7b5-3a27-4863-b6fe-c3c9ce6b244b)
- verdict: needs_review (adversary round 2 of 3, rejected)
- blocking_findings:
  - F3-e2e-raw-httpx-sweep-incomplete / the Round 2 sweep inventory missed raw clients in `tests/e2e/test_session_tracking.py` (:217/:249 → `/api/sessions`), `tests/e2e/test_worktrees_e2e.py` (:225 → `/api/mcp/tools/call`), and `tests/e2e/test_mcp_proxy_e2e.py` (:192 → `/api/mcp/tools/call`). Resolution: re-swept `tests/e2e/**` exhaustively; 4.1 now defines the sweep by pattern (any ad-hoc httpx client / bare httpx call to a daemon port) with the full round-2 inventory recorded, adds the three suites to targets, expands acceptance 4.1.8, and adds 4.1.9 for the newly found suites. Startup-progress resolution (F1) verified unchanged.
- resolution_notes: Folded into 4.1 (2026-07-09). No manifest written (rejection round).

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add local API token storage and provisioning
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: Token helper, reconciliation matrix, install, and daemon first-run provisioning tests pass.
  labels:
    - covers:daemon-auth-0-5-0:1.1:1.1.1
    - covers:daemon-auth-0-5-0:1.1:1.1.2
    - covers:daemon-auth-0-5-0:1.1:1.1.3
    - covers:daemon-auth-0-5-0:1.1:1.1.4
    - covers:daemon-auth-0-5-0:1.1:1.1.5
    - covers:daemon-auth-0-5-0:1.1:1.1.6
    - covers:daemon-auth-0-5-0:1.1:1.1.7
  assigned_agent: null
  tdd: true
  source_section: "1.1"
  implementation_domain: backend
- title: Add auth_mode to bootstrap and daemon config
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: Bootstrap, daemon config, template, and Rust bootstrap tolerance tests pass.
  labels:
    - covers:daemon-auth-0-5-0:1.2:1.2.1
    - covers:daemon-auth-0-5-0:1.2:1.2.2
    - covers:daemon-auth-0-5-0:1.2:1.2.3
  assigned_agent: null
  tdd: true
  source_section: "1.2"
- title: Restructure gobby auth CLI into a token-aware group
  category: code
  task_type: feature
  depends_on:
    - "1.1"
  validation_criteria: Auth CLI grouping, token rotation, and install auth-mode flag tests pass.
  labels:
    - covers:daemon-auth-0-5-0:1.3:1.3.1
    - covers:daemon-auth-0-5-0:1.3:1.3.2
    - covers:daemon-auth-0-5-0:1.3:1.3.3
  assigned_agent: null
  tdd: true
  source_section: "1.3"
  implementation_domain: backend
- title: Thread token through Python daemon clients
  category: code
  task_type: feature
  depends_on:
    - "1.1"
    - "1.2"
    - "1.3"
  validation_criteria: Python daemon clients, stdio proxy, inbox replay, rich status, and isolated-agent token tests pass.
  labels:
    - covers:daemon-auth-0-5-0:2.1:2.1.1
    - covers:daemon-auth-0-5-0:2.1:2.1.2
    - covers:daemon-auth-0-5-0:2.1:2.1.3
    - covers:daemon-auth-0-5-0:2.1:2.1.4
    - covers:daemon-auth-0-5-0:2.1:2.1.5
    - covers:daemon-auth-0-5-0:2.1:2.1.6
  assigned_agent: null
  tdd: true
  source_section: "2.1"
  implementation_domain: backend
- title: Add token to installed git-hook curl template
  category: code
  task_type: feature
  depends_on:
    - "1.1"
  validation_criteria: Generated git hook sends bearer when the token file exists and omits it cleanly when absent.
  labels:
    - covers:daemon-auth-0-5-0:2.2:2.2.1
    - covers:daemon-auth-0-5-0:2.2:2.2.2
  assigned_agent: null
  tdd: true
  source_section: "2.2"
  implementation_domain: backend
- title: Send token from Rust clients and remove dead broker paths
  category: code
  task_type: feature
  depends_on:
    - "1.1"
  validation_criteria: Shared Rust token helper, ghook, gcode graph lifecycle, and broker-path deletion checks pass.
  labels:
    - covers:daemon-auth-0-5-0:2.3:2.3.1
    - covers:daemon-auth-0-5-0:2.3:2.3.2
    - covers:daemon-auth-0-5-0:2.3:2.3.3
    - covers:daemon-auth-0-5-0:2.3:2.3.4
  assigned_agent: null
  tdd: true
  source_section: "2.3"
  implementation_domain: backend
- title: Implement AuthService and construct it in HTTPServer
  category: code
  task_type: feature
  depends_on:
    - "2.1"
    - "2.2"
    - "2.3"
  validation_criteria: AuthService rotation, precedence, local-token refresh, HTTPServer mode, and run_server removal tests pass.
  labels:
    - covers:daemon-auth-0-5-0:3.1:3.1.1
    - covers:daemon-auth-0-5-0:3.1:3.1.2
    - covers:daemon-auth-0-5-0:3.1:3.1.3
    - covers:daemon-auth-0-5-0:3.1:3.1.4
    - covers:daemon-auth-0-5-0:3.1:3.1.5
    - covers:daemon-auth-0-5-0:3.1:3.1.6
  assigned_agent: null
  tdd: true
  source_section: "3.1"
  implementation_domain: backend
- title: Rewrite middleware and auth routes on AuthService
  category: code
  task_type: feature
  depends_on:
    - "3.1"
  validation_criteria: Scrypt login, legacy password migration, middleware, auth status, and config cleanup tests pass.
  labels:
    - covers:daemon-auth-0-5-0:3.2:3.2.1
    - covers:daemon-auth-0-5-0:3.2:3.2.2
    - covers:daemon-auth-0-5-0:3.2:3.2.3
    - covers:daemon-auth-0-5-0:3.2:3.2.4
    - covers:daemon-auth-0-5-0:3.2:3.2.5
  assigned_agent: null
  tdd: true
  source_section: "3.2"
  implementation_domain: backend
- title: Wire WebSocket auth and browser proxy bearer bridge
  category: code
  task_type: feature
  depends_on:
    - "3.1"
  validation_criteria: Standalone WebSocket auth, /ws proxy cookie bridge, and localhost bind posture tests pass.
  labels:
    - covers:daemon-auth-0-5-0:3.3:3.3.1
    - covers:daemon-auth-0-5-0:3.3:3.3.2
    - covers:daemon-auth-0-5-0:3.3:3.3.3
  assigned_agent: null
  tdd: true
  source_section: "3.3"
  implementation_domain: backend
- title: Update web UI auth status and credential setup UX
  category: code
  task_type: feature
  depends_on:
    - "3.2"
  validation_criteria: Auth hook fails closed and login page shows setup guidance when credentials are absent.
  labels:
    - covers:daemon-auth-0-5-0:3.4:3.4.1
    - covers:daemon-auth-0-5-0:3.4:3.4.2
  assigned_agent: null
  tdd: true
  source_section: "3.4"
  implementation_domain: frontend
- title: Flip auth default to required and update fixture choke points
  category: code
  task_type: feature
  depends_on:
    - "3.1"
    - "3.2"
    - "3.3"
    - "3.4"
  validation_criteria: Required-by-default middleware, exact public surface, webhook gating, and e2e auth-aware raw-call sweep checks pass.
  labels:
    - covers:daemon-auth-0-5-0:4.1:4.1.1
    - covers:daemon-auth-0-5-0:4.1:4.1.2
    - covers:daemon-auth-0-5-0:4.1:4.1.3
    - covers:daemon-auth-0-5-0:4.1:4.1.4
    - covers:daemon-auth-0-5-0:4.1:4.1.5
    - covers:daemon-auth-0-5-0:4.1:4.1.6
    - covers:daemon-auth-0-5-0:4.1:4.1.7
    - covers:daemon-auth-0-5-0:4.1:4.1.8
    - covers:daemon-auth-0-5-0:4.1:4.1.9
  assigned_agent: null
  tdd: true
  source_section: "4.1"
  implementation_domain: backend
- title: Add live daemon e2e auth suite
  category: test
  task_type: task
  depends_on:
    - "4.1"
  validation_criteria: Live required-mode and disabled-mode daemon auth, WebSocket, and rotation e2e tests pass.
  labels:
    - covers:daemon-auth-0-5-0:5.1:5.1.1
    - covers:daemon-auth-0-5-0:5.1:5.1.2
    - covers:daemon-auth-0-5-0:5.1:5.1.3
    - covers:daemon-auth-0-5-0:5.1:5.1.5
    - covers:daemon-auth-0-5-0:5.1:5.1.4
  assigned_agent: null
  tdd: false
  source_section: "5.1"
- title: Document required-by-default daemon auth
  category: docs
  task_type: chore
  depends_on:
    - "4.1"
  validation_criteria: Shared-stack, endpoint guide, and changelog document the required-by-default auth posture and upgrade notes.
  labels:
    - covers:daemon-auth-0-5-0:5.2:5.2.1
    - covers:daemon-auth-0-5-0:5.2:5.2.2
    - covers:daemon-auth-0-5-0:5.2:5.2.3
  assigned_agent: null
  tdd: false
  source_section: "5.2"
```

