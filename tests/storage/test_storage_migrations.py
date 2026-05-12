import json
import sqlite3
from unittest.mock import patch

import pytest

from gobby.config.app import load_config
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MIGRATIONS,
    MigrationUnsupportedError,
    _run_migration_list,
    get_current_version,
    latest_known_version,
    migrations_needed,
    run_migrations,
)
from gobby.storage.sessions import SYSTEM_SESSION_ID

pytestmark = pytest.mark.unit


def _table_exists(db: LocalDatabase, table: str) -> bool:
    row = db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return row is not None


def _column_names(db: LocalDatabase, table: str) -> set[str]:
    return {row["name"] for row in db.fetchall(f"PRAGMA table_info({table})")}


def _index_names(db: LocalDatabase, table: str) -> set[str]:
    return {row["name"] for row in db.fetchall(f"PRAGMA index_list({table})")}


def test_migrations_fresh_db_bootstraps_launch_baseline(tmp_path) -> None:
    """Fresh databases apply the flattened baseline plus pending migrations."""
    db_path = tmp_path / "migration_test.db"
    db = LocalDatabase(db_path)

    assert BASELINE_VERSION == 239
    assert latest_known_version() == 255
    assert [version for version, _description, _action in MIGRATIONS] == [
        240,
        241,
        242,
        243,
        244,
        245,
        246,
        247,
        248,
        249,
        250,
        251,
        252,
        253,
        254,
        255,
    ]
    assert get_current_version(db) == 0

    applied = run_migrations(db)

    assert applied == 17
    assert get_current_version(db) == 255
    versions = [row["version"] for row in db.fetchall("SELECT version FROM schema_version")]
    assert versions == [
        239,
        240,
        241,
        242,
        243,
        244,
        245,
        246,
        247,
        248,
        249,
        250,
        251,
        252,
        253,
        254,
        255,
    ]
    assert "idx_tasks_github_issue_link" in _index_names(db, "tasks")
    assert "linear_project_id" in _column_names(db, "projects")
    assert "workspace_role" in _column_names(db, "worktrees")
    assert "integration_branch" in _column_names(db, "task_artifacts")
    assert _table_exists(db, "integration_workspace_mutex")


def test_migrations_idempotency_at_launch_baseline(tmp_path) -> None:
    """Running migrations again at the current schema version does not add versions."""
    db_path = tmp_path / "idempotency.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    assert run_migrations(db) == 0
    assert get_current_version(db) == 255
    versions = [row["version"] for row in db.fetchall("SELECT version FROM schema_version")]
    assert versions == [
        239,
        240,
        241,
        242,
        243,
        244,
        245,
        246,
        247,
        248,
        249,
        250,
        251,
        252,
        253,
        254,
        255,
    ]


def test_sql_string_migrations_roll_back_atomically(tmp_path) -> None:
    """SQL-string migrations should roll back all statements on failure."""
    db_path = tmp_path / "atomic_migration.db"
    db = LocalDatabase(db_path)
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")

    with pytest.raises(sqlite3.OperationalError):
        _run_migration_list(
            db,
            current_version=0,
            migrations=[
                (
                    1,
                    "Create temp table and fail",
                    """
                    CREATE TABLE temp_atomic (id INTEGER PRIMARY KEY);
                    INSERT INTO temp_atomic (id) VALUES (1);
                    INSERT INTO missing_table (id) VALUES (1);
                    """,
                )
            ],
        )

    assert (
        db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='temp_atomic'")
        is None
    )
    assert db.fetchall("SELECT version FROM schema_version") == []


def test_delivery_migration_drops_legacy_artifact_columns(tmp_path) -> None:
    db_path = tmp_path / "delivery_migration.db"
    db = LocalDatabase(db_path)
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute(
        """
        CREATE TABLE task_artifacts (
            task_id TEXT PRIMARY KEY,
            plan_file_path TEXT,
            pr_url TEXT,
            merge_commit_sha TEXT,
            pr_review_report TEXT,
            structured_pr_verdict TEXT,
            merge_campaign_report TEXT
        )
        """
    )

    delivery_migrations = [migration for migration in MIGRATIONS if migration[0] == 240]
    _run_migration_list(db, current_version=239, migrations=delivery_migrations)

    assert _table_exists(db, "task_delivery_campaigns")
    assert _table_exists(db, "task_delivery_units")
    assert {
        "pr_url",
        "merge_commit_sha",
        "pr_review_report",
        "structured_pr_verdict",
        "merge_campaign_report",
    }.isdisjoint(_column_names(db, "task_artifacts"))


def test_review_anchor_migration_adds_default_planning_stage(tmp_path) -> None:
    db_path = tmp_path / "review_anchor_migration.db"
    db = LocalDatabase(db_path)
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE task_stages_registry (name TEXT PRIMARY KEY)")
    db.execute(
        """
        CREATE TABLE task_type_default_stages (
            task_type TEXT NOT NULL,
            stage_name TEXT NOT NULL REFERENCES task_stages_registry(name) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            PRIMARY KEY (task_type, stage_name)
        )
        """
    )
    db.execute("INSERT INTO task_stages_registry (name) VALUES ('planning')")

    review_anchor_migration = [migration for migration in MIGRATIONS if migration[0] == 242]
    _run_migration_list(db, current_version=241, migrations=review_anchor_migration)

    row = db.fetchone(
        """
        SELECT stage_name, position
          FROM task_type_default_stages
         WHERE task_type = 'review_anchor'
        """
    )
    assert dict(row) == {"stage_name": "planning", "position": 0}


def test_config_store_cleanup_migrates_logging_and_removes_stale_keys(tmp_path) -> None:
    db_path = tmp_path / "config_store_cleanup.db"
    db = LocalDatabase(db_path)
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO schema_version (version) VALUES (242)")
    db.execute(
        """
        CREATE TABLE config_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            is_secret INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    store = ConfigStore(db)
    active_direct_keys = {
        "rules.enforcement_enabled": False,
        "ui_settings.fontSize": 15,
        "ui_settings.model": "opus",
        "ui_settings.theme": "dark",
        "ui_settings.defaultChatMode": "agent",
        "ui_settings.postPlanChatMode": "ask",
        "ui_settings.selectedProjectId": "project-123",
    }
    legacy_logging_keys = {
        "logging.level": "debug",
        "logging.format": "json",
        "logging.client": "/tmp/legacy-client.log",
        "logging.client_error": "/tmp/legacy-error.log",
        "logging.hook_manager": "/tmp/legacy-hook.log",
        "logging.mcp_server": "/tmp/legacy-mcp-server.log",
        "logging.mcp_client": "/tmp/legacy-mcp-client.log",
        "logging.max_size_mb": 42,
        "logging.backup_count": 8,
    }
    stale_exact_keys = {
        "_meta.yaml_imported": True,
        "agent_auth.forward_claude_oauth_env": True,
        "conductor.daily_budget_usd": 1,
        "conductor.throttle_threshold": 0.8,
        "conductor.tracking_window_days": 7,
        "conductor.warning_threshold": 0.5,
        "embeddings.provider": "openai",
        "gobby_tasks.expansion.max_subtasks": 12,
        "gobby_tasks.validation.external_validator_mode": "required",
        "gobby_tasks.validation.use_external_validator": True,
        "llm_providers.api_keys.openai_api_key": "sk-raw-test-key",
        "logging.watchdog": True,
        "memory.mem0_url": "http://localhost:8000",
        "ui_settings.defaultchatmode": "legacy",
        "ui_settings.fontsize": 99,
        "ui_settings.selectedprojectid": "legacy-project",
        "workflow.protected_tools": ["Edit"],
        "workflow.require_task_before_edit": True,
    }
    stale_prefix_keys = {
        "gobby_tasks.enrichment.enabled": False,
        "review.provider": "claude",
        "task_description.enabled": True,
        "title_synthesis.model": "haiku",
        "hook_extensions.plugins.old.enabled": True,
        "llm_providers.litellm.api_base": "http://localhost:4000",
        "memory_extraction.provider": "legacy",
        "watchdog.enabled": True,
    }
    store.set_many(active_direct_keys)
    store.set_many(legacy_logging_keys)
    store.set_many(stale_exact_keys)
    store.set_many(stale_prefix_keys)
    store.set("telemetry.log_level", "warning")

    cleanup_migration = [migration for migration in MIGRATIONS if migration[0] == 243]
    applied = _run_migration_list(db, current_version=242, migrations=cleanup_migration)

    assert applied == 1
    assert get_current_version(db) == 243

    keys = set(store.list_keys())
    for key, value in active_direct_keys.items():
        assert store.get(key) == value
        assert key in keys
    for key in legacy_logging_keys | stale_exact_keys | stale_prefix_keys:
        assert key not in keys
    for prefix in (
        "gobby_tasks.enrichment.",
        "review.",
        "task_description.",
        "title_synthesis.",
        "hook_extensions.plugins.",
        "llm_providers.litellm.",
        "memory_extraction.",
        "watchdog.",
    ):
        assert not any(key.startswith(prefix) for key in keys)

    assert store.get("telemetry.log_level") == "warning"
    assert store.get("telemetry.log_format") == "json"
    assert store.get("telemetry.log_file") == "/tmp/legacy-client.log"
    assert store.get("telemetry.log_file_error") == "/tmp/legacy-error.log"
    assert store.get("telemetry.log_file_hook_manager") == "/tmp/legacy-hook.log"
    assert store.get("telemetry.log_file_mcp_server") == "/tmp/legacy-mcp-server.log"
    assert store.get("telemetry.log_file_mcp_client") == "/tmp/legacy-mcp-client.log"
    assert store.get("telemetry.max_size_mb") == 42
    assert store.get("telemetry.backup_count") == 8

    config = load_config(config_file=str(tmp_path / "bootstrap.yaml"), config_store=store)
    assert config.telemetry.log_level == "warning"
    assert config.telemetry.log_format == "json"
    assert config.telemetry.max_size_mb == 42


def test_migrations_recreate_missing_system_session(tmp_path) -> None:
    """Existing baseline databases self-heal the bootstrapped system session on startup."""
    db_path = tmp_path / "system_session_repair.db"
    db = LocalDatabase(db_path)

    run_migrations(db)
    db.execute("DELETE FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,))
    assert db.fetchone("SELECT id FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,)) is None

    applied = run_migrations(db)

    assert applied == 0
    repaired = db.fetchone(
        "SELECT id, external_id, source, title FROM sessions WHERE id = ?",
        (SYSTEM_SESSION_ID,),
    )
    assert repaired is not None
    assert repaired["external_id"] == "system"
    assert repaired["source"] == "system"
    assert repaired["title"] == "_system"


@pytest.mark.parametrize("legacy_version", [1, 218, 219, 238])
def test_pre_launch_sqlite_versions_are_unsupported(tmp_path, legacy_version) -> None:
    """Historical SQLite upgrades below the v239 baseline are unsupported."""
    db_path = tmp_path / f"legacy_{legacy_version}.db"
    db = LocalDatabase(db_path)
    db.execute(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    db.execute("INSERT INTO schema_version (version) VALUES (?)", (legacy_version,))

    with pytest.raises(MigrationUnsupportedError) as exc_info:
        run_migrations(db)

    message = str(exc_info.value)
    assert f"Database version {legacy_version}" in message
    assert "current SQLite baseline 239" in message
    assert "reset" in message
    assert "manually recover" in message
    assert "~/.gobby/gobby-hub.db" in message


def test_newer_sqlite_version_is_left_untouched(tmp_path) -> None:
    """A DB from a newer build should not be modified by this migration runner."""
    db_path = tmp_path / "future.db"
    db = LocalDatabase(db_path)
    db.execute(
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    future_version = latest_known_version() + 1
    db.execute("INSERT INTO schema_version (version) VALUES (?)", (future_version,))

    assert run_migrations(db) == 0
    assert get_current_version(db) == future_version
    assert not _table_exists(db, "projects")


def test_flattened_baseline_core_tables_exist(tmp_path) -> None:
    """The v239 baseline includes representative storage domains."""
    db_path = tmp_path / "baseline_tables.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    expected_tables = {
        "schema_version",
        "projects",
        "sessions",
        "agent_runs",
        "tasks",
        "task_dependencies",
        "task_stages_registry",
        "task_type_default_stages",
        "task_stage_states",
        "task_delivery_campaigns",
        "task_delivery_units",
        "project_github_triage_configs",
        "gh_triage_deliveries",
        "gh_issues_triaged",
        "session_tasks",
        "expansion_runs",
        "pending_interactions",
        "token_events",
        "config_store",
        "memories",
        "skills",
        "workflow_definitions",
        "code_symbols",
        "code_calls",
        "code_content_chunks",
        "checkpoints",
        "chat_messages",
        "comms_messages",
        "bin_update_state",
    }
    missing = {table for table in expected_tables if not _table_exists(db, table)}
    assert missing == set()


def test_flattened_baseline_launch_columns(tmp_path) -> None:
    """The v239 baseline exposes the canonical post-flattening columns."""
    db_path = tmp_path / "baseline_columns.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    assert {
        "claimed_session_id",
        "agent_name",
        "requested_reasoning_effort",
        "effective_reasoning_effort",
        "reasoning_required",
        "reasoning_status",
        "reasoning_message",
    }.issubset(_column_names(db, "agent_runs"))
    assert {"title_source", "sandbox_enabled", "sandbox_policy_hash"}.issubset(
        _column_names(db, "sessions")
    )
    task_columns = _column_names(db, "tasks")
    assert {
        "claimed_by_session_id",
        "dispatch_failure_count",
        "allow_automation",
        "unattended",
        "isolation",
        "is_escalated",
    }.issubset(task_columns)
    assert {"status", "lifecycle", "lifecycle_stage"}.isdisjoint(task_columns)
    assert {"graph_synced", "graph_sync_attempted_at"}.issubset(
        _column_names(db, "code_indexed_files")
    )
    assert {"callee_target_kind", "callee_symbol_id", "callee_name"}.issubset(
        _column_names(db, "code_calls")
    )
    assert {"model_family", "cache_creation_tokens", "cache_read_tokens"}.issubset(
        _column_names(db, "token_events")
    )
    assert "content_blocks_json" in _column_names(db, "chat_messages")

    assert "expansion_context" not in _column_names(db, "tasks")
    assert "expansion_status" not in _column_names(db, "tasks")
    artifact_columns = _column_names(db, "task_artifacts")
    assert {
        "last_reviewed_plan_hash",
        "plan_review_attempts",
        "qa_attempts",
        "holistic_attempts",
        "merge_attempts",
    }.issubset(artifact_columns)
    assert {
        "max_expansion_attempts",
        "max_qa_rounds",
        "max_merge_attempts",
        "max_holistic_rounds",
        "max_review_rounds",
        "pr_url",
        "merge_commit_sha",
        "pr_review_report",
        "structured_pr_verdict",
        "merge_campaign_report",
    }.isdisjoint(artifact_columns)
    assert "input_token_usd_per_1m" not in _column_names(db, "model_costs")
    assert "output_token_usd_per_1m" not in _column_names(db, "model_costs")


def test_flattened_baseline_indexes_and_constraints(tmp_path) -> None:
    """The v239 baseline includes indexes and FK semantics formerly added by migrations."""
    db_path = tmp_path / "baseline_indexes.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    assert {
        "idx_tasks_claimed_session",
        "idx_tasks_closed_session",
        "idx_tasks_dispatch_scan",
        "idx_tasks_state_bucket",
    }.issubset(_index_names(db, "tasks"))
    assert "state_bucket" in _column_names(db, "tasks")
    assert "idx_tasks_status" not in _index_names(db, "tasks")
    assert "idx_tasks_lifecycle_stage" not in _index_names(db, "tasks")
    assert {"idx_sessions_prune_status_updated_at", "idx_sessions_parent_session"}.issubset(
        _index_names(db, "sessions")
    )
    assert "idx_memories_source_session" in _index_names(db, "memories")
    assert "idx_token_events_dedup" in _index_names(db, "token_events")
    assert "idx_cc_target" in _index_names(db, "code_calls")
    assert "idx_plans_project_state" in _index_names(db, "plans")
    assert {
        "tool_name",
        "installed_version",
        "floor_version",
        "latest_version",
        "last_status",
        "last_error",
        "checked_at",
        "installed_at",
        "is_dev",
        "floor_drift",
    }.issubset(_column_names(db, "bin_update_state"))

    rows = db.fetchall("PRAGMA foreign_key_list(tasks)")
    claimed_fk = next(row for row in rows if row["from"] == "claimed_by_session_id")
    assert claimed_fk["on_delete"] == "SET NULL"


def test_task_state_bucket_tracks_stage_and_terminal_state(tmp_path) -> None:
    """The persisted task state bucket follows stage and close changes."""
    from gobby.storage.tasks import LocalTaskManager

    db_path = tmp_path / "task_state_bucket.db"
    db = LocalDatabase(db_path)
    run_migrations(db)

    db.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (?, ?, datetime('now'), datetime('now'))
        """,
        ("proj-state", "State Project"),
    )
    manager = LocalTaskManager(db)
    task = manager.create_task("proj-state", "Track state")
    manager.initialize_task_manifest(task.id)

    assert (
        db.fetchone("SELECT state_bucket FROM tasks WHERE id = ?", (task.id,))["state_bucket"]
        == "ready"
    )

    current = manager.stage_states.current_stage(task.id)
    assert current is not None
    manager.stage_states.start_stage(task.id, current.stage_name, by_session_id=None)
    assert (
        db.fetchone("SELECT state_bucket FROM tasks WHERE id = ?", (task.id,))["state_bucket"]
        == "in_progress"
    )

    manager.close_task(task.id, force=True)
    assert (
        db.fetchone("SELECT state_bucket FROM tasks WHERE id = ?", (task.id,))["state_bucket"]
        == "closed"
    )


def test_flattened_baseline_stage_registry_and_defaults(tmp_path) -> None:
    """The v239 baseline contains the repaired stage registry and zero-based defaults."""
    db_path = tmp_path / "baseline_stages.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    stage_rows = db.fetchall(
        """
        SELECT name, review_policy, reviewer_agent, reviewer_agent_selector_json, default_agent
          FROM task_stages_registry
         ORDER BY position_hint, name
        """
    )
    stage_names = [row["name"] for row in stage_rows]
    assert stage_names == [
        "ideation",
        "research",
        "architecture",
        "prd",
        "planning",
        "expansion",
        "development",
        "holistic_qa",
        "pr",
        "merge",
    ]
    assert {"adversarial_review", "expansion_qa", "code_review_qa"}.isdisjoint(stage_names)

    by_stage = {row["name"]: row for row in stage_rows}
    assert by_stage["planning"]["review_policy"] == "required"
    assert by_stage["planning"]["reviewer_agent"] == "plan-adversary"
    assert by_stage["development"]["review_policy"] == "required"
    assert by_stage["development"]["reviewer_agent"] is None
    assert "doc-reviewer" in by_stage["development"]["reviewer_agent_selector_json"]
    assert by_stage["pr"]["review_policy"] == "required"
    assert by_stage["pr"]["reviewer_agent"] is None
    assert by_stage["pr"]["default_agent"] == "merge-orchestrator"

    default_rows = db.fetchall(
        """
        SELECT task_type, stage_name, position
          FROM task_type_default_stages
         ORDER BY task_type, position, stage_name
        """
    )
    by_type: dict[str, list[tuple[str, int]]] = {}
    for row in default_rows:
        by_type.setdefault(row["task_type"], []).append((row["stage_name"], row["position"]))

    assert by_type["simple_fix"] == [("development", 0), ("pr", 1), ("merge", 2)]
    assert by_type["research_spike"] == [("ideation", 0), ("research", 1), ("prd", 2)]
    assert by_type["prd_doc"] == [("ideation", 0), ("prd", 1)]
    assert by_type["architecture_doc"] == [("research", 0), ("architecture", 1)]
    assert by_type["review_anchor"] == [("planning", 0)]
    for rows in by_type.values():
        assert [position for _stage_name, position in rows] == list(range(len(rows)))


def test_migration_254_adds_reviewer_selector_to_existing_registry(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "stage-reviewer-selector.db")
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO schema_version (version) VALUES (253)")
    db.execute(
        """
        CREATE TABLE task_stages_registry (
            name TEXT PRIMARY KEY,
            reviewer_agent TEXT,
            updated_at TEXT
        )
        """
    )
    db.execute(
        """
        INSERT INTO task_stages_registry (name, reviewer_agent, updated_at)
        VALUES ('development', 'custom-reviewer', datetime('now'))
        """
    )
    migration_254 = [item for item in MIGRATIONS if item[0] == 254]

    assert _run_migration_list(db, 253, migration_254) == 1

    row = db.fetchone(
        """
        SELECT reviewer_agent, reviewer_agent_selector_json
          FROM task_stages_registry
         WHERE name = 'development'
        """
    )
    assert row["reviewer_agent"] is None
    selector = json.loads(row["reviewer_agent_selector_json"])
    assert selector["default"] == "custom-reviewer"
    assert selector["rules"] == [{"category": "docs", "reviewer_agent": "doc-reviewer"}]
    assert get_current_version(db) == 254


def test_migration_255_adds_project_lifecycle_events_to_existing_database(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "project-lifecycle-events.db")
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO schema_version (version) VALUES (254)")
    db.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE task_stages_registry (name TEXT PRIMARY KEY)")
    migration_255 = [item for item in MIGRATIONS if item[0] == 255]

    assert _run_migration_list(db, 254, migration_255) == 1

    assert _table_exists(db, "project_lifecycle_events")
    assert "idx_project_lifecycle_events_project" in _index_names(
        db,
        "project_lifecycle_events",
    )
    assert _table_exists(db, "build_profiles")
    assert "deleted_at" in _column_names(db, "task_stages_registry")
    assert get_current_version(db) == 255


def test_remove_test_arch_stage_migration_cleans_rows_and_renumbers(tmp_path) -> None:
    db_path = tmp_path / "remove_test_arch.db"
    db = LocalDatabase(db_path)
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    db.execute(
        """
        CREATE TABLE task_stage_states (
            task_id TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (task_id, stage_name)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE task_type_default_stages (
            task_type TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (task_type, stage_name)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE task_stages_registry (
            name TEXT PRIMARY KEY,
            position_hint INTEGER
        )
        """
    )
    db.execute(
        """
        CREATE TABLE task_artifacts (
            task_id TEXT PRIMARY KEY,
            test_arch_attempts INTEGER,
            qa_attempts INTEGER
        )
        """
    )
    db.executemany(
        """
        INSERT INTO task_stage_states (task_id, stage_name, position)
        VALUES (?, ?, ?)
        """,
        [
            ("task-1", "planning", 4),
            ("task-1", "test_arch", 5),
            ("task-1", "expansion", 6),
            ("task-1", "development", 7),
            ("task-2", "test_arch", 1),
            ("task-2", "pr", 3),
        ],
    )
    db.executemany(
        """
        INSERT INTO task_type_default_stages (task_type, stage_name, position)
        VALUES (?, ?, ?)
        """,
        [
            ("epic", "planning", 4),
            ("epic", "test_arch", 5),
            ("epic", "expansion", 6),
            ("epic", "development", 7),
            ("feature", "planning", 0),
            ("feature", "test_arch", 1),
            ("feature", "expansion", 2),
        ],
    )
    db.executemany(
        "INSERT INTO task_stages_registry (name, position_hint) VALUES (?, ?)",
        [
            ("planning", 4),
            ("test_arch", 5),
            ("expansion", 6),
            ("development", 7),
            ("pr", 8),
        ],
    )
    db.execute(
        """
        INSERT INTO task_artifacts (task_id, test_arch_attempts, qa_attempts)
        VALUES ('task-1', 2, 3)
        """
    )

    migration = [item for item in MIGRATIONS if item[0] == 253]
    applied = _run_migration_list(db, current_version=252, migrations=migration)

    assert applied == 1
    assert "test_arch_attempts" not in _column_names(db, "task_artifacts")
    assert db.fetchone("SELECT name FROM task_stages_registry WHERE name = 'test_arch'") is None
    stage_rows = db.fetchall(
        """
        SELECT task_id, stage_name, position
          FROM task_stage_states
         ORDER BY task_id, position
        """
    )
    assert [tuple(row) for row in stage_rows] == [
        ("task-1", "planning", 0),
        ("task-1", "expansion", 1),
        ("task-1", "development", 2),
        ("task-2", "pr", 0),
    ]
    default_rows = db.fetchall(
        """
        SELECT task_type, stage_name, position
          FROM task_type_default_stages
         ORDER BY task_type, position
        """
    )
    assert [tuple(row) for row in default_rows] == [
        ("epic", "planning", 0),
        ("epic", "expansion", 1),
        ("epic", "development", 2),
        ("feature", "planning", 0),
        ("feature", "expansion", 1),
    ]


def test_migrations_needed_checks_latest_schema_version(tmp_path) -> None:
    """The lightweight schema-version check avoids running no-op CLI migrations."""
    db_path = tmp_path / "migrations-needed.db"
    db = LocalDatabase(db_path)

    assert migrations_needed(db) is True
    run_migrations(db)
    assert migrations_needed(db) is False

    pending_index = db.fetchone(
        """
        SELECT sql
          FROM sqlite_master
         WHERE type = 'index'
           AND name = 'idx_pending_interactions_active'
        """
    )
    assert pending_index is not None
    assert "WHERE status = 'pending'" in pending_index["sql"]


def test_flattened_baseline_fts_tables_and_triggers(tmp_path) -> None:
    """Fresh bootstrap creates baseline FTS virtual tables and sync triggers."""
    db_path = tmp_path / "baseline_fts.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    for table in (
        "code_symbols_fts",
        "code_content_fts",
        "tasks_fts",
        "skills_fts",
        "memories_fts",
    ):
        assert _table_exists(db, table), f"{table} missing"

    trigger_names = {
        row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    expected_triggers = {
        "code_symbols_ai",
        "code_content_ai",
        "tasks_fts_ai",
        "memories_fts_ai",
    }
    assert expected_triggers.issubset(trigger_names)
    assert "memories_fts_au" in trigger_names


def test_get_current_version_error(tmp_path) -> None:
    """get_current_version treats missing or unreadable schema metadata as version 0."""
    db_path = tmp_path / "error.db"
    db = LocalDatabase(db_path)

    assert get_current_version(db) == 0

    with patch.object(db, "fetchone", side_effect=sqlite3.OperationalError("Boom")):
        assert get_current_version(db) == 0
