# Sandbox hub credential isolation validation

Task: #19562
Implementation commit: b09b0456d
Validation hub: postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test

This records the WP7 exit evidence and the eight system-property gates required
by sandbox-hub-credential-isolation.md. Every pytest command used
GOBBY_TEST_PROTECT=1, the isolated PostgreSQL hub above, and a fresh basetemp.

## TDD evidence

Initial red, 7 failed because the cutover behaviors did not exist:

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/agents/test_isolation.py::TestEnsureIsolationCodeIndex::test_reaps_legacy_shared_dsn_bootstrap_and_preserves_scoped_bootstrap tests/cli/hub_backup/test_stores.py::test_collect_source_roles_skips_builtin_roles_and_reports_flags tests/cli/hub_backup/test_stores.py::test_dump_postgres_captures_cluster_globals_only tests/cli/hub_backup/test_stores.py::test_dump_postgres_aborts_while_ephemeral_login_remains tests/cli/hub_backup/test_cli.py::TestRestore::test_restore_uses_explicit_target_and_verified_hub_artifact tests/cli/test_postgres_cli.py::test_postgres_scoped_roles_lists_metadata_without_credentials tests/cli/test_postgres_cli.py::test_postgres_force_revoke_run_uses_execution_id -q --basetemp=/tmp/gobby-pytest-19562-red

Minimal green, 8 passed:

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/agents/test_isolation.py::TestEnsureIsolationCodeIndex::test_reaps_stale_runtime_credentials_and_kek tests/agents/test_isolation.py::TestEnsureIsolationCodeIndex::test_reaps_legacy_shared_dsn_bootstrap_and_preserves_scoped_bootstrap tests/cli/hub_backup/test_stores.py::test_collect_source_roles_skips_builtin_roles_and_reports_flags tests/cli/hub_backup/test_stores.py::test_dump_postgres_captures_cluster_globals_only tests/cli/hub_backup/test_stores.py::test_dump_postgres_aborts_while_ephemeral_login_remains tests/cli/hub_backup/test_cli.py::TestRestore::test_restore_uses_explicit_target_and_verified_hub_artifact tests/cli/test_postgres_cli.py::test_postgres_scoped_roles_lists_metadata_without_credentials tests/cli/test_postgres_cli.py::test_postgres_force_revoke_run_uses_execution_id -q --basetemp=/tmp/gobby-pytest-19562-green1

Bounded review found two additional defects. The stable-role test first failed
because gobby_agent_issuer was omitted, then passed after generated-role
filtering was narrowed. The globals replay test first failed because CREATE ROLE
was non-idempotent, then passed after duplicate_object handling was added while
ON_ERROR_STOP remained enabled.

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/cli/hub_backup/test_stores.py::test_collect_source_roles_skips_builtin_roles_and_reports_flags -q --basetemp=/tmp/gobby-pytest-19562-stable-role-red
    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/cli/hub_backup/test_stores.py::test_collect_source_roles_skips_builtin_roles_and_reports_flags -q --basetemp=/tmp/gobby-pytest-19562-stable-role-green
    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/cli/hub_backup/test_stores.py::test_restore_postgres_globals_tolerates_existing_stable_roles -q --basetemp=/tmp/gobby-pytest-19562-globals-red
    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/cli/hub_backup/test_stores.py::test_restore_postgres_globals_tolerates_existing_stable_roles -q --basetemp=/tmp/gobby-pytest-19562-globals-green

## WP7 exit evidence

Upgrade cleanup is covered by the runtime reaper tests. They prove shared-DSN
bootstraps, local tokens, KEK files, and KEK symlinks are removed before gcode
launch while a valid generated-role bootstrap remains usable.

Backup and restore are covered by the hub-backup tests and migration integration
tests. The source-role assertion retains gobby_agent_issuer,
gobby_daemon_runtime, and gobby_gcode_capability and omits generated login
roles. Backup aborts while a generated login remains. Restore orders globals,
data, then principal drain; the PostgreSQL integration test proves drain removes
both login authority and the role.

The full lifecycle matrix command below covers issue, use, rotate, resume,
normal exit, crash, timeout, forced revoke, expiry, daemon restart,
tool-request cancellation, and provider-native refusal:

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/storage/test_managed_credentials.py::test_issue_materializes_private_bootstrap_and_revoke_terminates_sessions tests/storage/test_managed_credentials.py::test_rotation_race_creates_one_successor_and_drains_the_predecessor tests/storage/test_managed_credentials.py::test_restart_reconcile_revokes_expired_role_with_a_live_connection tests/storage/test_storage_agents.py::test_terminal_transitions_revoke_managed_credentials_once tests/storage/test_storage_agents.py::TestLocalAgentRunManager::test_cleanup_stale_runs tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation::test_restart_reconciles_and_rotates_managed_credentials_without_agent_runner tests/test_runner_lifecycle_restart_replay.py::TestAgentRestartReconciliation::test_reconcile_missing_tmux_session_parks_and_resumes_run tests/cli/test_postgres_cli.py::test_postgres_force_revoke_run_uses_execution_id tests/ai/test_managed_tool_chat_lease.py tests/agents/test_isolation.py::TestEnsureIsolationCodeIndex::test_cancelling_gcode_run_kills_child_process tests/agents/test_spawn_executor.py::test_required_managed_code_index_preflight_fails_closed -q --basetemp=/tmp/gobby-pytest-19562-lifecycle-evidence

Result: 16 passed.

The repository-wide credential-fixture scan derives unique operator markers at
runtime so the values themselves are absent from this document. It scans every
tracked file and every regular runtime file, emits counts only, asserts every
count is zero, and returned zero for DSN, password, token, and KEK in both
scopes:

    uv run python - <<'PY'
    from __future__ import annotations

    import json
    import subprocess
    from pathlib import Path

    repo = Path.cwd()
    runtime_root = Path.home() / ".gobby" / "runtime"
    nonce = "71f09ad9" + "4fdd3fc7"
    markers = {
        "operator_dsn": (
            "postgresql://wp7_operator:WP7-fixture-password-"
            + nonce
            + "@127.0.0.1:65432/wp7"
        ).encode(),
        "operator_password": ("WP7-fixture-password-" + nonce).encode(),
        "operator_token": ("wp7_fixture_token_" + nonce + "7d5b49f7").encode(),
        "operator_kek": ("wp7_fixture_kek_" + nonce + "73584313").encode(),
    }
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True
    ).stdout.split(b"\0")
    paths = {
        "repository": [repo / raw.decode() for raw in tracked if raw],
        "runtime": [
            path
            for path in runtime_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        ] if runtime_root.exists() else [],
    }
    result: dict[str, dict[str, int]] = {}
    for scope, scope_paths in paths.items():
        result[scope] = {name: 0 for name in markers}
        for path in scope_paths:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            for name, marker in markers.items():
                result[scope][name] += data.count(marker)
    print(json.dumps(result, sort_keys=True))
    assert all(count == 0 for scope in result.values() for count in scope.values())
    PY

Result:

    {"repository": {"operator_dsn": 0, "operator_kek": 0, "operator_password": 0, "operator_token": 0}, "runtime": {"operator_dsn": 0, "operator_kek": 0, "operator_password": 0, "operator_token": 0}}

## Eight isolated-hub gates

1. Sensitive root bootstrap, token, and KEK paths are unreadable and
   unwritable; SRT executable mutation is denied. Result: 5 passed.

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/agents/test_srt_filesystem_integration.py::test_supported_backend_blocks_sensitive_path_traversal_and_later_launch_persistence tests/agents/test_sandbox.py::TestComputeSandboxPaths::test_sensitive_gobby_roots_are_effectively_denied tests/agents/test_sandbox.py::TestToolchainGrants::test_toolchain_credentials_are_effectively_denied tests/agents/test_isolation.py::TestEnsureIsolationCodeIndex::test_runtime_home_excludes_kek_and_links_non_secret_assets --run-sandbox -q --basetemp=/tmp/gobby-pytest-19562-gate1

2. SRT installation and executable content are verified, including corruption
   and unmanifested-content refusal. Result: 10 passed.

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/cli/test_install_setup_srt.py tests/agents/test_srt_runtime.py::test_verify_srt_installation_wraps_missing_lockfile tests/agents/test_srt_runtime.py::test_verify_srt_installation_accepts_release_contract tests/agents/test_srt_runtime.py::test_verify_srt_installation_rejects_unmanifested_package_content tests/agents/test_srt_runtime.py::test_verify_srt_installation_rejects_corruption -q --basetemp=/tmp/gobby-pytest-19562-gate2

3. Scoped DSNs cannot cross project or gcode-domain boundaries. Result:
   8 passed.

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/storage/test_postgres_agent_authorization.py -q --basetemp=/tmp/gobby-pytest-19562-gate3

4. Gcode retains its direct PostgreSQL data plane. Result: 2 passed with the
   real gcode writer.

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/code_index/test_gcode_storage_conformance.py::test_real_gcode_writer_matches_python_model_contract tests/agents/test_isolation.py::TestEnsureIsolationCodeIndex::test_scoped_credential_creates_gcode_wrapper_runtime -q --basetemp=/tmp/gobby-pytest-19562-gate4

5. Expiry, every agent terminal path, crash recovery, and every tool-request
   exit revoke authority. Result: 16 passed.

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/storage/test_managed_credentials.py tests/storage/test_storage_agents.py::test_terminal_transitions_revoke_managed_credentials_once tests/storage/test_storage_agents.py::TestLocalAgentRunManager::test_cleanup_stale_runs tests/ai/test_managed_tool_chat_lease.py -q --basetemp=/tmp/gobby-pytest-19562-gate5

6. Web chat applies daemon-owned sandbox defaults, refuses unsupported
   enforcement, and uses managed tool-chat leases. Result: 7 passed.

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/servers/websocket/chat/test_runtime_manager.py::TestWebChatRuntimeManager::test_create_session_rejects_droid_without_sensitive_path_enforcement tests/servers/websocket/chat/test_runtime_manager.py::TestWebChatRuntimeManager::test_manager_uses_daemon_owned_web_chat_sandbox_defaults tests/servers/websocket/chat/test_runtime_manager.py::TestWebChatRuntimeManager::test_manager_defaults_web_chat_sandbox_to_enabled tests/servers/test_chat_session.py::TestProjectRouting::test_start_materializes_sandbox_settings_file tests/ai/test_managed_tool_chat_lease.py -q --basetemp=/tmp/gobby-pytest-19562-gate6

7. Diagnostics, metadata, activation errors, and listings remain structurally
   redacted. Result: 5 passed.

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/agents/watchdog/test_claude_reader.py::test_api_error_is_redacted_diagnostic_only tests/agents/watchdog/test_claude_reader.py::test_tail_is_bounded_and_structurally_redacted tests/agents/test_spawn_executor.py::test_capability_token_never_in_argv_or_metadata tests/ai/test_endpoint_activation.py::test_core_probe_failure_keeps_endpoint_dark_and_redacts_key tests/servers/test_auth_service.py::test_agent_listing_redaction -q --basetemp=/tmp/gobby-pytest-19562-gate7

8. Service-capability responses are allowlisted and reveal neither KEK nor
   long-lived shared credentials. Result: 2 passed.

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/servers/routes/test_configuration_effective_routes.py::test_service_capabilities_are_claim_bound_and_allowlisted tests/servers/routes/test_configuration_effective_routes.py::test_service_capabilities_reject_query_selected_secrets -q --basetemp=/tmp/gobby-pytest-19562-gate8

## Final validation and audits

Affected unit and CLI tests:

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/agents/test_isolation.py tests/cli/hub_backup/test_stores.py tests/cli/hub_backup/test_cli.py tests/cli/test_postgres_cli.py -q --basetemp=/tmp/gobby-pytest-19562-unit-evidence

Migration and managed-credential integration tests run separately to avoid
cross-module fixture registration:

    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/storage/test_postgres_agent_authorization.py -q --basetemp=/tmp/gobby-pytest-19562-auth-evidence
    GOBBY_TEST_PROTECT=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test uv run pytest tests/storage/test_managed_credentials.py -q --basetemp=/tmp/gobby-pytest-19562-managed-evidence

Static validation:

    uv run ruff format --check src/gobby/agents/code_index.py src/gobby/cli/hub_backup/_stores.py src/gobby/cli/hub_backup/cli.py src/gobby/cli/postgres.py src/gobby/storage/managed_credentials.py tests/agents/test_isolation.py tests/cli/hub_backup/test_cli.py tests/cli/hub_backup/test_stores.py tests/cli/test_postgres_cli.py tests/storage/test_managed_credentials.py tests/storage/test_postgres_agent_authorization.py
    uv run ruff check src/gobby/agents/code_index.py src/gobby/cli/hub_backup/_stores.py src/gobby/cli/hub_backup/cli.py src/gobby/cli/postgres.py src/gobby/storage/managed_credentials.py tests/agents/test_isolation.py tests/cli/hub_backup/test_cli.py tests/cli/hub_backup/test_stores.py tests/cli/test_postgres_cli.py tests/storage/test_managed_credentials.py tests/storage/test_postgres_agent_authorization.py
    uv run mypy src/gobby/agents/code_index.py src/gobby/cli/hub_backup/_stores.py src/gobby/cli/hub_backup/cli.py src/gobby/cli/postgres.py src/gobby/storage/managed_credentials.py

Touched-test audits:

    uv run gobby test-quality audit tests/agents/test_isolation.py tests/cli/hub_backup/test_cli.py tests/cli/hub_backup/test_stores.py tests/cli/test_postgres_cli.py tests/storage/test_managed_credentials.py tests/storage/test_postgres_agent_authorization.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high
    uv run gobby test-types audit tests/agents/test_isolation.py tests/cli/hub_backup/test_cli.py tests/cli/hub_backup/test_stores.py tests/cli/test_postgres_cli.py tests/storage/test_managed_credentials.py tests/storage/test_postgres_agent_authorization.py --baseline .gobby/test-types-baseline.json --fail-on-new

Final results: affected unit/CLI 141 passed; authorization integration 8
passed; managed-credential integration 8 passed. Ruff reports 11 files
formatted and no lint errors. Mypy reports no issues in 5 production files.
Test-quality reports 153 tests and 0 issues. Test-types reports 0 new errors
against the baseline.
