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
