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
