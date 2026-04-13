import sqlite3
from unittest.mock import patch

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MIGRATIONS,
    _migrate_expansion_runs,
    get_current_version,
    run_migrations,
)
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit

# Calculate expected version after all migrations
EXPECTED_FINAL_VERSION = max(
    BASELINE_VERSION,
    max((m[0] for m in MIGRATIONS), default=BASELINE_VERSION),
)


def test_migrations_fresh_db(tmp_path) -> None:
    """Test running migrations on a fresh database.

    With the baseline schema architecture:
    - Fresh databases get BASELINE_SCHEMA applied directly (counts as 1 migration)
    - Plus any incremental migrations beyond the baseline
    - Final version is EXPECTED_FINAL_VERSION
    """
    db_path = tmp_path / "migration_test.db"
    db = LocalDatabase(db_path)

    # Initial state
    assert get_current_version(db) == 0

    # Run migrations
    applied = run_migrations(db)

    # Fresh databases apply baseline schema + incremental migrations
    expected_count = 1 + len([m for m in MIGRATIONS if m[0] > BASELINE_VERSION])
    assert applied == expected_count

    # Verify version reaches expected final version
    current_version = get_current_version(db)
    assert current_version == EXPECTED_FINAL_VERSION

    # Check tables exist (sample check)
    tables = [
        "schema_version",
        "projects",
        "sessions",
        "mcp_servers",
        "tools",
        "tasks",
        "task_dependencies",
        "session_tasks",
        "memories",
        "tool_embeddings",
        "task_validation_history",
        "workflow_definitions",
    ]
    for table in tables:
        # Check if table exists in sqlite_master
        row = db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        assert row is not None, f"Table {table} not created"


def test_migrations_idempotency(tmp_path) -> None:
    """Test that running migrations again does nothing."""
    db_path = tmp_path / "idempotency.db"
    db = LocalDatabase(db_path)

    run_migrations(db)
    initial_version = get_current_version(db)
    assert initial_version == EXPECTED_FINAL_VERSION

    # Run again
    applied = run_migrations(db)
    assert applied == 0
    assert get_current_version(db) == initial_version


def test_tasks_table_includes_claimed_by_session_id_on_fresh_db(tmp_path) -> None:
    """Fresh baseline schema should include canonical task ownership."""
    db_path = tmp_path / "tasks_claim_owner.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    task_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(tasks)")}
    assert "claimed_by_session_id" in task_columns

    task_indexes = {row["name"] for row in db.fetchall("PRAGMA index_list(tasks)")}
    assert "idx_tasks_claimed_session" in task_indexes


def test_agent_runs_table_includes_claimed_session_id_on_fresh_db(tmp_path) -> None:
    """Fresh baseline schema should include persisted agent run claim ownership."""
    db_path = tmp_path / "agent_runs_claim_owner.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    agent_run_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(agent_runs)")}
    assert "claimed_session_id" in agent_run_columns


def test_tasks_claimed_session_fk_is_set_null_on_fresh_db(tmp_path) -> None:
    """Fresh databases should end with ON DELETE SET NULL for canonical task ownership."""
    db_path = tmp_path / "tasks_claim_owner_fk.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    rows = db.fetchall("PRAGMA foreign_key_list(tasks)")
    claimed_fk = next(row for row in rows if row["from"] == "claimed_by_session_id")
    assert claimed_fk["on_delete"] == "SET NULL"


def test_migration_211_adds_claimed_session_id_to_agent_runs(tmp_path) -> None:
    """Migration 211 should add the claimed_session_id column to existing databases."""
    db_path = tmp_path / "agent_runs_claim_owner_partial.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    db.connection.executescript("""
        PRAGMA foreign_keys=OFF;
        DROP TABLE IF EXISTS agent_runs_legacy;
        CREATE TABLE agent_runs_legacy AS
        SELECT
            id,
            parent_session_id,
            child_session_id,
            workflow_name,
            agent_name,
            provider,
            model,
            status,
            prompt,
            result,
            error,
            tool_calls_count,
            turns_used,
            started_at,
            completed_at,
            created_at,
            updated_at,
            sdk_session_id,
            continuation_prompt,
            task_id,
            pid,
            tmux_session_name,
            worktree_id,
            clone_id,
            timeout_seconds
        FROM agent_runs;
        DROP TABLE agent_runs;
        CREATE TABLE agent_runs (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT NOT NULL REFERENCES sessions(id),
            child_session_id TEXT REFERENCES sessions(id),
            workflow_name TEXT,
            agent_name TEXT,
            provider TEXT NOT NULL,
            model TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            prompt TEXT NOT NULL,
            result TEXT,
            error TEXT,
            tool_calls_count INTEGER DEFAULT 0,
            turns_used INTEGER DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            sdk_session_id TEXT,
            continuation_prompt TEXT,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            pid INTEGER,
            tmux_session_name TEXT,
            worktree_id TEXT,
            clone_id TEXT,
            timeout_seconds REAL
        );
        INSERT INTO agent_runs
        SELECT * FROM agent_runs_legacy;
        DROP TABLE agent_runs_legacy;
        PRAGMA foreign_keys=ON;
    """)
    db.execute("DELETE FROM schema_version")
    db.execute("INSERT INTO schema_version (version) VALUES (210)")

    applied = run_migrations(db)

    assert applied == EXPECTED_FINAL_VERSION - 210
    agent_run_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(agent_runs)")}
    assert "claimed_session_id" in agent_run_columns


def test_migration_212_updates_tasks_claimed_session_fk(tmp_path) -> None:
    """Migration 212 should rebuild tasks with ON DELETE SET NULL ownership semantics."""
    db_path = tmp_path / "tasks_claim_owner_fk_partial.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    db.execute("DELETE FROM schema_version")
    db.execute("INSERT INTO schema_version (version) VALUES (211)")

    db.connection.executescript("""
        PRAGMA foreign_keys=OFF;
        DROP TABLE IF EXISTS tasks_legacy;
        CREATE TABLE tasks_legacy AS
        SELECT
            id, project_id, parent_task_id, created_in_session_id, claimed_by_session_id,
            lifecycle_stage, closed_in_session_id, closed_commit_sha, closed_at, title,
            description, status, priority, task_type, assignee, labels, closed_reason,
            compacted_at, validation_status, validation_feedback, validation_override_reason,
            category, validation_criteria, validation_fail_count, dispatch_failure_count,
            commits, escalated_at, escalation_reason, github_issue_number, github_pr_number,
            github_repo, linear_issue_id, linear_team_id, seq_num, path_cache,
            start_date, due_date, created_at, updated_at
        FROM tasks;
        DROP TABLE tasks;
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            parent_task_id TEXT REFERENCES tasks(id),
            created_in_session_id TEXT REFERENCES sessions(id),
            claimed_by_session_id TEXT REFERENCES sessions(id),
            lifecycle_stage TEXT CHECK(lifecycle_stage IN ('in_progress', 'needs_review', 'review_approved')),
            closed_in_session_id TEXT REFERENCES sessions(id),
            closed_commit_sha TEXT,
            closed_at TEXT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'open',
            priority INTEGER DEFAULT 2,
            task_type TEXT DEFAULT 'task',
            assignee TEXT,
            labels TEXT,
            closed_reason TEXT,
            compacted_at TEXT,
            validation_status TEXT CHECK(validation_status IN ('pending', 'valid', 'invalid')),
            validation_feedback TEXT,
            validation_override_reason TEXT,
            category TEXT,
            validation_criteria TEXT,
            validation_fail_count INTEGER DEFAULT 0,
            dispatch_failure_count INTEGER DEFAULT 0,
            commits TEXT,
            escalated_at TEXT,
            escalation_reason TEXT,
            github_issue_number INTEGER,
            github_pr_number INTEGER,
            github_repo TEXT,
            linear_issue_id TEXT,
            linear_team_id TEXT,
            seq_num INTEGER,
            path_cache TEXT,
            start_date TEXT,
            due_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO tasks
        SELECT * FROM tasks_legacy;
        DROP TABLE tasks_legacy;
        PRAGMA foreign_keys=ON;
    """)

    applied = run_migrations(db)

    assert applied == EXPECTED_FINAL_VERSION - 211
    rows = db.fetchall("PRAGMA foreign_key_list(tasks)")
    claimed_fk = next(row for row in rows if row["from"] == "claimed_by_session_id")
    assert claimed_fk["on_delete"] == "SET NULL"


def test_migrate_expansion_runs_drops_legacy_task_fields_without_backfill(tmp_path) -> None:
    """Legacy task-level expansion blobs are dropped instead of mapped to expansion_runs."""
    db_path = tmp_path / "expansion_runs_audit.db"
    db = LocalDatabase(db_path)
    run_migrations(db)

    db.execute(
        """
        INSERT INTO projects (id, name, repo_path, created_at, updated_at)
        VALUES (?, ?, ?, datetime('now'), datetime('now'))
        """,
        ("proj-1", "test-project", "/tmp/test-project"),
    )
    manager = LocalTaskManager(db)
    task = manager.create_task(project_id="proj-1", title="Legacy expansion task")

    db.execute("ALTER TABLE tasks ADD COLUMN expansion_context TEXT")
    db.execute("ALTER TABLE tasks ADD COLUMN expansion_status TEXT")
    db.execute(
        """
        UPDATE tasks
        SET expansion_context = ?, expansion_status = ?
        WHERE id = ?
        """,
        (
            '{"research_findings":["legacy"],"validation_criteria":"old"}',
            "completed",
            task.id,
        ),
    )

    _migrate_expansion_runs(db)

    task_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(tasks)")}
    assert "expansion_context" not in task_columns
    assert "expansion_status" not in task_columns

    count_row = db.fetchone("SELECT COUNT(*) AS count FROM expansion_runs")
    assert count_row is not None
    assert count_row["count"] == 0


def test_migration_208_recovers_when_column_exists_but_version_does_not(tmp_path) -> None:
    """Migration 208 should heal partial application without duplicate-column failure."""
    db_path = tmp_path / "tasks_claim_owner_partial.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    project_id = "00000000-0000-0000-0000-000000060887"
    session_id = "session-208"
    task_id = "task-208"
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (session_id, "ext-208", "machine-208", "codex", project_id),
    )
    db.execute(
        """
        INSERT INTO tasks (
            id, project_id, title, assignee, created_at, updated_at, claimed_by_session_id
        ) VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), NULL)
        """,
        (task_id, project_id, "partial migration task", session_id),
    )

    db.execute("DROP INDEX IF EXISTS idx_tasks_claimed_session")
    db.execute("DELETE FROM schema_version")
    db.execute("INSERT INTO schema_version (version) VALUES (207)")

    applied = run_migrations(db)

    # Seeded at 207; baseline now advances through 208, 209, 210.
    assert applied == EXPECTED_FINAL_VERSION - 207
    assert get_current_version(db) == EXPECTED_FINAL_VERSION
    task_row = db.fetchone(
        "SELECT claimed_by_session_id FROM tasks WHERE id = ?",
        (task_id,),
    )
    assert task_row is not None
    assert task_row["claimed_by_session_id"] == session_id

    task_indexes = {row["name"] for row in db.fetchall("PRAGMA index_list(tasks)")}
    assert "idx_tasks_claimed_session" in task_indexes


def test_migration_208_backfills_despite_legacy_orphaned_task_foreign_keys(tmp_path) -> None:
    """Migration 208 should recover even when legacy task rows violate older FKs."""
    db_path = tmp_path / "tasks_claim_owner_orphaned.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    project_id = "00000000-0000-0000-0000-000000060887"
    session_id = "session-208-valid"
    task_id = "task-208-orphaned"
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (session_id, "ext-208-valid", "machine-208", "codex", project_id),
    )

    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute(
            """
            INSERT INTO tasks (
                id,
                project_id,
                title,
                assignee,
                created_in_session_id,
                created_at,
                updated_at,
                claimed_by_session_id
            ) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), NULL)
            """,
            (task_id, project_id, "orphaned task", session_id, "missing-session"),
        )
    finally:
        db.execute("PRAGMA foreign_keys=ON")

    db.execute("DROP INDEX IF EXISTS idx_tasks_claimed_session")
    db.execute("DELETE FROM schema_version")
    db.execute("INSERT INTO schema_version (version) VALUES (207)")

    applied = run_migrations(db)

    # Seeded at 207; baseline now advances through 208, 209, 210.
    assert applied == EXPECTED_FINAL_VERSION - 207
    task_row = db.fetchone(
        "SELECT claimed_by_session_id FROM tasks WHERE id = ?",
        (task_id,),
    )
    assert task_row is not None
    assert task_row["claimed_by_session_id"] == session_id


def test_get_current_version_error(tmp_path) -> None:
    """Test get_current_version handles errors (e.g. missing table)."""
    db_path = tmp_path / "error.db"
    db = LocalDatabase(db_path)

    # schema_version doesn't exist yet
    assert get_current_version(db) == 0

    # Mock execute to raise exception even if table exists logic was reached
    with patch.object(db, "fetchone", side_effect=sqlite3.OperationalError("Boom")):
        assert get_current_version(db) == 0
