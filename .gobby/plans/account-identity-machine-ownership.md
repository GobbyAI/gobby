# Canonical User Identity and Machine Ownership Foundation

> **Plan ID:** account-identity-machine-ownership

## Summary
`kind: framing`

Create one durable user during first interactive `gobby install`, reuse that record for web login, and assign every machine exactly one owner. Authentication becomes mandatory across HTTP and WebSocket transports.

```text
user ──< machines ──< coding sessions
  └──< browser auth sessions
```

Unknown machine IDs received through hooks or session registration are never auto-claimed. Installation, later authenticated enrollment, and trusted startup under an unambiguous canonical user are the ownership-establishing paths.

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
- Existing baseline-375 datastores transition only through the fenced
  `account-identity-cutover` campaign. Automatic predecessor-receipt refresh is
  prohibited.
- Identity cutover completes and soaks locally before the independent Hub-PC
  datastore move. The Hub-PC restore, nonce-volume, repoint, lease-transfer,
  smoke, and rollback design remains unchanged.

## P1: Durable identity and ownership
`kind: framing`

**Goal:** Establish database invariants and storage APIs before changing ingress or authentication.

### 1.1 Add canonical user persistence [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the full baseline catalog manifest
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerate the full expected schema identity
- `src/gobby/storage/users.py::*` — scope-reason: add the complete canonical-user persistence capability
- `src/gobby/storage/auth.py::AuthStore`
- `src/gobby/identity.py::hash_password`
- `src/gobby/identity.py::verify_password_hash`
- `tests/storage/test_users.py::*` — scope-reason: cover the complete canonical-user and password contract
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
- `src/gobby/runner_lifecycle.py::run_daemon`
- `src/gobby/hooks/hook_manager.py::HookManager._record_machine_ingress`
- `src/gobby/storage/sessions/_crud.py::_SessionCRUDMixin.register`
- `src/gobby/storage/users.py::*` — scope-reason: machine ownership resolution joins through the canonical user capability
- `tests/storage/test_machines.py::*` — scope-reason: replace nullable and overwrite semantics with complete ownership coverage
- `tests/hooks/test_hook_manager.py::*` — scope-reason: verify existing-only machine refresh at untrusted hook ingress
- `tests/storage/sessions/test_usage_and_bootstrap.py::*` — scope-reason: verify session rejection for unknown machines
- `tests/runner_helpers.py::create_base_patches`
- `tests/test_runner_lifecycle.py::TestShutdownLoop.test_readiness_failure_rolls_back_runner_resources`
- `tests/e2e/test_single_active_daemon.py::_write_daemon_home`
- `tests/e2e/test_single_active_daemon.py::test_single_active_daemon_and_explicit_handoff`

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
- `src/gobby/cli/install_identity.py::ensure_install_identity`
- `src/gobby/cli/install.py::install`
- `src/gobby/utils/machine_id.py::require_machine_id`
- `src/gobby/cli/auth.py::credentials`
- `tests/cli/test_install_identity.py::*` — scope-reason: cover fresh install, rollback, rerun, and unattended identity behavior
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
- `src/gobby/cli/install.py::install`
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
- `src/gobby/config/ui.py::*` — scope-reason: remove the obsolete AuthConfig while preserving UI settings
- `src/gobby/config/registry.py::_supplemental_key_specs`
- `web/src/hooks/useAuth.ts::useAuth`
- `web/src/components/auth/LoginPage.tsx::LoginPage`
- `web/src/App.tsx::App`
- `web/src/components/settings/sections/SecretsAuthSection.tsx::SecretsAuthSection`
- `tests/servers/routes/test_auth_routes.py::*` — scope-reason: replace config username cases with user-backed email authentication
- `tests/servers/routes/test_configuration_routes.py::*` — scope-reason: remove ConfigStore web-credential expectations
- `tests/servers/routes/test_configuration_effective_routes.py::*` — scope-reason: update effective-config authentication fixtures to mandatory user sessions
- `tests/servers/routes/test_config_values_api.py::*` — scope-reason: keep restricted-config coverage on the machine token hash after removing password keys
- `tests/servers/test_mcp_routes.py::*` — scope-reason: remove inbox replay dependence on the retired authentication mode
- `tests/e2e/test_daemon_auth.py::*` — scope-reason: seed and authenticate the canonical user in live-daemon coverage
- `web/src/hooks/useAuth.test.tsx::*` — scope-reason: update status and login wire contracts
- `web/src/components/auth/LoginPage.test.tsx::*` — scope-reason: update the complete login form contract
- `web/src/components/settings/sections/__tests__/SecretsAuthSection.test.tsx::*` — scope-reason: remove AuthGroup assertions while preserving other secret groups
- `web/src/components/settings/sections/__tests__/sections.coverage.test.ts::*` — scope-reason: preserve section registration after removing only its auth group
- `web/src/__tests__/App.test.tsx::*` — scope-reason: simplify the always-required application auth guard
- `web/tests/style-surfaces.spec.ts::baseApi`
- `web/tests/style-surfaces.spec.ts::buildImplementations`
- `web/tests/activity-panel-changes-session-scope.spec.ts::*` — scope-reason: update captured auth-status payload to the mandatory-auth contract
- `web/tests/activity-panel-web-chat-sessions.spec.ts::*` — scope-reason: update captured auth-status payloads to the mandatory-auth contract
- `web/tests/approval-modes-live.spec.ts::*` — scope-reason: require an authenticated live daemon without an optional-auth flag
- `web/tests/codex-model-switch-live.spec.ts::*` — scope-reason: require an authenticated live daemon without an optional-auth flag
- `web/tests/provider-picker-live.spec.ts::*` — scope-reason: require an authenticated live daemon without an optional-auth flag
- `web/tests/provider-picker.spec.ts::*` — scope-reason: update captured auth-status payload to the mandatory-auth contract
- `web/tests/terminal-colors.spec.ts::*` — scope-reason: update captured auth-status payload to the mandatory-auth contract
- `web/tests/web-chat-restore-plan.spec.ts::*` — scope-reason: update captured auth-status payload to the mandatory-auth contract
- `web/tests/web-chat-swap-send-respond.spec.ts::*` — scope-reason: update captured auth-status payload to the mandatory-auth contract

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
- `docs/contracts/identity-model.md`
- `docs/guides/account-identity-cutover.md`
- `docs/guides/admin-operations.md`
- `docs/guides/hub-install-contract.md`
- `docs/guides/remote-docker-acceptance.md`
- `docs/guides/shared-stack.md`

Document first-install account creation, required authentication, email login, local password reset, user-owned browser sessions, and removal of `auth_mode`. Document unknown-machine rejection and the distinction between ownership-establishing install/enrollment and metadata-only ingress refresh.

Keep the local daemon bearer token documented as a separate machine-local credential.

**Acceptance:**

- 3.2.1 - Installation and CLI docs describe the sole initial-user and password-reset flows. behavior: "first-install account bootstrap" in `docs/guides/cli-commands.md`.
- 3.2.2 - Configuration docs contain no supported authentication-disable or config-backed password path. behavior: "authentication configuration" in `docs/guides/configuration.md`.
- 3.2.3 - Web/API docs specify email login and user-owned browser sessions. behavior: "web login contract" in `docs/guides/web-ui.md`.
- 3.2.4 - Ownership docs state that hook/session ingress cannot claim unknown machines. behavior: "machine ownership ingress" in `docs/guides/configuration.md`.
- 3.2.5 - Operator docs bind rehearsal, live cutover, rollback, and the later independent Hub-PC move to one reviewed commit and matching binaries. file: `docs/guides/account-identity-cutover.md`.

## P4: Existing-datastore transition
`kind: framing`

**Goal:** Transform the authoritative laptop datastore transactionally, preserve
all non-auth data, and make the transition reproducible before any live window.

### 4.1 Add the fenced account-identity campaign [category: code] (depends: P3)
`kind: deliverable`

Targets:
- `src/gobby/storage/account_identity_cutover.py::*` — scope-reason: own preflight, transactional mutation, durable evidence, resume, and verification
- `src/gobby/cli/account_identity_cutover.py::*` — scope-reason: collect identity outside SQL and adapt the campaign to hub-maintenance
- `src/gobby/storage/maintenance_epoch.py::open_maintenance_epoch`
- `src/gobby/cli/hub_maintenance.py::_load_campaign_executor`
- `tests/storage/test_account_identity_cutover.py::*` — scope-reason: prove populated predecessor transition and schema invariants
- `tests/cli/test_account_identity_cutover.py::*` — scope-reason: prove prompt sequencing and resume behavior
- `tests/cli/test_hub_maintenance.py::*` — scope-reason: prove campaign registration and fenced verification failure

Add `account-identity-cutover` to the PostgreSQL campaign CHECK constraints,
Python `Campaign` literal, and `CAMPAIGNS`. Before opening the mutation
transaction, require the exact predecessor receipt, no `users` table, zero
non-NULL owners, recorded table counts and invariants, and validated name,
normalized email, and Argon2id password hash.

The prompt-free transaction creates the sole user, assigns its application-side
UUIDv4 to every machine, deletes obsolete auth sessions, replaces UUID/FK/
uniqueness/required constraints, validates row-count and ownership invariants,
then writes the new baseline receipt last. Durable batch evidence makes resume
verification prompt-free and idempotent. Any mutation failure rolls back the
whole transaction while the maintenance epoch remains fenced.

**Acceptance:**

- 4.1.1 - Python campaign registry and both PostgreSQL CHECK constraints admit the exact campaign set, retaining historical `flatten` only in database constraints. test: `tests/storage/test_account_identity_cutover.py::test_campaign_registry_and_admitted_constraints_have_exact_parity`.
- 4.1.2 - Preflight refuses unexpected receipt, existing users, or any non-NULL owner before prompting or mutation. test: `tests/storage/test_account_identity_cutover.py`.
- 4.1.3 - Populated predecessor cutover creates one canonical user, backfills every owner, forces logout, preserves exact non-auth row counts, and verifies the release schema identity. test: `tests/storage/test_account_identity_cutover.py::test_populated_predecessor_cutover_preserves_rows_and_forces_logout`.
- 4.1.4 - Mutation failure rolls back identity and receipt changes, while apply/verification failures retain the maintenance fence and suppress restart. test: `tests/cli/test_hub_maintenance.py::test_verification_failure_keeps_epoch_open_and_daemon_stopped`.
- 4.1.5 - Resume after a committed mutation skips prompts and re-verifies durable evidence. test: `tests/cli/test_account_identity_cutover.py::test_resume_after_commit_skips_prompts`.

### 4.2 Retire receipt refresh and regenerate release identity [category: code] (depends: 4.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: reject obsolete receipts through the native schema authority
- `crates/gcore/src/bootstrap.rs::*` — scope-reason: remove predecessor refresh wiring
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: publish final baseline checksum
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate final catalog identity
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerate final Python contract
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pin final embedded identity and obsolete-receipt rejection
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: pin release daemon identity output
- `src/gobby/utils/native_bin.py::*` — scope-reason: resolve staged rehearsal binaries without changing production default

The dedicated campaign is the sole receipt transition. Native schema startup
rejects the obsolete predecessor receipt. Regenerate baseline checksum, catalog
manifest, release binaries, expected schema identity, and contract literals in
that order. `GOBBY_NATIVE_BIN_DIR` selects staged/rehearsal binaries; production
continues to resolve installed binaries from the normal Gobby bin directory.

**Acceptance:**

- 4.2.1 - Native schema verification rejects the predecessor receipt and accepts only the regenerated target identity. test: `crates/gcore/src/schema/runner_tests.rs`.
- 4.2.2 - Baseline checksum, catalog manifest, expected identity, and Rust contract literals agree exactly. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`. command: `cargo test -p gobby-core --features postgres --test schema_contract embedded_assets_publish_a_complete_schema_identity -- --exact`.
- 4.2.3 - Release `gdaemon` publishes the exact expected identity and `gcode` is built from the same reviewed source. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`. command: `cargo test -p gobby-daemon --test cli_contract version_json_reports_exact_schema_identity_contract -- --exact`.
- 4.2.4 - Rehearsal can resolve immutable staged binaries through `GOBBY_NATIVE_BIN_DIR` while the production fallback remains unchanged. test: `tests/utils/test_native_bin.py`.

### 4.3 Bind rehearsal, live gate, and rollback evidence [category: docs] (depends: 4.2)
`kind: deliverable`

Targets:
- `docs/guides/account-identity-cutover.md`
- `.gobby/plans/hub-pc-datastore-move.md::*` — scope-reason: cross-reference only; preserve the independent move design unchanged

Before live maintenance, stage release binaries and record reviewed SHA,
baseline checksum, expected identity, and binary hashes. Restore a verified hub
backup into an isolated protected `gobby_test` stack with no route to live
services, run the complete campaign, and capture schema identity, ownership
backfill, session invalidation, and exact non-auth row counts.

Evidence and binaries are valid only for the recorded reviewed commit. Any
later commit requires artifact regeneration and complete rehearsal. Live cutover
stops all daemons, takes a fresh verified backup, compares source and artifact
identity with rehearsal, runs the campaign, installs staged binaries, performs
fresh login and task/memory/wiki/code-index/session smoke checks, then soaks
locally. Failure restores backup, source, binaries, old receipt, and row counts
before restart. Cross-reference #19982 for ConfigStore 4.3 identity dependency.

**Acceptance:**

- 4.3.1 - Rehearsal instructions require isolated full-stack endpoints, protected `gobby_test`, verified restore, complete campaign, and exact evidence capture. file: `docs/guides/account-identity-cutover.md`.
- 4.3.2 - Live gate aborts on reviewed SHA, artifact, staged-binary, or rehearsal mismatch. behavior: "reviewed-SHA mismatch" in `docs/guides/account-identity-cutover.md`.
- 4.3.3 - Rollback restores the verified backup plus predecessor source/binaries and verifies old receipt and counts before restart. behavior: "Failure and rollback" in `docs/guides/account-identity-cutover.md`.
- 4.3.4 - Hub-PC migration remains a later independent operation using the transformed schema and matching binaries. file: `.gobby/plans/hub-pc-datastore-move.md`.

## V1 Plan Changelog
`kind: verification`

- Initial draft unified install-time user creation, web credentials, and machine ownership.
- Fable review repair added existing-only hook/session machine refresh, shared PostgreSQL and E2E identity fixtures, complete auth-mode test migration, rate-limit preservation, precise SecretsAuthSection scope, pg_dump baseline ordering, and fail-closed machine-ID resolution.
- Task #19650 implementation revision added the dedicated baseline-375 cutover,
  obsolete-receipt rejection, commit-bound artifact generation, isolated
  rehearsal evidence, live rollback, Hub-PC separation, and #19982 dependency.

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
  tests/cli/test_auth.py \
  tests/storage/test_account_identity_cutover.py \
  tests/cli/test_account_identity_cutover.py \
  tests/cli/test_hub_maintenance.py -v
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

Also run scoped Ruff and mypy, targeted gcore/gdaemon tests, schema
catalog/identity freshness checks, `gobby test-quality` and `gobby test-types`
audits for touched tests, and code-index sweeps for ownerless machine
inserts/upserts plus daemon `auth_mode` references. Run standard and expansion
plan validation before adversarial review and after every review repair. Do not
run the full pytest suite.

Before rebuilding local development state:

1. Stop the daemon.
2. Run `uv run gobby hub-backup` and require verified scratch restore success.
3. Recreate managed PostgreSQL from modified baseline 375.
4. Run interactive `gobby install`.
5. Start the daemon and smoke-test email login, browser WebSocket access, local-token CLI access, unknown-machine rejection, and unauthenticated rejection.

The companion coverage ledger maps every acceptance item exactly once. Every M1
leaf carries `covers:account-identity-machine-ownership:<deliverable-id>:<acceptance-id>`
labels. Validate both artifacts in standard and expansion modes before
adversarial approval and again after review repair.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add canonical user persistence
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Baseline 375 creates users and user-owned auth sessions
    with enforced PK/FK/index contracts. file: `crates/gcore/assets/schema/baseline.sql`.

    1.1.2: User storage supports create, lookup, list, profile update, and password
    update with typed duplicate-email conflicts. symbol: `gobby.storage.users.LocalUserManager`.

    1.1.3: Password hashing retains random salts and the canonical RFC-profile encoding.
    test: `tests/storage/test_users.py`.

    1.1.4: Auth sessions require an existing user while retaining expiry and logout
    behavior. test: `tests/storage/test_auth.py`.'
  labels:
  - covers:account-identity-machine-ownership:1.1:1.1.1
  - covers:account-identity-machine-ownership:1.1:1.1.2
  - covers:account-identity-machine-ownership:1.1:1.1.3
  - covers:account-identity-machine-ownership:1.1:1.1.4
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Enforce ownership and separate registration from refresh
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Every machine has one existing owner and one user can
    enumerate multiple machines. test: `tests/storage/test_machines.py`.

    1.2.2: Same-owner retries succeed and cross-owner claims return a typed conflict
    without mutation. test: `tests/storage/test_machines.py`.

    1.2.3: Hook ingress refreshes known machines and never creates an unknown machine.
    test: `tests/hooks/test_hook_manager.py`.

    1.2.4: Session registration rejects unknown machines before writing a session.
    test: `tests/storage/sessions/test_usage_and_bootstrap.py`.

    1.2.5: Coding-session user identity resolves only through the machine relationship.
    test: `tests/storage/test_users.py`.

    1.2.6: Daemon startup fails closed when no sole installed user can own the local
    machine. test: `tests/test_runner_init.py`.'
  labels:
  - covers:account-identity-machine-ownership:1.2:1.2.1
  - covers:account-identity-machine-ownership:1.2:1.2.2
  - covers:account-identity-machine-ownership:1.2:1.2.3
  - covers:account-identity-machine-ownership:1.2:1.2.4
  - covers:account-identity-machine-ownership:1.2:1.2.5
  - covers:account-identity-machine-ownership:1.2:1.2.6
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Update shared PostgreSQL test identity fixtures
  category: test
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: Shared PostgreSQL fixtures always produce valid user-owned
    machine rows. symbol: `tests.fixtures.postgres.postgres_canonical_seed`.

    1.3.2: Session fixtures reference pre-registered owned machines. file: `tests/storage/sessions/conftest.py`.

    1.3.3: A repository-wide search finds no machine insert or ownership-establishing
    upsert lacking `owner_user_id`. behavior: "owned machine test fixtures" in `tests/fixtures/postgres.py`.'
  labels:
  - covers:account-identity-machine-ownership:1.3:1.3.1
  - covers:account-identity-machine-ownership:1.3:1.3.2
  - covers:account-identity-machine-ownership:1.3:1.3.3
  tdd: false
  source_section: '1.3'
  assigned_agent: backend-developer
- title: Bootstrap the initial user and machine
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: '2.1.1: First install creates exactly one user and its owned
    machine before daemon startup. test: `tests/cli/test_install_identity.py`.

    2.1.2: Database failure rolls back user and machine rows and blocks daemon startup.
    test: `tests/cli/test_install_identity.py`.

    2.1.3: Reruns are idempotent and fresh unattended installs fail with actionable
    guidance. test: `tests/cli/test_install_identity.py`.

    2.1.4: Credential CLI resets the sole user''s password without moving or removing
    identity. test: `tests/cli/test_auth.py`.

    2.1.5: `install.py` remains below 1,000 lines. file: `src/gobby/cli/install.py`.'
  labels:
  - covers:account-identity-machine-ownership:2.1:2.1.1
  - covers:account-identity-machine-ownership:2.1:2.1.2
  - covers:account-identity-machine-ownership:2.1:2.1.3
  - covers:account-identity-machine-ownership:2.1:2.1.4
  - covers:account-identity-machine-ownership:2.1:2.1.5
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Remove the authentication mode switch
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: '2.2.1: No production config, CLI option, or server constructor
    can disable authentication. symbol: `gobby.servers.http.HTTPServer.__init__`.

    2.2.2: Protected HTTP and WebSocket traffic always fails closed without accepted
    credentials. symbol: `gobby.servers.middleware.auth.AuthMiddleware.dispatch`.

    2.2.3: Hook ingress always requires the local token prerequisite. file: `src/gobby/hooks/inbox.py`.

    2.2.4: Bundled bootstrap output contains no `auth_mode`. file: `src/gobby/install/shared/config/bootstrap.yaml`.'
  labels:
  - covers:account-identity-machine-ownership:2.2:2.2.1
  - covers:account-identity-machine-ownership:2.2:2.2.2
  - covers:account-identity-machine-ownership:2.2:2.2.3
  - covers:account-identity-machine-ownership:2.2:2.2.4
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Convert server and E2E tests to authenticated fixtures
  category: test
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.3.1: Shared server clients authenticate without a production-shaped
    bypass. file: `tests/servers/conftest.py`.

    2.3.2: Every E2E daemon boots with a canonical user and owned local machine. symbol:
    `tests.e2e.conftest.e2e_config`.

    2.3.3: No test constructs `DaemonConfig`, `HTTPServer`, or bootstrap YAML with
    daemon `auth_mode`. behavior: "mandatory authentication test contract" in `tests/config/test_bootstrap.py`.

    2.3.4: E2E coverage proves unauthenticated rejection and authenticated HTTP/WebSocket
    success. test: `tests/e2e/test_daemon_auth.py`.'
  labels:
  - covers:account-identity-machine-ownership:2.3:2.3.1
  - covers:account-identity-machine-ownership:2.3:2.3.2
  - covers:account-identity-machine-ownership:2.3:2.3.3
  - covers:account-identity-machine-ownership:2.3:2.3.4
  tdd: false
  source_section: '2.3'
  assigned_agent: backend-developer
- title: Switch login to user email/password
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  validation_criteria: '3.1.1: Browser login authenticates only against canonical
    user email/password and creates a user-owned auth session. test: `tests/servers/routes/test_auth_routes.py`.

    3.1.2: Unknown email and wrong password have the same external response, Argon2
    work class, and rate-limit handling. test: `tests/servers/routes/test_auth_routes.py`.

    3.1.3: Login failure counting, lockout, `Retry-After`, and success reset remain
    intact. symbol: `gobby.servers.routes.auth._LoginRateLimiter`.

    3.1.4: Browser status and application state contain no runtime authentication-mode
    or credential-toggle semantics. test: `web/src/__tests__/App.test.tsx`.

    3.1.5: Secrets settings retain non-auth credential management while removing the
    web-auth editor. test: `web/src/components/settings/sections/__tests__/SecretsAuthSection.test.tsx`.'
  labels:
  - covers:account-identity-machine-ownership:3.1:3.1.1
  - covers:account-identity-machine-ownership:3.1:3.1.2
  - covers:account-identity-machine-ownership:3.1:3.1.3
  - covers:account-identity-machine-ownership:3.1:3.1.4
  - covers:account-identity-machine-ownership:3.1:3.1.5
  tdd: true
  source_section: '3.1'
  implementation_domain: fullstack
- title: Update operator-facing contracts
  category: docs
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: Installation and CLI docs describe the sole initial-user
    and password-reset flows. behavior: "first-install account bootstrap" in `docs/guides/cli-commands.md`.

    3.2.2: Configuration docs contain no supported authentication-disable or config-backed
    password path. behavior: "authentication configuration" in `docs/guides/configuration.md`.

    3.2.3: Web/API docs specify email login and user-owned browser sessions. behavior:
    "web login contract" in `docs/guides/web-ui.md`.

    3.2.4: Ownership docs state that hook/session ingress cannot claim unknown machines.
    behavior: "machine ownership ingress" in `docs/guides/configuration.md`.

    3.2.5: Operator docs bind rehearsal, live cutover, rollback, and the later independent
    Hub-PC move to one reviewed commit and matching binaries. file: `docs/guides/account-identity-cutover.md`.'
  labels:
  - covers:account-identity-machine-ownership:3.2:3.2.1
  - covers:account-identity-machine-ownership:3.2:3.2.2
  - covers:account-identity-machine-ownership:3.2:3.2.3
  - covers:account-identity-machine-ownership:3.2:3.2.4
  - covers:account-identity-machine-ownership:3.2:3.2.5
  tdd: false
  source_section: '3.2'
  assigned_agent: tech-writer
- title: Add the fenced account-identity campaign
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  validation_criteria: '4.1.1: Python campaign registry and both PostgreSQL CHECK
    constraints admit the exact campaign set, retaining historical `flatten` only
    in database constraints. test: `tests/storage/test_account_identity_cutover.py::test_campaign_registry_and_admitted_constraints_have_exact_parity`.

    4.1.2: Preflight refuses unexpected receipt, existing users, or any non-NULL owner
    before prompting or mutation. test: `tests/storage/test_account_identity_cutover.py`.

    4.1.3: Populated predecessor cutover creates one canonical user, backfills every
    owner, forces logout, preserves exact non-auth row counts, and verifies the release
    schema identity. test: `tests/storage/test_account_identity_cutover.py::test_populated_predecessor_cutover_preserves_rows_and_forces_logout`.

    4.1.4: Mutation failure rolls back identity and receipt changes, while apply/verification
    failures retain the maintenance fence and suppress restart. test: `tests/cli/test_hub_maintenance.py::test_verification_failure_keeps_epoch_open_and_daemon_stopped`.

    4.1.5: Resume after a committed mutation skips prompts and re-verifies durable
    evidence. test: `tests/cli/test_account_identity_cutover.py::test_resume_after_commit_skips_prompts`.'
  labels:
  - covers:account-identity-machine-ownership:4.1:4.1.1
  - covers:account-identity-machine-ownership:4.1:4.1.2
  - covers:account-identity-machine-ownership:4.1:4.1.3
  - covers:account-identity-machine-ownership:4.1:4.1.4
  - covers:account-identity-machine-ownership:4.1:4.1.5
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Retire receipt refresh and regenerate release identity
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: Native schema verification rejects the predecessor
    receipt and accepts only the regenerated target identity. test: `crates/gcore/src/schema/runner_tests.rs`.

    4.2.2: Baseline checksum, catalog manifest, expected identity, and Rust contract
    literals agree exactly. test: `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`.
    command: `cargo test -p gobby-core --features postgres --test schema_contract
    embedded_assets_publish_a_complete_schema_identity -- --exact`.

    4.2.3: Release `gdaemon` publishes the exact expected identity and `gcode` is
    built from the same reviewed source. test: `crates/gdaemon/tests/cli_contract.rs::version_json_reports_exact_schema_identity_contract`.
    command: `cargo test -p gobby-daemon --test cli_contract version_json_reports_exact_schema_identity_contract
    -- --exact`.

    4.2.4: Rehearsal can resolve immutable staged binaries through `GOBBY_NATIVE_BIN_DIR`
    while the production fallback remains unchanged. test: `tests/utils/test_native_bin.py`.'
  labels:
  - covers:account-identity-machine-ownership:4.2:4.2.1
  - covers:account-identity-machine-ownership:4.2:4.2.2
  - covers:account-identity-machine-ownership:4.2:4.2.3
  - covers:account-identity-machine-ownership:4.2:4.2.4
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Bind rehearsal, live gate, and rollback evidence
  category: docs
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: '4.3.1: Rehearsal instructions require isolated full-stack
    endpoints, protected `gobby_test`, verified restore, complete campaign, and exact
    evidence capture. file: `docs/guides/account-identity-cutover.md`.

    4.3.2: Live gate aborts on reviewed SHA, artifact, staged-binary, or rehearsal
    mismatch. behavior: "reviewed-SHA mismatch" in `docs/guides/account-identity-cutover.md`.

    4.3.3: Rollback restores the verified backup plus predecessor source/binaries
    and verifies old receipt and counts before restart. behavior: "Failure and rollback"
    in `docs/guides/account-identity-cutover.md`.

    4.3.4: Hub-PC migration remains a later independent operation using the transformed
    schema and matching binaries. file: `.gobby/plans/hub-pc-datastore-move.md`.'
  labels:
  - covers:account-identity-machine-ownership:4.3:4.3.1
  - covers:account-identity-machine-ownership:4.3:4.3.2
  - covers:account-identity-machine-ownership:4.3:4.3.3
  - covers:account-identity-machine-ownership:4.3:4.3.4
  tdd: false
  source_section: '4.3'
  assigned_agent: tech-writer
```
