import sqlite3
from unittest.mock import patch

import pytest

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


def _index_columns(db: LocalDatabase, index_name: str) -> tuple[str, ...]:
    return tuple(
        row["name"]
        for row in db.fetchall(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index_name,)
        )
    )


def _trigger_names(db: LocalDatabase, table: str) -> set[str]:
    return {
        row["name"]
        for row in db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (table,),
        )
    }


def test_migrations_fresh_db_bootstraps_launch_baseline(tmp_path) -> None:
    """Fresh databases apply the current flattened baseline directly."""
    db_path = tmp_path / "migration_test.db"
    db = LocalDatabase(db_path)

    assert BASELINE_VERSION == 260
    assert latest_known_version() == 260
    assert MIGRATIONS == []
    assert get_current_version(db) == 0

    applied = run_migrations(db)

    assert applied == 1
    assert get_current_version(db) == 260
    versions = [row["version"] for row in db.fetchall("SELECT version FROM schema_version")]
    assert versions == [260]
    assert "idx_tasks_github_issue_link" in _index_names(db, "tasks")
    assert "linear_project_id" in _column_names(db, "projects")
    assert {"delivery_mode", "source_repo", "target_repo"}.issubset(
        _column_names(db, "task_delivery_campaigns")
    )
    assert {"delivery_mode", "delivery_target_repo"}.issubset(_column_names(db, "build_profiles"))
    assert "workspace_role" in _column_names(db, "worktrees")
    assert "integration_branch" in _column_names(db, "task_artifacts")
    assert _table_exists(db, "integration_workspace_mutex")
    assert "project_id" in _column_names(db, "chat_attachments")
    assert "idx_chat_attachments_project" in _index_names(db, "chat_attachments")
    assert "idx_chat_attachments_local_path" in _index_names(db, "chat_attachments")
    assert {
        "trg_chat_attachments_bound_at_write_once",
        "trg_chat_attachments_updated_at_touch",
    }.issubset(_trigger_names(db, "chat_attachments"))


def test_migrations_idempotency_at_launch_baseline(tmp_path) -> None:
    """Running migrations again at the current schema version does not add versions."""
    db_path = tmp_path / "idempotency.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    assert run_migrations(db) == 0
    assert get_current_version(db) == 260
    versions = [row["version"] for row in db.fetchall("SELECT version FROM schema_version")]
    assert versions == [260]


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


def test_sqlite_test_baseline_does_not_repair_mutated_fixture_database(tmp_path) -> None:
    """The fixture-only SQLite baseline does not run runtime repair migrations."""
    db_path = tmp_path / "system_session_repair.db"
    db = LocalDatabase(db_path)

    run_migrations(db)
    db.execute("DELETE FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,))
    assert db.fetchone("SELECT id FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,)) is None

    applied = run_migrations(db)

    assert applied == 0
    assert db.fetchone("SELECT id FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,)) is None


def test_sqlite_test_baseline_does_not_repair_legacy_runtime_indexes(tmp_path) -> None:
    """The fixture-only SQLite baseline leaves mutated runtime indexes untouched."""
    db_path = tmp_path / "sessions_unique_index_repair.db"
    db = LocalDatabase(db_path)

    run_migrations(db)
    db.execute("DROP INDEX idx_sessions_unique")
    db.execute(
        """
        CREATE UNIQUE INDEX idx_sessions_unique
        ON sessions(external_id, machine_id, source, project_id)
        """
    )
    assert _index_columns(db, "idx_sessions_unique") == (
        "external_id",
        "machine_id",
        "source",
        "project_id",
    )

    applied = run_migrations(db)

    assert applied == 0
    assert _index_columns(db, "idx_sessions_unique") == (
        "external_id",
        "machine_id",
        "source",
        "project_id",
    )


@pytest.mark.parametrize("legacy_version", [1, 218, 219, 238, 239, 257, 258, 259])
def test_pre_launch_sqlite_versions_are_unsupported(tmp_path, legacy_version) -> None:
    """Historical SQLite upgrades below the v260 baseline are unsupported."""
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
    assert "SQLite test baseline can only initialize an empty test database." in message
    assert f"Database version {legacy_version}" not in message
    assert "current SQLite baseline 260" not in message
    assert "gobby-hub.db" not in message


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
    """The v260 baseline includes representative storage domains."""
    db_path = tmp_path / "baseline_tables.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    expected_tables = {
        "schema_version",
        "projects",
        "sessions",
        "agent_runs",
        "build_runs",
        "build_history_events",
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
        "chat_attachments",
        "comms_messages",
        "bin_update_state",
    }
    missing = {table for table in expected_tables if not _table_exists(db, table)}
    assert missing == set()


def test_flattened_baseline_launch_columns(tmp_path) -> None:
    """The v260 baseline exposes the canonical post-flattening columns."""
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
    assert "project_id" in _column_names(db, "chat_attachments")
    assert {"token_hash", "expires_at", "remember_me"}.issubset(
        _column_names(db, "auth_sessions")
    )
    assert "token" not in _column_names(db, "auth_sessions")

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
    """The v260 baseline includes indexes and FK semantics formerly added by migrations."""
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
    assert "idx_chat_attachments_project" in _index_names(db, "chat_attachments")
    assert "idx_chat_attachments_local_path" in _index_names(db, "chat_attachments")
    assert "idx_ism_completion_lookup" in _index_names(db, "inter_session_messages")
    assert {
        "trg_chat_attachments_bound_at_write_once",
        "trg_chat_attachments_updated_at_touch",
    }.issubset(_trigger_names(db, "chat_attachments"))
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
    """The v260 baseline contains the repaired stage registry and zero-based defaults."""
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


def test_sqlite_test_baseline_excludes_removed_runtime_fts_objects(tmp_path) -> None:
    """The fixture-only SQLite baseline excludes removed runtime FTS objects."""
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
        assert not _table_exists(db, table), f"{table} should not exist"

    trigger_names = {
        row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    expected_triggers = {
        "code_symbols_ai",
        "code_content_ai",
        "tasks_fts_ai",
        "memories_fts_ai",
    }
    assert expected_triggers.isdisjoint(trigger_names)
    assert "memories_fts_au" not in trigger_names


def test_get_current_version_error(tmp_path) -> None:
    """get_current_version treats missing or unreadable schema metadata as version 0."""
    db_path = tmp_path / "error.db"
    db = LocalDatabase(db_path)

    assert get_current_version(db) == 0

    with patch.object(db, "fetchone", side_effect=sqlite3.OperationalError("Boom")):
        assert get_current_version(db) == 0
