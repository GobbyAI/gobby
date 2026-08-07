# Canonical User Identity and Machine Ownership Foundation

> **Plan ID:** account-identity-machine-ownership

## Summary
`kind: framing`

Create one durable user during first interactive `gobby install`, reuse that record for web login, and assign every machine exactly one owner. Authentication becomes mandatory across HTTP and WebSocket transports.

```text
user ──< machines ──< coding sessions
  └──< browser auth sessions
```

Unknown machine IDs received through hooks or session registration are never auto-claimed. Installation and later authenticated enrollment are the only ownership-establishing paths.

## Public contracts
`kind: framing`

- `users`: UUID `id`, required case-insensitive unique `email`, required `name`, Argon2id `password_hash`, `created_at`, and `updated_at`.
- `machines.owner_user_id`: `UUID NOT NULL REFERENCES users(id)` with restricted deletion.
- `auth_sessions.user_id`: `UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`.
- `POST /api/auth/login`: `{email, password, remember_me}`.
- `GET /api/auth/status`: `{authenticated}`; always-required and installation-state fields are removed as redundant.
- `LocalMachineManager.upsert_seen(machine_id, owner_user_id, ...)`: ownership-establishing operation.
- `LocalMachineManager.refresh_seen(machine_id, ...)`: existing-machine metadata refresh that never inserts or changes ownership.
- `LocalMachineManager.list_for_user(user_id)`.
- `LocalUserManager.resolve_for_session(session_id)`.
- Remove bootstrap/application `auth_mode`, `gobby install --auth-mode`, and `gobby auth credentials --remove`.

## Constraints
`kind: framing`

- Every runnable datastore has exactly one initial user. Schema permits later multiuser work.
- First unattended install fails with instructions to run interactive installation.
- Existing local bearer-token and managed-agent authentication remain.
- Roles, user-management HTTP APIs, deletion/disable lifecycle, enrollment, API-key redesign, and user-scoped secrets remain in #17769 and #18902.
- Keep baseline version 375; regenerate its catalog checksum and expected identity without a numbered migration.
- Follow pg_dump layout: add `CREATE TABLE users` in the alphabetical table section and add PK/FK/index constraints in their existing later sections.
- Keep installer bootstrap logic outside the current 955-line `install.py`.

## P1: Durable identity and ownership
`kind: framing`

**Goal:** Establish database invariants and storage APIs before changing ingress or authentication.

### 1.1 Add canonical user persistence [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the full baseline catalog manifest
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerate the full expected schema identity
- `src/gobby/storage/users.py`
- `src/gobby/storage/auth.py::AuthStore`
- `src/gobby/storage/auth.py::hash_password`
- `src/gobby/storage/auth.py::verify_password_hash`
- `tests/storage/test_users.py`
- `tests/storage/test_auth.py::*` — scope-reason: replace anonymous auth-session coverage with user-owned sessions

Add `users` with application-generated UUIDs. Trim email input, preserve its submitted casing, and enforce uniqueness using `UNIQUE (lower(email))`. Reject blank email/name values. Profile and password changes update `updated_at`.

Retain the existing encoded Argon2id format:

```text
$argon2id$v=19$m=65536,t=3,p=4$<128-bit salt>$<256-bit tag>
```

This matches RFC 9106's second recommended Argon2id profile. Argon2id combines data-independent and data-dependent memory access for side-channel and tradeoff-attack resistance. See [RFC 9106 §4](https://www.rfc-editor.org/rfc/rfc9106.html#section-4). OWASP recommends Argon2id and specifies a lower minimum baseline than Gobby's current parameters. See the [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#argon2id).

Add `auth_sessions.user_id` and require `AuthStore.create_session(user_id, remember_me=...)`. Preserve hashed tokens, expiry, remember-me duration, logout, and cleanup.

**Acceptance:**

- 1.1.1 - Baseline 375 creates users and user-owned auth sessions with enforced PK/FK/index contracts. file: `crates/gcore/assets/schema/baseline.sql`.
- 1.1.2 - User storage supports create, lookup, list, profile update, and password update with typed duplicate-email conflicts. symbol: `gobby.storage.users.LocalUserManager`.
- 1.1.3 - Password hashing retains random salts and the canonical RFC-profile encoding. test: `tests/storage/test_users.py`.
- 1.1.4 - Auth sessions require an existing user while retaining expiry and logout behavior. test: `tests/storage/test_auth.py`.

### 1.2 Enforce ownership and separate registration from refresh [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/machines.py::Machine`
- `src/gobby/storage/machines.py::LocalMachineManager.upsert_seen`
- `src/gobby/storage/machines.py::LocalMachineManager.get`
- `src/gobby/runner_init/helpers.py::ensure_machine_identity`
- `src/gobby/hooks/hook_manager.py::HookManager._record_machine_ingress`
- `src/gobby/storage/sessions/_crud.py::_SessionCRUDMixin.register`
- `src/gobby/storage/users.py`
- `tests/storage/test_machines.py::*` — scope-reason: replace nullable and overwrite semantics with complete ownership coverage
- `tests/hooks/test_hook_manager.py::*` — scope-reason: verify existing-only machine refresh at untrusted hook ingress
- `tests/storage/sessions/test_usage_and_bootstrap.py::*` — scope-reason: verify session rejection for unknown machines

Change `owner_user_id` to required UUID. Implement:

- `upsert_seen(machine_id, owner_user_id, ...)` inserts a machine or refreshes it only when the existing owner matches.
- First insert establishes ownership.
- Same-owner retries are idempotent.
- Different-owner retries raise `MachineOwnershipConflictError` without changing the row.
- `refresh_seen(machine_id, ...)` updates an existing row and returns `None` for an unknown machine.
- `list_for_user` enumerates all machines owned by one user.
- `resolve_for_session` joins session → machine → user.

`HookManager._record_machine_ingress` uses `refresh_seen`. Unknown UUIDs are logged and ignored without aborting unrelated hook processing.

Session registration also uses `refresh_seen`. An unknown machine raises `MachineNotRegisteredError` before any session insert. It never claims the machine for the initial user.

Daemon startup loads the sole installed user and idempotently registers the canonical local machine under that user. Missing or multiple users fail with instructions to run or repair installation.

**Acceptance:**

- 1.2.1 - Every machine has one existing owner and one user can enumerate multiple machines. test: `tests/storage/test_machines.py`.
- 1.2.2 - Same-owner retries succeed and cross-owner claims return a typed conflict without mutation. test: `tests/storage/test_machines.py`.
- 1.2.3 - Hook ingress refreshes known machines and never creates an unknown machine. test: `tests/hooks/test_hook_manager.py`.
- 1.2.4 - Session registration rejects unknown machines before writing a session. test: `tests/storage/sessions/test_usage_and_bootstrap.py`.
- 1.2.5 - Coding-session user identity resolves only through the machine relationship. test: `tests/storage/test_users.py`.
- 1.2.6 - Daemon startup fails closed when no sole installed user can own the local machine. test: `tests/test_runner_init.py`.

### 1.3 Update shared PostgreSQL test identity fixtures [category: test] (depends: 1.2)
`kind: deliverable`

Targets:
- `tests/fixtures/postgres.py::postgres_canonical_seed`
- `tests/storage/sessions/conftest.py::session_identity`
- `tests/install/test_bin_freshness.py::*` — scope-reason: supply the canonical test owner to machine registration
- `tests/mcp_proxy/tools/sessions/test_registration.py::*` — scope-reason: register owned machines before sessions
- `tests/servers/routes/test_sessions_routes.py::*` — scope-reason: register owned machines before route tests
- `tests/code_index/test_storage.py::*` — scope-reason: add required owners to direct machine SQL fixtures
- `tests/communications/test_attachments.py::*` — scope-reason: add required owners to direct machine SQL fixtures
- `tests/runner_maintenance/test_isolation_machine_scope.py::*` — scope-reason: add required owners to direct machine SQL fixtures
- `tests/scheduler/test_cron_machine_scope.py::*` — scope-reason: add required owners to direct machine SQL fixtures
- `tests/storage/agents/test_active_run_scope.py::*` — scope-reason: add required owners to direct machine SQL fixtures
- `tests/storage/test_chat_attachments.py::*` — scope-reason: add required owners to direct machine SQL fixtures
- `tests/storage/test_workspace_machine_scope.py::*` — scope-reason: add required owners to direct machine SQL fixtures
- `tests/storage/test_worktrees.py::*` — scope-reason: add required owners to direct machine SQL fixtures

Seed a canonical test user before canonical machine rows and assign every fixture machine to it. Export stable test user/machine constants or helpers so focused tests do not duplicate raw identity setup.

Update every direct machine insert and `upsert_seen` call to provide an owner. Tests that require an empty users table explicitly clear the canonical seed in their isolated schema.

**Acceptance:**

- 1.3.1 - Shared PostgreSQL fixtures always produce valid user-owned machine rows. symbol: `tests.fixtures.postgres.postgres_canonical_seed`.
- 1.3.2 - Session fixtures reference pre-registered owned machines. file: `tests/storage/sessions/conftest.py`.
- 1.3.3 - A repository-wide search finds no machine insert or ownership-establishing upsert lacking `owner_user_id`. behavior: "owned machine test fixtures" in `tests/fixtures/postgres.py`.

## P2: Installation and mandatory authentication
`kind: framing`

**Goal:** Make installation the sole user bootstrap and remove every unauthenticated server path.

### 2.1 Bootstrap the initial user and machine [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/cli/install_identity.py`
- `src/gobby/cli/install.py::install`
- `src/gobby/utils/machine_id.py::require_machine_id`
- `src/gobby/cli/auth.py::credentials`
- `tests/cli/test_install_identity.py`
- `tests/cli/test_auth.py::*` — scope-reason: replace ConfigStore credential setup/removal with installed-user password reset

After schema setup and before `--config-only` return or daemon startup:

1. Inspect users.
2. If empty, require interactive name, email, hidden password, and confirmation.
3. Normalize input and compute Argon2id outside the transaction.
4. Resolve the canonical machine UUID with `require_machine_id()`.
5. Acquire a transaction-scoped bootstrap lock and recheck user count.
6. Insert the user, then register the machine under that user in one database transaction.
7. On rerun with one user, skip prompts and idempotently ensure local machine ownership.
8. Fail on multiple users because v0.5 has no account selector.
9. Prevent daemon startup after any failure.

A machine-ID file may survive database rollback; reruns reuse it safely.

Change `gobby auth credentials` to reset the sole user's password. Remove credential creation and `--remove`.

**Acceptance:**

- 2.1.1 - First install creates exactly one user and its owned machine before daemon startup. test: `tests/cli/test_install_identity.py`.
- 2.1.2 - Database failure rolls back user and machine rows and blocks daemon startup. test: `tests/cli/test_install_identity.py`.
- 2.1.3 - Reruns are idempotent and fresh unattended installs fail with actionable guidance. test: `tests/cli/test_install_identity.py`.
- 2.1.4 - Credential CLI resets the sole user's password without moving or removing identity. test: `tests/cli/test_auth.py`.
- 2.1.5 - `install.py` remains below 1,000 lines. file: `src/gobby/cli/install.py`.

### 2.2 Remove the authentication mode switch [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/config/bootstrap.py::*` — scope-reason: remove auth-mode type, field, parsing, and serialization
- `src/gobby/config/app.py::DaemonConfig`
- `src/gobby/config/app.py::DaemonConfig.validate_remote_ui_auth`
- `src/gobby/cli/install.py::_set_bootstrap_auth_mode`
- `src/gobby/servers/http.py::HTTPServer.__init__`
- `src/gobby/servers/auth_service.py::AuthService`
- `src/gobby/servers/middleware/auth.py::AuthMiddleware.dispatch`
- `src/gobby/servers/_app_ui.py::_mount_ws_endpoint`
- `src/gobby/hooks/inbox.py::*` — scope-reason: make local-token prerequisites unconditional
- `src/gobby/install/shared/config/bootstrap.yaml::*` — scope-reason: remove the daemon auth-mode field from the bundled bootstrap document
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: remove the obsolete bootstrap auth-mode contract test

Remove `AuthMode`, bootstrap/application `auth_mode`, installer option and writer, HTTP server override, and `AuthService.enabled`.

HTTP middleware, UI WebSockets, direct WebSockets, hook ingress, and daemon API routes always enforce their existing browser-session, local-token, or managed-capability authentication. Login, status, health, and other explicitly public bootstrap routes retain their existing allowlist.

**Acceptance:**

- 2.2.1 - No production config, CLI option, or server constructor can disable authentication. symbol: `gobby.servers.http.HTTPServer.__init__`.
- 2.2.2 - Protected HTTP and WebSocket traffic always fails closed without accepted credentials. symbol: `gobby.servers.middleware.auth.AuthMiddleware.dispatch`.
- 2.2.3 - Hook ingress always requires the local token prerequisite. file: `src/gobby/hooks/inbox.py`.
- 2.2.4 - Bundled bootstrap output contains no `auth_mode`. file: `src/gobby/install/shared/config/bootstrap.yaml`.

### 2.3 Convert server and E2E tests to authenticated fixtures [category: test] (depends: 2.2)
`kind: deliverable`

Targets:
- `tests/config/test_bootstrap.py::*` — scope-reason: remove auth-mode parsing and propagation cases
- `tests/config/test_remote_ui_auth.py::*` — scope-reason: remove validation for a configuration switch that no longer exists
- `tests/cli/test_cli_install.py::*` — scope-reason: remove install auth-mode option coverage
- `tests/cli/test_install_prompts.py::*` — scope-reason: remove auth-mode command arguments
- `tests/servers/conftest.py::*` — scope-reason: make shared clients authenticated by default
- `tests/servers/test_auth_service.py::*` — scope-reason: remove mode construction and assert unconditional authentication
- `tests/servers/test_auth_middleware.py::*` — scope-reason: replace disabled-mode cases with credential success/failure cases
- `tests/servers/test_http_middleware.py::*` — scope-reason: replace disabled-mode HTTP coverage
- `tests/servers/test_http_endpoints.py::*` — scope-reason: authenticate protected endpoint clients
- `tests/servers/test_http_server.py::*` — scope-reason: remove HTTPServer auth-mode overrides
- `tests/servers/test_ws_asgi_endpoint.py::*` — scope-reason: remove disabled WebSocket handshake coverage
- `tests/e2e/conftest.py::*` — scope-reason: remove e2e_auth_mode and seed/use canonical authenticated identity
- `tests/e2e/test_daemon_auth.py::*` — scope-reason: remove disabled-mode E2E and retain required-auth coverage

Seed the canonical user centrally through the PostgreSQL fixture. E2E daemon startup registers its isolated machine under that user. Remove `e2e_auth_mode`, omit `auth_mode` from generated bootstrap YAML, and route test clients through existing authenticated client/header fixtures.

Perform a complete code-index sweep for `auth_mode=` and `auth_mode:` in server/config/E2E tests. Convert remaining protected-route tests to authenticated fixtures and delete cases whose only purpose was disabled-auth behavior.

**Acceptance:**

- 2.3.1 - Shared server clients authenticate without a production-shaped bypass. file: `tests/servers/conftest.py`.
- 2.3.2 - Every E2E daemon boots with a canonical user and owned local machine. symbol: `tests.e2e.conftest.e2e_config`.
- 2.3.3 - No test constructs `DaemonConfig`, `HTTPServer`, or bootstrap YAML with daemon `auth_mode`. behavior: "mandatory authentication test contract" in `tests/config/test_bootstrap.py`.
- 2.3.4 - E2E coverage proves unauthenticated rejection and authenticated HTTP/WebSocket success. test: `tests/e2e/test_daemon_auth.py`.

## P3: Unified browser authentication
`kind: framing`

**Goal:** Authenticate web users from the canonical table and remove the obsolete credential editor.

### 3.1 Switch login to user email/password [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/servers/auth_service.py::AuthService.verify_password`
- `src/gobby/servers/routes/auth.py::_LoginRateLimiter`
- `src/gobby/servers/routes/auth.py::LoginRequest`
- `src/gobby/servers/routes/auth.py::login`
- `src/gobby/servers/routes/auth.py::auth_status`
- `src/gobby/config/ui.py::AuthConfig`
- `web/src/hooks/useAuth.ts::useAuth`
- `web/src/components/auth/LoginPage.tsx::LoginPage`
- `web/src/App.tsx::App`
- `web/src/components/settings/sections/SecretsAuthSection.tsx::AuthGroup`
- `web/src/components/settings/sections/SecretsAuthSection.tsx::SecretsAuthSection`
- `tests/servers/routes/test_auth_routes.py::*` — scope-reason: replace config username cases with user-backed email authentication
- `tests/servers/routes/test_configuration_routes.py::*` — scope-reason: remove ConfigStore web-credential expectations
- `web/src/hooks/useAuth.test.tsx::*` — scope-reason: update status and login wire contracts
- `web/src/components/auth/LoginPage.test.tsx::*` — scope-reason: update the complete login form contract
- `web/src/components/settings/sections/__tests__/SecretsAuthSection.test.tsx::*` — scope-reason: remove AuthGroup assertions while preserving other secret groups
- `web/src/components/settings/sections/__tests__/sections.coverage.test.ts::*` — scope-reason: preserve section registration after removing only its auth group
- `web/src/__tests__/App.test.tsx::*` — scope-reason: simplify the always-required application auth guard

Replace `verify_password(username, password) -> bool` with email authentication returning the user or `None`. Lookup is case-insensitive. Unknown email uses a valid constant dummy Argon2id hash so missing-user and wrong-password attempts both perform the same password derivation and return `Invalid email or password`.

Preserve `_LoginRateLimiter` behavior: client failure counting, lockout, `Retry-After`, reset on success, and generic error responses.

Successful login passes `user.id` to `AuthStore.create_session`.

Since a daemon cannot start without an installed user:

- `/api/auth/status` returns only `authenticated`.
- Remove `AuthService.credentials_configured`.
- `useAuth` tracks `authenticated` and `loading`.
- `App` always presents login when unauthenticated, always gates chat transport on authentication, and shows logout after authentication.
- `LoginPage` drops its unconfigured-credentials state.

Change login form/request from username to email and use `autocomplete="email"`.

Remove `AuthConfig`, ConfigStore `auth.username`, `auth.password_hash`, and legacy `auth.password`. Remove only `AuthGroup` from `SecretsAuthSection`; retain service credentials, SecretStore management, section registration, and existing section ID.

**Acceptance:**

- 3.1.1 - Browser login authenticates only against canonical user email/password and creates a user-owned auth session. test: `tests/servers/routes/test_auth_routes.py`.
- 3.1.2 - Unknown email and wrong password have the same external response, Argon2 work class, and rate-limit handling. test: `tests/servers/routes/test_auth_routes.py`.
- 3.1.3 - Login failure counting, lockout, `Retry-After`, and success reset remain intact. symbol: `gobby.servers.routes.auth._LoginRateLimiter`.
- 3.1.4 - Browser status and application state contain no runtime authentication-mode or credential-toggle semantics. test: `web/src/__tests__/App.test.tsx`.
- 3.1.5 - Secrets settings retain non-auth credential management while removing the web-auth editor. test: `web/src/components/settings/sections/__tests__/SecretsAuthSection.test.tsx`.

### 3.2 Update operator-facing contracts [category: docs] (depends: 3.1)
`kind: deliverable`

Targets:
- `docs/guides/cli-commands.md`
- `docs/guides/configuration.md`
- `docs/guides/web-ui.md`
- `docs/guides/http-endpoints.md`
- `docs/contracts/secrets.md`

Document first-install account creation, required authentication, email login, local password reset, user-owned browser sessions, and removal of `auth_mode`. Document unknown-machine rejection and the distinction between ownership-establishing install/enrollment and metadata-only ingress refresh.

Keep the local daemon bearer token documented as a separate machine-local credential.

**Acceptance:**

- 3.2.1 - Installation and CLI docs describe the sole initial-user and password-reset flows. behavior: "first-install account bootstrap" in `docs/guides/cli-commands.md`.
- 3.2.2 - Configuration docs contain no supported authentication-disable or config-backed password path. behavior: "authentication configuration" in `docs/guides/configuration.md`.
- 3.2.3 - Web/API docs specify email login and user-owned browser sessions. behavior: "web login contract" in `docs/guides/web-ui.md`.
- 3.2.4 - Ownership docs state that hook/session ingress cannot claim unknown machines. behavior: "machine ownership ingress" in `docs/guides/configuration.md`.

## V1 Plan Changelog
`kind: verification`

- Initial draft unified install-time user creation, web credentials, and machine ownership.
- Fable review repair added existing-only hook/session machine refresh, shared PostgreSQL and E2E identity fixtures, complete auth-mode test migration, rate-limit preservation, precise SecretsAuthSection scope, pg_dump baseline ordering, and fail-closed machine-ID resolution.

## V2: Verification
`kind: verification`

Run focused validation with daemon isolation:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/storage/test_users.py \
  tests/storage/test_machines.py \
  tests/storage/test_auth.py \
  tests/storage/sessions/test_usage_and_bootstrap.py \
  tests/hooks/test_hook_manager.py \
  tests/cli/test_install_identity.py \
  tests/cli/test_auth.py -v
```

```bash
GOBBY_TEST_PROTECT=1 uv run pytest \
  tests/config/test_bootstrap.py \
  tests/servers/test_auth_service.py \
  tests/servers/routes/test_auth_routes.py \
  tests/servers/test_auth_middleware.py \
  tests/servers/test_http_middleware.py \
  tests/servers/test_ws_asgi_endpoint.py \
  tests/e2e/test_daemon_auth.py -v
```

```bash
cd web
npm test -- \
  src/hooks/useAuth.test.tsx \
  src/components/auth/LoginPage.test.tsx \
  src/components/settings/sections/__tests__/SecretsAuthSection.test.tsx \
  src/components/settings/sections/__tests__/sections.coverage.test.ts \
  src/__tests__/App.test.tsx
```

Also run scoped Ruff and mypy, `cargo test -p gcore`, schema catalog/identity freshness checks, and code-index sweeps for ownerless machine inserts/upserts plus daemon `auth_mode` references. Do not run the full pytest suite.

Before rebuilding local development state:

1. Stop the daemon.
2. Run `uv run gobby hub-backup` and require verified scratch restore success.
3. Recreate managed PostgreSQL from modified baseline 375.
4. Run interactive `gobby install`.
5. Start the daemon and smoke-test email login, browser WebSocket access, local-token CLI access, unknown-machine rejection, and unauthenticated rejection.

When this plan is persisted, generate its companion `.coverage-ledger.yaml`, validate both artifacts, and obtain adversarial approval before expansion.
