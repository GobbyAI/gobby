# Epic #17441 work log

## 2026-07-10 — #17789 Add local API token storage and provisioning

Session: #8117

Plan:

1. Inspect the task targets, dependency plan, worktree, and source-file sizes.
2. Add storage, CLI, and runner-startup tests before implementation.
3. Implement the lightweight token helpers, five-case reconciliation, rotation,
   private file writes, daemon startup provisioning, and install provisioning.
4. Run focused tests, lint, type checking, test-quality audit, and size checks.
5. Commit the leaf and close it with the linked commit.

Implemented:

- Added lightweight `local_cli_token` path, read, and bearer-header helpers.
- Added public token hashing plus file/DB reconciliation and rotation.
- Promoted private-file writing to a public helper and enforced mode `0600` on
  both newly created and overwritten files.
- Provisioned the token immediately after secret-envelope initialization during
  daemon startup.
- Provisioned file + hash during `gobby install` with a reachable hub; when the
  hub is unreachable, install writes only the token file for first-start adoption.
- Added tests for all five reconciliation cases, token rotation, helper headers,
  daemon startup ordering, and both install database paths.

TDD evidence:

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/test_auth.py
  tests/cli/test_cli_install.py::TestInstallCommand::test_install_provisions_api_token
  tests/cli/test_cli_install.py::TestInstallCommand::test_install_db_unreachable_writes_file_only
  tests/test_runner_init.py::TestGobbyRunnerInit::test_init_provisions_local_api_token_after_secret_envelope_setup
  -q` failed during collection because `LOCAL_API_TOKEN_HASH_KEY` and the new
  provisioning API did not exist.
- Minimal green: the same command passed 23 tests after implementation.
- Refactor/final green: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/storage/test_auth.py tests/storage/test_secrets_store.py
  tests/cli/test_cli_install.py tests/test_runner_init.py -q` passed 158 tests.

Additional validation:

- `uv run ruff check src/gobby/utils/local_token.py src/gobby/storage/auth.py
  src/gobby/storage/secrets.py src/gobby/runner_init/storage.py
  src/gobby/cli/install.py tests/storage/test_auth.py
  tests/cli/test_cli_install.py tests/runner_helpers.py tests/test_runner_init.py`
  passed.
- `uv run mypy src/gobby/utils/local_token.py src/gobby/storage/auth.py
  src/gobby/storage/secrets.py src/gobby/runner_init/storage.py
  src/gobby/cli/install.py` passed with no issues in five source files.
- `uv run gobby test-quality audit tests/storage/test_auth.py
  tests/cli/test_cli_install.py tests/runner_helpers.py tests/test_runner_init.py
  --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high`
  scanned 87 tests and reported zero issues.
- All touched non-test Python source files remain below 1,000 lines; no refactor
  task was required.

Test gaps: none for this leaf's specified storage, CLI, or daemon-startup behavior.

## 2026-07-10 — #17790 Add auth_mode to bootstrap and daemon config

Session: #8117

Plan:

1. Inspect bootstrap parsing, DaemonConfig composition, the installed template,
   Rust endpoint parsing, and source-file sizes.
2. Add focused Python and Rust tests before implementation.
3. Add typed bootstrap parsing with a required default and hard rejection, flow
   the value into DaemonConfig, and update the bundled template and manifest.
4. Run focused Python and Rust tests, static checks, packaging, and test-quality audit.
5. Commit the leaf and close it with the linked commit.

Implemented:

- Added `AuthMode = Literal["required", "disabled"]` and a strict bootstrap parser.
- Defaulted absent `auth_mode` to `required` and raised `BootstrapConfigError` for
  unsupported values.
- Emitted `auth_mode` through `BootstrapConfig.to_config_dict()` and added the
  top-level `DaemonConfig.auth_mode` field.
- Documented `auth_mode: "required"` in the installed bootstrap template and
  refreshed its bundled-content manifest hash.
- Added focused Python coverage and the Rust `reads_bootstrap_with_auth_mode`
  tolerance test without adding an unused Rust config field.

TDD evidence:

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest tests/config/test_bootstrap.py -q`
  failed both initial tests because `BootstrapConfig` and `DaemonConfig` lacked
  `auth_mode`.
- Minimal green: the same command passed 2 tests after the smallest config parser
  and flow implementation; `cargo test -p gobby-core reads_bootstrap_with_auth_mode`
  passed 1 test and confirmed unknown-key tolerance.
- Refactor/final green: after separating the Python behaviors, `GOBBY_TEST_PROTECT=1
  uv run pytest tests/config/test_bootstrap.py tests/config/test_bootstrap_falkordb.py
  tests/config/test_bootstrap_postgres.py tests/config/test_app_config.py -q` passed
  185 tests. `cargo test -p gobby-core bootstrap::tests` passed 23 tests.

Additional validation:

- `uv run ruff format --check src/gobby/config/bootstrap.py src/gobby/config/app.py
  tests/config/test_bootstrap.py` reported all 3 files formatted.
- `uv run ruff check src/gobby/config/bootstrap.py src/gobby/config/app.py
  tests/config/test_bootstrap.py` passed.
- `uv run mypy --strict src/gobby/config/bootstrap.py src/gobby/config/app.py`
  passed with no issues in 2 source files.
- `cargo clippy -p gobby-core -- -D warnings` passed.
- `cargo fmt --all -- --check` initially found 3 formatting defects in pre-existing
  dirty `gwiki` changes. `cargo fmt --all` fixed them, and the repeated check passed.
- `uv run gobby test-quality audit tests/config/test_bootstrap.py --baseline
  .gobby/test-quality-baseline.json --fail-on-new --min-severity high` scanned 4
  tests and reported zero issues.
- `uv build` successfully built the 0.5.0 source distribution and wheel.
- `git diff --check --` on all #17790 target files passed.
- Touched non-test Python and Rust source files remain below 1,000 lines; no
  refactor task was required.

Test gaps: none for the specified bootstrap parsing, DaemonConfig flow, template,
or Rust unknown-key tolerance behavior.

## 2026-07-10 — #17791 Restructure gobby auth CLI into a token-aware group

Session: #8117

Plan:

1. Inspect the existing auth command, install flow, token storage helpers, CLI
   registration, affected tests, and source-file sizes.
2. Update focused CLI tests before implementation to require the auth group,
   token status and rotation, and bootstrap auth-mode persistence.
3. Move the credential flow under `auth credentials`, add the token command,
   and persist `install --auth-mode` through `update_bootstrap_yaml`.
4. Run focused tests, CLI help checks, formatting, lint, strict type checking,
   test-quality audit, packaging, diff checks, and size checks.
5. Commit the leaf and close it with the linked commit.

Implemented:

- Converted `gobby auth` into a Click group with `credentials` and `token`
  subcommands while preserving the existing credential-management behavior.
- Added token status output for file path, existence, stored hash prefix, and
  file/database agreement.
- Added `--show` for explicit plaintext output and `--rotate` for file + hash
  replacement, including the ~5-second client pickup and remote recopy notices.
- Added the exact `gobby auth token --rotate` remediation string to token help.
- Added `gobby install --auth-mode [required|disabled]` and persisted the chosen
  value to the generated or existing bootstrap file through `update_bootstrap_yaml`.
- Added coverage proving group registration, status/show behavior, real rotation
  invalidating the old token, and install bootstrap persistence.

TDD evidence:

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_auth.py
  tests/cli/test_cli_install.py::test_install_auth_mode_flag -q` failed 9 tests:
  the old `auth` command rejected subcommands, `auth.commands` was absent, and
  the rotation and bootstrap update symbols/options did not exist.
- Minimal green: the same command passed 9 tests after the smallest group,
  token-status, rotation, and install option implementation.
- Refactor red: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/cli/test_auth.py::test_auth_group_has_credentials_and_token_commands
  tests/cli/test_auth.py::test_auth_token_rotate -q` passed real rotation and
  failed only because token help lacked the exact remediation command.
- Refactor/final green: after adding help remediation and extracting bootstrap
  mutation, `GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_auth.py
  tests/cli/test_cli_install.py::test_install_auth_mode_flag -q` passed 9 tests.
- Broader focused green: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/cli/test_auth.py tests/cli/test_cli_install.py -q` passed 49 tests.

Additional validation:

- `uv run ruff format src/gobby/cli/auth.py src/gobby/cli/install.py
  tests/cli/test_auth.py tests/cli/test_cli_install.py` left all 4 files unchanged.
- `uv run ruff check src/gobby/cli/auth.py src/gobby/cli/install.py
  tests/cli/test_auth.py tests/cli/test_cli_install.py` passed.
- `uv run mypy --strict src/gobby/cli/auth.py src/gobby/cli/install.py` passed
  with no issues in 2 source files.
- `uv run gobby test-quality audit tests/cli/test_auth.py
  tests/cli/test_cli_install.py --baseline .gobby/test-quality-baseline.json
  --fail-on-new --min-severity high` scanned 48 tests and reported zero issues.
- `uv run gobby auth --help`, `uv run gobby auth token --help`, and
  `uv run gobby install --help` all exited successfully and exposed the expected
  subcommands, remediation command, and auth-mode option.
- `uv build` successfully built the 0.5.0 source distribution and wheel.
- `git diff --check` passed.
- `src/gobby/cli/auth.py` is 92 lines and `src/gobby/cli/install.py` is 927
  lines; both remain below 1,000, so no refactor task was required.

Test gaps: none for this leaf's specified CLI grouping, status, rotation, help,
or bootstrap-persistence behavior.

## 2026-07-10 — #17792 Thread token through Python daemon clients

Session: #8117

Plan:

1. Inspect each daemon client, raw HTTP call, inbox replay path, code-index
   isolation setup, focused tests, and source-file sizes.
2. Add the six acceptance behaviors and raw-call assertions before implementation.
3. Cache and attach bearer headers, retry stdio requests once after a 401 token
   refresh, sanitize replay headers, and copy the token into isolated homes.
4. Run focused red/green tests, broader affected suites, static checks,
   test-quality audit, packaging, diff checks, and size checks.
5. Commit the leaf and close it with the linked commit.

Implemented:

- Cached `daemon_auth_headers()` in `DaemonClient` and applied it to health and
  GET/POST/PUT/DELETE requests; 401s now return or raise the exact actionable
  install/rotate/copy remediation.
- Cached bearer headers in `DaemonProxy`, attached them to every request, and
  re-read the token file and retried exactly once after a 401.
- Attached bearer headers to terminal-context session bootstrap and rich status.
- Added auth headers to all five clone calls, agent spawn, worktree tool calls,
  and both workflow reload calls while preserving bare public port health probes.
- Stripped persisted Authorization headers case-insensitively during inbox replay,
  attached the current local token, and emitted one actionable warning per drain
  cycle in required mode when the token is missing.
- Copied `local_cli_token` with mode `0600` into distinct code-index isolation
  homes alongside `bootstrap.yaml`.
- Added the exact acceptance test paths plus focused CLI, warning, bootstrap,
  and isolated-home coverage.

TDD evidence:

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest tests/utils/test_daemon_client.py
  tests/mcp_proxy/test_stdio_proxy.py
  tests/hooks/test_inbox.py::test_replay_attaches_token
  tests/hooks/test_inbox.py::test_missing_required_token_warns_once_per_drain
  tests/utils/test_status.py::test_fetch_rich_status_sends_bearer
  tests/agents/test_isolation.py::TestEnsureIsolationCodeIndex::test_database_url_creates_gcode_wrapper_runtime
  tests/cli/test_clones_cli.py::TestClonesCreateCommand::test_create_clone_success
  tests/cli/test_clones_cli.py::TestClonesSpawnCommand::test_spawn_agent_success
  tests/cli/test_clones_cli.py::TestClonesSyncCommand::test_sync_clone_success
  tests/cli/test_clones_cli.py::TestClonesMergeCommand::test_merge_clone_success
  tests/cli/test_clones_cli.py::TestClonesDeleteCommand::test_delete_clone_success
  tests/cli/test_cli_agents.py::TestAgentsSpawnCommand::test_spawn_success
  tests/cli/test_worktrees_cli.py::test_create_worktree_success
  tests/cli/test_workflows_coverage.py::test_reload_workflows_success -q`
  failed all 16 checks for the intended missing imports, headers, retry,
  remediation, replay sanitization, warning, and isolated-token copy behavior.
- Minimal green: the same exact command passed all 16 tests after the scoped
  implementation and correction of copied-token permissions from `0700` to `0600`.
- Refactor/final green: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/utils/test_utils_daemon_client.py tests/utils/test_daemon_client.py
  tests/utils/test_status.py tests/mcp_proxy/test_stdio_proxy.py
  tests/mcp_proxy/test_mcp_proxy_stdio_session_context.py
  tests/mcp_proxy/test_mcp_proxy_stdio.py::TestDaemonProxy
  tests/mcp_proxy/test_mcp_proxy_stdio.py::TestDaemonProxyMethods
  tests/hooks/test_inbox.py
  tests/agents/test_isolation.py::TestEnsureIsolationCodeIndex
  tests/cli/test_clones_cli.py
  tests/cli/test_cli_agents.py::TestAgentsSpawnCommand
  tests/cli/test_worktrees_cli.py tests/cli/test_workflows_coverage.py -q`
  passed 149 tests after formatting and assertion cleanup.

Additional validation:

- `uv run ruff format` on all 19 touched Python source/test files reformatted one
  file; the repeated focused suite remained green.
- `uv run ruff check` on the same 19 files passed.
- `uv run mypy --strict` on all 10 touched source files passed with no issues.
- `uv run gobby test-quality audit tests/utils/test_daemon_client.py
  tests/mcp_proxy/test_stdio_proxy.py tests/hooks/test_inbox.py
  tests/utils/test_status.py tests/agents/test_isolation.py
  tests/cli/test_clones_cli.py tests/cli/test_cli_agents.py
  tests/cli/test_worktrees_cli.py tests/cli/test_workflows_coverage.py
  --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high`
  initially identified one new medium issue; after strengthening replay response,
  route, and payload assertions, it scanned 189 tests and reported zero issues.
- `gcode grep` confirmed nine `headers=daemon_auth_headers()` raw call sites and
  confirmed the three enumerated public health probes remain bare.
- `uv build` successfully built the 0.5.0 source distribution and wheel.
- `git diff --check` passed.
- All 10 touched non-test Python source files remain below 1,000 lines; no
  refactor task was required.

Test gaps: none for the specified client, retry, replay, rich-status, raw-call,
or isolated-home behaviors.

## 2026-07-10 — #17793 Add token to installed git-hook curl template

Session: #8117

Plan:

1. Inspect the generated reindex body, existing hook-template coverage, worktree
   state, and source-file size.
2. Add the exact acceptance test before implementation and capture its intended
   bearer-header failure.
3. Read the optional local token into a Bash argument array and expand it at both
   codewiki-refresh curl sites.
4. Exercise both jq and shell-fallback branches with and without a token, run
   focused regressions and static checks, audit test quality, commit, and close.

Implemented:

- Read `${GOBBY_HOME:-$HOME/.gobby}/local_cli_token` when readable and non-empty.
- Stored the optional `-H "Authorization: Bearer $TOKEN"` pair in a Bash array,
  preserving a zero-argument expansion when the file is missing or empty.
- Expanded the array at both the jq-piped and shell-escaped curl branches.
- Added `tests/cli/installers/test_git_hooks.py::test_hook_body_includes_token`,
  which executes both branches against isolated fake executables for token-present
  and token-missing cases.

TDD evidence:

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/cli/installers/test_git_hooks.py::test_hook_body_includes_token -q`
  failed because `Authorization: Bearer test-token` was absent from captured curl
  arguments.
- Minimal green: the same exact command passed 1 test after adding the optional
  header array and expanding it at both curl sites.
- Refactor/final green: after extending the harness to execute both curl branches,
  the command exposed a test-only PATH defect (`FileNotFoundError` for `bash`).
  Using `/bin/bash` fixed the harness; the same exact command then passed 1 test.

Additional validation:

- `GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/installers/test_git_hooks.py
  tests/cli/installers/test_git_hooks_installer.py::TestHookTemplates -q` passed
  all 6 focused acceptance and existing template tests.
- `uv run ruff format src/gobby/cli/installers/git_hooks.py
  tests/cli/installers/test_git_hooks.py` left both files unchanged.
- `uv run ruff check src/gobby/cli/installers/git_hooks.py
  tests/cli/installers/test_git_hooks.py` passed.
- `uv run mypy --strict src/gobby/cli/installers/git_hooks.py
  tests/cli/installers/test_git_hooks.py` passed with no issues.
- `uv run gobby test-quality audit tests/cli/installers/test_git_hooks.py
  --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high`
  scanned 1 test and reported zero issues.
- `gcode grep` confirmed two optional-header expansions, one in each curl branch.
- `src/gobby/cli/installers/git_hooks.py` ends at line 673 and remains below
  1,000, so no refactor task was required.

Test gaps: none for token-present, token-missing, jq, fallback, generated-template,
or marker-installed behavior.

## 2026-07-10 — #17794 Send token from Rust clients and remove dead broker paths

Session: #8117

Plan:

1. Inspect the shared AI transport, ghook delivery/diagnostics, graph lifecycle,
   and standalone database resolver paths with gcode.
2. Add focused loopback HTTP tests before each client implementation and capture
   the missing-bearer failures.
3. Centralize token reading and bearer formatting in gcore, authenticate Rust
   daemon requests, and extend ghook diagnostics.
4. Remove both dead database-broker clients and their loopback guards/fixtures,
   run focused crate validation, build release binaries, reinstall, commit, and
   close.

Implemented:

- Added public `gobby_core::local_token` with the canonical token filename,
  Authorization header name, trimmed token reader, and `authorization_bearer`.
- Delegated the gcore AI daemon transport to the shared helper and migrated its
  HTTP requests from `X-Gobby-Local-Token` to `Authorization: Bearer`.
- Made ghook read the token best-effort, strip persisted Authorization headers,
  attach the fresh bearer when available, and send a truly bare request when the
  token file is missing.
- Extended ghook diagnose JSON/schema with token-file presence and actionable
  401 remediation guidance.
- Added the bearer to gcode graph clear/rebuild lifecycle POSTs.
- Removed `/api/local/runtime/database-url`, broker timeouts/token readers,
  loopback-only URL guards, validation helpers, and broker fixtures from gcode
  and gwiki. Standalone resolution now uses direct environment/bootstrap/gcore
  DSN sources.
- Rebuilt and reinstalled release `ghook`, `gcode`, and `gwiki` binaries under
  `~/.gobby/bin`.

TDD evidence:

- Red: `cargo test -p gobby-hooks post_includes_bearer_when_token_present --
  --nocapture` failed because the loopback server received no
  `Authorization: Bearer ghook-test-token` header.
- Minimal green: the same exact command passed 1 test after the shared helper and
  ghook bearer attachment were added.
- Second red: `cargo test -p gobby-code lifecycle_post_includes_bearer --
  --nocapture` failed because the graph lifecycle POST lacked
  `Authorization: Bearer gcode-test-token`.
- Second green: the same exact command passed 1 test after the lifecycle request
  used the shared helper.
- Refactor/final green: the exact ghook command passed after the test was
  strengthened with a stale persisted Authorization header. The companion
  `cargo test -p gobby-hooks post_omits_authorization_when_token_missing --
  --nocapture` also passed, proving the disabled-mode fallback stays bare.

Additional validation:

- `cargo test -p gobby-hooks` passed 105 unit, 18 contract, and 3 inbox-fallback
  tests.
- `cargo test -p gobby-core --features ai ai::daemon::tests -- --nocapture`
  passed 14 tests; the focused daemon-agentic header test passed separately.
- `cargo test -p gobby-code db::resolution::tests -- --nocapture` passed 12
  resolver tests; `cargo test -p gobby-code lifecycle_post_includes_bearer --
  --nocapture` passed 1 focused lifecycle test.
- `cargo test -p gobby-wiki support::env::tests -- --nocapture` passed 2
  standalone resolver tests.
- `cargo fmt --all -- --check` passed.
- `cargo clippy -p gobby-core -p gobby-hooks -p gobby-code -p gobby-wiki
  --features gobby-core/ai --all-targets -- -D warnings` passed. The gate also
  exposed concurrent codewiki/gwiki compile drift; those shared work paths were
  reconciled and left outside this leaf's staged diff.
- `uv run gobby test-quality audit` on all six touched Rust test-bearing files
  with the repository baseline scanned 60 tests and reported zero issues.
- `gcode grep` returned no dead broker route, no loopback guard, and no legacy
  Rust `X-Gobby-Local-Token` occurrence in the affected clients.
- `cargo build --release -p gobby-hooks -p gobby-code -p gobby-wiki --bins`
  passed. `cmp` confirmed each installed binary byte-matches its release artifact;
  versions report `ghook 0.7.1`, `gcode 1.5.0`, and `gwiki 0.8.0`.
- `git diff --check` passed on all leaf paths.

Test gaps: none for shared token reading/formatting, gcore AI headers, ghook
token-present/token-missing/stale-header behavior, graph lifecycle auth,
diagnostics, broker deletion, standalone DSN fallback, or installed binaries.

Commit attribution: code commit `1a8c23a4f` contains the 13 Rust client and
resolver paths. Documentation commit `358511ea0` records this work-log entry.
The transient mixed commit `8f12ad86c` was replaced by its owner and explicitly
unlinked from #17794.

## 2026-07-10 — #17795 Implement AuthService and construct it in HTTPServer

Session: #8117

Plan:

1. Inspect AuthStore, ConfigStore, HTTPServer construction/call sites, and plan
   section 3.1 with gcode; verify source-size constraints.
2. Add the smallest storage-backed acceptance tests and capture an exact red
   failure before implementation.
3. Implement the shared AuthService, phase-default HTTPServer ownership, and
   dead `run_server` deletion; capture minimal green.
4. Refine rotation revocation, run focused regressions, lint, typing, and test
   quality; commit and close.

Implemented:

- Added a typed `AuthService` that reads `auth.api_token_hash`, `auth.username`,
  and `auth.password_hash` directly through ConfigStore and reads the daemon's
  plaintext token file on the same lock-guarded refresh path.
- Debounced refreshes at 5 seconds and re-verifies bearer hashes with
  `hmac.compare_digest`. Periodic pre-verification refresh revokes a rotated old
  token within the same bound even when only a stale client connects.
- Enforced request credential precedence: Bearer header, local-token alias,
  then session cookie. Added AuthStore session delegation and the async
  WebSocket callback shape.
- Added scrypt password-hash verification with fixed `n=16384`, `r=8`, `p=1`,
  `dklen=32` parameters and timing-safe username/digest comparisons.
- Made HTTPServer own AuthService through a new `auth_mode` kwarg. This phase
  resolves only explicit kwarg or `_PHASE_DEFAULT_AUTH_MODE = "disabled"` and
  never consults the config snapshot or `test_mode`.
- Removed dead production `run_server` and its obsolete unit-test class.

TDD evidence:

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/servers/test_auth_service.py::test_verify_bearer_rotation_refresh -q`
  failed during collection with `ModuleNotFoundError: No module named
  'gobby.servers.auth_service'` before implementation existed.
- Minimal green: the same exact command passed 1 test after the shared service
  and mechanical HTTPServer ownership were added.
- Rotation hardening red: after strengthening the same test to require stale
  token revocation at the debounce boundary, the exact command failed because
  `verify_bearer("old-token")` still returned true.
- Rotation hardening green: the same exact command passed 1 test after bearer
  verification performed its debounced refresh before comparing.
- Refactor/final green: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/servers/test_auth_service.py tests/servers/test_http_init.py
  tests/servers/test_http_middleware.py tests/servers/routes/test_auth_routes.py
  -q` passed all 39 tests.

Additional validation:

- `uv run ruff format --check src/gobby/servers/auth_service.py
  src/gobby/servers/http.py tests/servers/test_auth_service.py
  tests/servers/test_http_init.py` passed with all four files formatted.
- `uv run ruff check` on the same four paths passed.
- `uv run mypy src/gobby/servers/auth_service.py
  src/gobby/servers/http.py` passed with no issues.
- `uv run gobby test-quality audit tests/servers/test_auth_service.py
  tests/servers/test_http_init.py --baseline
  .gobby/test-quality-baseline.json --fail-on-new --min-severity high` scanned
  25 tests and reported zero issues.
- `gcode grep -w "run_server" src/gobby -m 50` returned no occurrences.
- `src/gobby/servers/auth_service.py` and `src/gobby/servers/http.py` remain
  below 1,000 lines, so no refactor task was required.

Test gaps: none for the six named 3.1 acceptance items or the retained HTTP/auth
route behavior covered by the focused regression files.

## 2026-07-10 — #17796 Rewrite middleware and auth routes on AuthService

Session: #8117

Plan:

1. Inspect middleware, auth routes, credential CLI, startup storage, config
   hydration, and adjacent tests with gcode; verify the shared AuthService
   contract from #17795.
2. Add the four named 3.2 acceptance tests plus CLI/config regressions and
   capture an exact red failure.
3. Centralize scrypt hashing, rewrite middleware/routes, migrate legacy Fernet
   credentials atomically, and update adjacent legacy tests.
4. Run focused auth/config/startup regressions, strict typing, lint, security,
   test-quality, and build checks; commit and close.

Implemented:

- Moved the canonical password hash/verify primitives into
  `gobby.storage.auth` so CLI setup, daemon migration, and AuthService share the
  exact `scrypt$16384$8$1$<salt_b64>$<hash_b64>` representation.
- Rewrote AuthMiddleware to use `server.auth_service.enabled` and
  `is_request_authenticated`, preserving the phase's existing public-prefix
  list and returning the required CLI/browser remediation in API 401 bodies.
- Deleted `_get_auth_credentials`, `is_auth_enabled`, and
  `validate_session_cookie`; login now uses AuthService timing-safe password
  verification, and status reports `auth_required`, `authenticated`, and
  `credentials_configured`.
- Changed `gobby auth credentials` to persist only the scrypt hash through
  ConfigStore and clean up the legacy `auth.password` secret/reference on set,
  reset, or removal.
- Added an atomic startup migration after secret-envelope setup and local-token
  provisioning. It hashes a decryptable legacy password, removes both legacy
  rows, and logs the required `gobby auth credentials` recovery instruction on
  failure.
- Removed dead `AuthConfig.password` and `session_secret` fields, explicitly
  retained `extra="ignore"` for internal hash keys, and updated configuration
  regressions so the removed credential surface stays absent.

TDD evidence:

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/servers/test_auth_service.py::test_legacy_password_migration
  tests/servers/test_http_middleware.py::test_bearer_and_alias_accepted
  tests/servers/routes/test_auth_routes.py::TestAuthStatus::test_status_credentials_configured
  tests/cli/test_auth.py::test_auth_credentials_store_scrypt_hash -q` failed
  during collection with `ImportError: cannot import name 'hash_password' from
  'gobby.servers.auth_service'` before the shared hashing API existed.
- Minimal green: the same exact command passed all 4 tests after the storage
  hashing API, migration, middleware, route, and CLI changes. It exposed one
  Starlette per-request-cookie deprecation warning, which was removed by using
  the client's cookie jar.
- Refactor/final green: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/servers/test_auth_service.py tests/servers/test_http_middleware.py
  tests/servers/test_auth_middleware.py tests/servers/routes/test_auth_routes.py
  tests/servers/routes/test_configuration_routes.py
  tests/servers/routes/test_communications.py tests/cli/test_auth.py
  tests/test_runner_init.py tests/storage/test_auth.py -q` passed all 233 tests
  with no warnings.

Additional validation:

- `uv run ruff format --check` and `uv run ruff check` passed on all 8 touched
  production files and 10 touched test files.
- `uv run mypy --strict` passed on all 8 touched production files.
- `uv run bandit -c pyproject.toml` found no issues in the touched production
  files.
- `uv run gobby test-quality audit ... --baseline
  .gobby/test-quality-baseline.json --fail-on-new --min-severity high` scanned
  220 tests across the 10 touched test paths and reported zero issues.
- `uv build` successfully built the source distribution and wheel.
- Every touched non-test Python file remains below 1,000 lines; the largest is
  `src/gobby/runner_init/storage.py` at 250 lines, so no refactor task was
  required.

Test gaps: none for the five named 3.2 acceptance items. Focused regression
coverage also proves public-route exemptions, disabled mode, logout, config
masking, startup ordering, CLI removal/reset, and token provisioning remain
intact.

## 2026-07-10 — #17797 Wire WebSocket auth and browser proxy bearer bridge

Session: #8117

Plan:

1. Inspect standalone WebSocket construction, handshake authentication, the
   HTTP `/ws` proxy, AuthService credential APIs, and adjacent tests with
   gcode; preserve concurrent #17757 work.
2. Add the named standalone wiring and proxy cookie-bridge acceptance tests,
   plus a localhost fallback assertion, then capture the exact pre-change red
   failure.
3. Wire `AuthService.verify_ws_token` into the standalone server when auth is
   enabled; authenticate proxy upgrades before upstream connection and inject
   the daemon bearer token into that upstream connection.
4. Run minimal green, refactor/final-green focused regressions, lint, strict
   typing, security, test-quality, build, and source-size checks; commit and
   close.

Test judgment:

- Runner-construction integration plus the standalone server's handshake
  authenticator prove rejection and bearer acceptance through the callback
  supplied during initialization.
- ASGI endpoint tests prove `/ws` closes with 4401 before proxying for missing
  credentials and bridges both a session cookie and bearer header into one
  daemon-owned upstream bearer token.
- A focused unit assertion proves the fallback WebSocket bind remains
  localhost.

Implemented:

- Runner initialization now supplies `AuthService.verify_ws_token` to the
  standalone WebSocket server exactly when shared authentication is enabled;
  disabled mode retains the local-first callback-free handshake.
- Both `/ws` endpoint shapes authenticate the incoming WebSocket connection
  through AuthService before opening an upstream connection. Missing or invalid
  credentials close with code 4401, while a missing daemon token after a valid
  browser credential closes with 1011.
- Authenticated proxy connections inject the install-scoped daemon token using
  websockets 16.0 `additional_headers`. Disabled-auth and Vite HMR proxy paths
  retain their header-free connection shape.
- Widened the shared request-authentication boundary from Starlette `Request`
  to its `HTTPConnection` base so HTTP requests and WebSocket upgrades use the
  same typed credential parser.
- Corrected the standalone server example to bind to `localhost`; the
  `WebSocketConfig` default was already localhost and now has an explicit
  regression assertion.

TDD evidence:

- Red: `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/servers/websocket/test_auth.py::test_wired_callback_rejects_and_accepts
  tests/servers/test_ws_proxy_auth.py::test_proxy_rejects_missing_credentials
  tests/servers/test_ws_proxy_auth.py::test_proxy_cookie_bridge
  tests/servers/test_ws_proxy_auth.py::test_proxy_bearer_bridge
  tests/servers/websocket/test_server.py::test_default_bind_is_localhost -q`
  collected 5 tests and failed the four new auth behaviors: callback was absent,
  bare proxy traffic closed 1011, and both accepted proxy paths omitted
  `additional_headers`. The pre-existing localhost default assertion passed.
- Minimal green: the same exact command passed all 5 tests after callback
  wiring, pre-proxy authentication, and upstream bearer injection.
- Refactor/final green: the same exact command passed all 5 tests after keeping
  the existing header-free connection signature for disabled-auth and HMR
  proxy traffic and exercising the real standalone WebSocket server
  authenticator.

Additional validation:

- `GOBBY_TEST_PROTECT=1 uv run pytest
  tests/servers/websocket/test_auth.py tests/servers/websocket/test_server.py
  tests/servers/test_ws_proxy_auth.py tests/servers/test_app_factory_ui_modes.py
  tests/servers/test_auth_service.py tests/servers/test_http_middleware.py -q`
  passed all 49 focused regression tests without warnings.
- `uv run ruff format --check` and `uv run ruff check` passed on all 7 touched
  source and test files.
- `uv run mypy --strict` passed on all 4 touched production files.
- `uv run bandit -q -c pyproject.toml` found no issues in the 4 touched
  production files.
- `uv run gobby test-quality audit ... --baseline
  .gobby/test-quality-baseline.json --fail-on-new --min-severity high` scanned
  29 tests across the 3 touched test files and reported zero issues.
- `uv build` successfully built the source distribution and wheel.
- Every touched non-test Python file remains below 1,000 lines; the largest is
  `src/gobby/servers/websocket/server.py` at 433 lines, so no refactor task was
  required.

Test gap: live-daemon end-to-end coverage belongs to dependent task #17800.
This leaf covers its construction, handshake, ASGI endpoint, proxy transport,
shared credential parsing, and HMR compatibility boundaries in isolation.
