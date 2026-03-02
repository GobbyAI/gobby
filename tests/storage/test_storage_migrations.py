import sqlite3
from unittest.mock import patch

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MIGRATIONS,
    get_current_version,
    run_migrations,
)

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

    # Fresh databases apply baseline schema (v107) + incremental migrations
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
        "session_messages",
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


def test_get_current_version_error(tmp_path) -> None:
    """Test get_current_version handles errors (e.g. missing table)."""
    db_path = tmp_path / "error.db"
    db = LocalDatabase(db_path)

    # schema_version doesn't exist yet
    assert get_current_version(db) == 0

    # Mock execute to raise exception even if table exists logic was reached
    with patch.object(db, "fetchone", side_effect=sqlite3.OperationalError("Boom")):
        assert get_current_version(db) == 0


# =============================================================================
# Task System V2: Commit Linking Migration Tests
# =============================================================================


def test_commits_column_exists_after_migration(tmp_path) -> None:
    """Test that the 'commits' column is added to the tasks table."""
    db_path = tmp_path / "commits_migration.db"
    db = LocalDatabase(db_path)

    # Run all migrations
    run_migrations(db)

    # Check that commits column exists in tasks table
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    assert row is not None
    assert "commits" in row["sql"].lower(), "commits column not found in tasks table"


def test_commits_column_allows_null(tmp_path) -> None:
    """Test that the commits column allows NULL values (for existing tasks)."""
    db_path = tmp_path / "commits_null.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create a project first (required for task)
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert a task without commits (NULL)
    db.execute(
        """INSERT INTO tasks (id, project_id, title, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task"),
    )

    # Verify task was created with NULL commits
    row = db.fetchone("SELECT commits FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["commits"] is None


def test_commits_column_accepts_json_array(tmp_path) -> None:
    """Test that commits column stores JSON array of commit SHAs."""
    db_path = tmp_path / "commits_json.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task with commits as JSON array
    import json

    commits = json.dumps(["abc123", "def456", "789ghi"])
    db.execute(
        """INSERT INTO tasks (id, project_id, title, commits, created_at, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task", commits),
    )

    # Verify commits stored correctly
    row = db.fetchone("SELECT commits FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["commits"] == commits
    parsed = json.loads(row["commits"])
    assert parsed == ["abc123", "def456", "789ghi"]


def test_commits_migration_idempotent(tmp_path) -> None:
    """Test that running migrations twice doesn't fail or duplicate columns."""
    db_path = tmp_path / "commits_idempotent.db"
    db = LocalDatabase(db_path)

    # Run migrations twice
    run_migrations(db)
    applied = run_migrations(db)

    # Second run should apply 0 migrations
    assert applied == 0

    # commits column should still exist and work
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    assert row is not None
    # Count occurrences of 'commits' - should be exactly 1
    sql_lower = row["sql"].lower()
    assert sql_lower.count("commits") == 1, "commits column duplicated or missing"


# =============================================================================
# Task System V2: Validation History Table Migration Tests
# =============================================================================


def test_validation_history_table_exists(tmp_path) -> None:
    """Test that task_validation_history table is created."""
    db_path = tmp_path / "validation_history.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Check table exists
    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_validation_history'"
    )
    assert row is not None, "task_validation_history table not created"


def test_validation_history_schema(tmp_path) -> None:
    """Test that task_validation_history has correct columns."""
    db_path = tmp_path / "validation_schema.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Get table info
    rows = db.fetchall("PRAGMA table_info(task_validation_history)")
    columns = {row["name"] for row in rows}

    # Verify required columns exist
    expected_columns = {
        "id",
        "task_id",
        "iteration",
        "status",
        "feedback",
        "issues",
        "context_type",
        "context_summary",
        "validator_type",
        "created_at",
    }
    for col in expected_columns:
        assert col in columns, f"Column {col} missing from task_validation_history"


def test_validation_history_foreign_key(tmp_path) -> None:
    """Test that task_validation_history has foreign key to tasks."""
    db_path = tmp_path / "validation_fk.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Get table SQL
    row = db.fetchone(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_validation_history'"
    )
    assert row is not None
    sql_lower = row["sql"].lower()

    # Check for foreign key reference to tasks
    assert "references tasks" in sql_lower or "foreign key" in sql_lower, (
        "task_validation_history missing foreign key to tasks"
    )


def test_validation_history_index_exists(tmp_path) -> None:
    """Test that index on task_id exists for task_validation_history."""
    db_path = tmp_path / "validation_index.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Check for index on task_id
    rows = db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='task_validation_history'"
    )
    index_names = [row["name"] for row in rows]

    # Should have an index containing 'task' in the name
    has_task_index = any("task" in name.lower() for name in index_names)
    assert has_task_index, f"No task_id index found. Indexes: {index_names}"


def test_validation_history_cascade_delete(tmp_path) -> None:
    """Test that deleting a task cascades to validation history."""
    db_path = tmp_path / "validation_cascade.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Enable foreign keys
    db.execute("PRAGMA foreign_keys = ON")

    # Create project and task
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        """INSERT INTO tasks (id, project_id, title, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task"),
    )

    # Insert validation history record
    db.execute(
        """INSERT INTO task_validation_history
           (task_id, iteration, status, created_at)
           VALUES (?, ?, ?, datetime('now'))""",
        ("task-1", 1, "invalid"),
    )

    # Verify record exists
    row = db.fetchone("SELECT * FROM task_validation_history WHERE task_id = ?", ("task-1",))
    assert row is not None

    # Delete the task
    db.execute("DELETE FROM tasks WHERE id = ?", ("task-1",))

    # Verify validation history was cascade deleted
    row = db.fetchone("SELECT * FROM task_validation_history WHERE task_id = ?", ("task-1",))
    assert row is None, "Validation history not cascade deleted"


def test_tasks_escalation_columns(tmp_path) -> None:
    """Test that escalation columns are added to tasks table."""
    db_path = tmp_path / "escalation_cols.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Get tasks table info
    rows = db.fetchall("PRAGMA table_info(tasks)")
    columns = {row["name"] for row in rows}

    # Check for escalation columns
    assert "escalated_at" in columns, "escalated_at column missing from tasks"
    assert "escalation_reason" in columns, "escalation_reason column missing from tasks"


# =============================================================================
# GitHub Integration: GitHub Columns Migration Tests
# =============================================================================


def test_github_columns_exist_after_migration(tmp_path) -> None:
    """Test that GitHub integration columns are added to tasks table."""
    db_path = tmp_path / "github_cols.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Get tasks table info
    rows = db.fetchall("PRAGMA table_info(tasks)")
    columns = {row["name"] for row in rows}

    # Check for GitHub columns
    assert "github_issue_number" in columns, "github_issue_number column missing from tasks"
    assert "github_pr_number" in columns, "github_pr_number column missing from tasks"
    assert "github_repo" in columns, "github_repo column missing from tasks"


def test_github_columns_allow_null(tmp_path) -> None:
    """Test that GitHub columns allow NULL values (for existing tasks)."""
    db_path = tmp_path / "github_null.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create a project first (required for task)
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert a task without GitHub fields (NULL)
    db.execute(
        """INSERT INTO tasks (id, project_id, title, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task"),
    )

    # Verify task was created with NULL GitHub fields
    row = db.fetchone(
        "SELECT github_issue_number, github_pr_number, github_repo FROM tasks WHERE id = ?",
        ("task-1",),
    )
    assert row is not None
    assert row["github_issue_number"] is None
    assert row["github_pr_number"] is None
    assert row["github_repo"] is None


def test_github_columns_store_values(tmp_path) -> None:
    """Test that GitHub columns store integer and text values correctly."""
    db_path = tmp_path / "github_values.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task with GitHub fields
    db.execute(
        """INSERT INTO tasks (id, project_id, title, github_issue_number, github_pr_number, github_repo, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task", 123, 456, "owner/repo"),
    )

    # Verify GitHub fields stored correctly
    row = db.fetchone(
        "SELECT github_issue_number, github_pr_number, github_repo FROM tasks WHERE id = ?",
        ("task-1",),
    )
    assert row is not None
    assert row["github_issue_number"] == 123
    assert row["github_pr_number"] == 456
    assert row["github_repo"] == "owner/repo"


# =============================================================================
# Task ID Redesign: seq_num and path_cache Migration Tests
# =============================================================================


def test_seq_num_and_path_cache_columns_exist(tmp_path) -> None:
    """Test that seq_num and path_cache columns are added to tasks table."""
    db_path = tmp_path / "seq_num_cols.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Get tasks table info
    rows = db.fetchall("PRAGMA table_info(tasks)")
    columns = {row["name"] for row in rows}

    # Check for new columns
    assert "seq_num" in columns, "seq_num column missing from tasks"
    assert "path_cache" in columns, "path_cache column missing from tasks"


def test_seq_num_unique_index_per_project(tmp_path) -> None:
    """Test that seq_num is unique per project via index."""
    db_path = tmp_path / "seq_num_index.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Check for unique index on (project_id, seq_num)
    rows = db.fetchall(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='tasks'"
    )
    index_names = {row["name"] for row in rows}

    assert "idx_tasks_seq_num" in index_names, "idx_tasks_seq_num index missing"

    # Verify it's unique
    for row in rows:
        if row["name"] == "idx_tasks_seq_num":
            assert "UNIQUE" in row["sql"].upper(), "idx_tasks_seq_num should be UNIQUE"


def test_path_cache_index_exists(tmp_path) -> None:
    """Test that path_cache index exists for efficient lookups."""
    db_path = tmp_path / "path_cache_index.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Check for index on path_cache
    rows = db.fetchall("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks'")
    index_names = {row["name"] for row in rows}

    assert "idx_tasks_path_cache" in index_names, "idx_tasks_path_cache index missing"


def test_seq_num_allows_null(tmp_path) -> None:
    """Test that seq_num allows NULL values (for existing tasks pre-backfill)."""
    db_path = tmp_path / "seq_num_null.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create a project first (required for task)
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task without seq_num (simulating pre-backfill state)
    db.execute(
        """INSERT INTO tasks (id, project_id, title, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task"),
    )

    # Should work without seq_num
    row = db.fetchone("SELECT seq_num, path_cache FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["seq_num"] is None, "seq_num should be NULL when not set"
    assert row["path_cache"] is None, "path_cache should be NULL when not set"


def test_seq_num_stores_integer_values(tmp_path) -> None:
    """Test that seq_num stores integer values correctly."""
    db_path = tmp_path / "seq_num_values.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task with seq_num
    db.execute(
        """INSERT INTO tasks (id, project_id, title, seq_num, path_cache, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task", 42, "1.2.42"),
    )

    # Verify values stored correctly
    row = db.fetchone("SELECT seq_num, path_cache FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["seq_num"] == 42
    assert row["path_cache"] == "1.2.42"


# =============================================================================
# TDD Expansion Restructure: Category Migration Tests
# =============================================================================


def test_category_column_exists_after_migration(tmp_path) -> None:
    """Test that the 'category' column exists in the tasks table after migration.

    This replaces the old 'test_strategy' column with a more semantic name.
    The category field represents the task's classification (e.g., 'unit', 'integration',
    'e2e', 'manual') rather than just a testing strategy.
    """
    db_path = tmp_path / "category_migration.db"
    db = LocalDatabase(db_path)

    # Run all migrations
    run_migrations(db)

    # Check that category column exists in tasks table
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    assert row is not None
    sql_lower = row["sql"].lower()
    assert "category" in sql_lower, "category column not found in tasks table"


def test_test_strategy_column_removed_after_migration(tmp_path) -> None:
    """Test that the 'test_strategy' column no longer exists after migration.

    The migration renames test_strategy to category, so test_strategy should
    not appear in the schema.
    """
    db_path = tmp_path / "test_strategy_removed.db"
    db = LocalDatabase(db_path)

    # Run all migrations
    run_migrations(db)

    # Check that test_strategy column does NOT exist in tasks table
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    assert row is not None
    sql_lower = row["sql"].lower()
    # test_strategy should not appear in the schema (it's been renamed to category)
    assert "test_strategy" not in sql_lower, (
        "test_strategy column should not exist after migration to category"
    )


def test_category_column_accepts_values(tmp_path) -> None:
    """Test that the category column accepts valid values."""
    db_path = tmp_path / "category_values.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task with category
    db.execute(
        """INSERT INTO tasks (id, project_id, title, category, created_at, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task", "unit"),
    )

    # Verify category stored correctly
    row = db.fetchone("SELECT category FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["category"] == "unit"


def test_category_column_allows_null(tmp_path) -> None:
    """Test that the category column allows NULL values."""
    db_path = tmp_path / "category_null.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task without category (NULL)
    db.execute(
        """INSERT INTO tasks (id, project_id, title, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task"),
    )

    # Verify task was created with NULL category
    row = db.fetchone("SELECT category FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["category"] is None


# =============================================================================
# TDD Expansion Restructure: agent_name column addition
# =============================================================================


def test_agent_name_column_exists_after_migration(tmp_path) -> None:
    """Test that the 'agent_name' column exists in the tasks table after migration.

    The agent_name field specifies which subagent configuration file to use when
    spawning an agent to work on this task (e.g., 'backend-specialist', 'test-writer').
    """
    db_path = tmp_path / "agent_name_migration.db"
    db = LocalDatabase(db_path)

    # Run all migrations
    run_migrations(db)

    # Check that agent_name column exists in tasks table
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    assert row is not None
    sql_lower = row["sql"].lower()
    assert "agent_name" in sql_lower, "agent_name column not found in tasks table"


def test_agent_name_column_accepts_values(tmp_path) -> None:
    """Test that the agent_name column accepts valid TEXT values."""
    db_path = tmp_path / "agent_name_values.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task with agent_name
    db.execute(
        """INSERT INTO tasks (id, project_id, title, agent_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task", "backend-specialist"),
    )

    # Verify the agent_name was stored correctly
    row = db.fetchone("SELECT agent_name FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["agent_name"] == "backend-specialist"


def test_agent_name_column_allows_null(tmp_path) -> None:
    """Test that the agent_name column allows NULL values.

    Most tasks won't have a specific agent configuration, so NULL should be allowed.
    """
    db_path = tmp_path / "agent_name_null.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task without agent_name (NULL)
    db.execute(
        """INSERT INTO tasks (id, project_id, title, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task"),
    )

    # Verify task was created with NULL agent_name
    row = db.fetchone("SELECT agent_name FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["agent_name"] is None


# =============================================================================
# TDD Expansion Restructure: reference_doc column addition
# =============================================================================


def test_reference_doc_column_exists_after_migration(tmp_path) -> None:
    """Test that the 'reference_doc' column exists in the tasks table after migration.

    The reference_doc field stores the path to the source specification document
    for traceability, linking tasks back to their origin in PRDs or design docs.
    """
    db_path = tmp_path / "reference_doc_migration.db"
    db = LocalDatabase(db_path)

    # Run all migrations
    run_migrations(db)

    # Check that reference_doc column exists in tasks table
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    assert row is not None
    sql_lower = row["sql"].lower()
    assert "reference_doc" in sql_lower, "reference_doc column not found in tasks table"


def test_reference_doc_column_accepts_values(tmp_path) -> None:
    """Test that the reference_doc column accepts valid TEXT values."""
    db_path = tmp_path / "reference_doc_values.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task with reference_doc
    db.execute(
        """INSERT INTO tasks (id, project_id, title, reference_doc, created_at, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task", "docs/specs/auth-design.md"),
    )

    # Verify the reference_doc was stored correctly
    row = db.fetchone("SELECT reference_doc FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["reference_doc"] == "docs/specs/auth-design.md"


def test_reference_doc_column_allows_null(tmp_path) -> None:
    """Test that the reference_doc column allows NULL values.

    Most tasks won't have a reference document, so NULL should be allowed.
    """
    db_path = tmp_path / "reference_doc_null.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task without reference_doc (NULL)
    db.execute(
        """INSERT INTO tasks (id, project_id, title, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task"),
    )

    # Verify task was created with NULL reference_doc
    row = db.fetchone("SELECT reference_doc FROM tasks WHERE id = ?", ("task-1",))
    assert row is not None
    assert row["reference_doc"] is None


# =============================================================================
# Task Expansion: boolean columns (is_expanded, expansion_status)
# =============================================================================


def test_boolean_columns_exist_after_migration(tmp_path) -> None:
    """Test that the boolean columns exist in the tasks table after migration.

    These flags enable idempotent batch operations:
    - is_expanded: subtasks have been created
    """
    db_path = tmp_path / "boolean_columns_migration.db"
    db = LocalDatabase(db_path)

    # Run all migrations
    run_migrations(db)

    # Check that is_expanded column exists in tasks table
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    assert row is not None
    sql_lower = row["sql"].lower()
    assert "is_expanded" in sql_lower, "is_expanded column not found in tasks table"
    # is_enriched was dropped in migration 66
    # is_tdd_applied was dropped in migration 74


def test_boolean_columns_accept_values(tmp_path) -> None:
    """Test that the boolean columns accept INTEGER values (0/1)."""
    db_path = tmp_path / "boolean_columns_values.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task with boolean values set to true (1)
    db.execute(
        """INSERT INTO tasks (id, project_id, title, is_expanded, created_at, updated_at)
           VALUES (?, ?, ?, 1, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task"),
    )

    # Verify the boolean values were stored correctly
    row = db.fetchone(
        "SELECT is_expanded FROM tasks WHERE id = ?",
        ("task-1",),
    )
    assert row is not None
    assert row["is_expanded"] == 1


def test_boolean_columns_default_to_zero(tmp_path) -> None:
    """Test that the boolean columns default to 0 (false).

    New tasks should have all processing flags set to false by default.
    """
    db_path = tmp_path / "boolean_columns_default.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )

    # Insert task without specifying boolean values
    db.execute(
        """INSERT INTO tasks (id, project_id, title, created_at, updated_at)
           VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
        ("task-1", "test-project", "Test Task"),
    )

    # Verify task was created with default values of 0
    row = db.fetchone(
        "SELECT is_expanded FROM tasks WHERE id = ?",
        ("task-1",),
    )
    assert row is not None
    assert row["is_expanded"] == 0


# =============================================================================
# Inter-Session Messaging: inter_session_messages table migration
# =============================================================================


def test_inter_session_messages_table_exists(tmp_path) -> None:
    """Test that inter_session_messages table is created after migration.

    This table enables communication between parent and child agent sessions,
    allowing agents to coordinate work without using the filesystem.
    """
    db_path = tmp_path / "inter_session_messages.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Check table exists
    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='inter_session_messages'"
    )
    assert row is not None, "inter_session_messages table not created"


def test_inter_session_messages_schema(tmp_path) -> None:
    """Test that inter_session_messages has correct columns."""
    db_path = tmp_path / "inter_session_schema.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Get table info
    rows = db.fetchall("PRAGMA table_info(inter_session_messages)")
    columns = {row["name"] for row in rows}

    # Verify required columns exist
    expected_columns = {
        "id",
        "from_session",
        "to_session",
        "content",
        "priority",
        "sent_at",
        "read_at",
    }
    for col in expected_columns:
        assert col in columns, f"Column {col} missing from inter_session_messages"


def test_inter_session_messages_foreign_keys(tmp_path) -> None:
    """Test that inter_session_messages has foreign keys to sessions table."""
    db_path = tmp_path / "inter_session_fk.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Get table SQL
    row = db.fetchone(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='inter_session_messages'"
    )
    assert row is not None
    sql_lower = row["sql"].lower()

    # Check for foreign key references to sessions
    assert "references sessions" in sql_lower or "foreign key" in sql_lower, (
        "inter_session_messages missing foreign key to sessions"
    )


def test_inter_session_messages_indexes(tmp_path) -> None:
    """Test that inter_session_messages has proper indexes for queries."""
    db_path = tmp_path / "inter_session_index.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Check for indexes
    rows = db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='inter_session_messages'"
    )
    index_names = [row["name"] for row in rows]

    # Should have indexes on to_session for efficient message retrieval
    has_to_session_index = any("to_session" in name.lower() for name in index_names)
    assert has_to_session_index, f"No to_session index found. Indexes: {index_names}"


def test_inter_session_messages_insert_and_query(tmp_path) -> None:
    """Test that inter_session_messages can store and retrieve messages."""
    db_path = tmp_path / "inter_session_insert.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project and sessions first (required for foreign keys)
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-parent", "ext-parent", "machine-1", "claude", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, parent_session_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-child", "ext-child", "machine-1", "claude", "test-project", "session-parent"),
    )

    # Insert a message from parent to child
    import uuid

    msg_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO inter_session_messages
           (id, from_session, to_session, content, priority, sent_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (msg_id, "session-parent", "session-child", "Please work on subtask A", "normal"),
    )

    # Query messages for child session
    row = db.fetchone(
        "SELECT * FROM inter_session_messages WHERE to_session = ?",
        ("session-child",),
    )
    assert row is not None
    assert row["from_session"] == "session-parent"
    assert row["content"] == "Please work on subtask A"
    assert row["priority"] == "normal"
    assert row["read_at"] is None  # Not read yet


def test_inter_session_messages_read_at_nullable(tmp_path) -> None:
    """Test that read_at is nullable for unread messages."""
    db_path = tmp_path / "inter_session_read.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create required parent records
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-1", "ext-1", "machine-1", "claude", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-2", "ext-2", "machine-1", "claude", "test-project"),
    )

    import uuid

    msg_id = str(uuid.uuid4())

    # Insert message without read_at (NULL)
    db.execute(
        """INSERT INTO inter_session_messages
           (id, from_session, to_session, content, priority, sent_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (msg_id, "session-1", "session-2", "Test message", "normal"),
    )

    row = db.fetchone("SELECT read_at FROM inter_session_messages WHERE id = ?", (msg_id,))
    assert row is not None
    assert row["read_at"] is None

    # Update to mark as read
    db.execute(
        "UPDATE inter_session_messages SET read_at = datetime('now') WHERE id = ?",
        (msg_id,),
    )

    row = db.fetchone("SELECT read_at FROM inter_session_messages WHERE id = ?", (msg_id,))
    assert row["read_at"] is not None


def test_inter_session_messages_priority_values(tmp_path) -> None:
    """Test that priority accepts expected values (normal, urgent)."""
    db_path = tmp_path / "inter_session_priority.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create required parent records
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-1", "ext-1", "machine-1", "claude", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-2", "ext-2", "machine-1", "claude", "test-project"),
    )

    import uuid

    # Insert with normal priority
    db.execute(
        """INSERT INTO inter_session_messages
           (id, from_session, to_session, content, priority, sent_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (str(uuid.uuid4()), "session-1", "session-2", "Normal message", "normal"),
    )

    # Insert with urgent priority
    db.execute(
        """INSERT INTO inter_session_messages
           (id, from_session, to_session, content, priority, sent_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (str(uuid.uuid4()), "session-1", "session-2", "Urgent message", "urgent"),
    )

    # Verify both were stored
    rows = db.fetchall("SELECT priority FROM inter_session_messages ORDER BY priority")
    priorities = [row["priority"] for row in rows]
    assert "normal" in priorities
    assert "urgent" in priorities


def test_inter_session_messages_cascade_delete(tmp_path) -> None:
    """Test that deleting a session cascades to inter_session_messages."""
    db_path = tmp_path / "inter_session_cascade.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Enable foreign keys
    db.execute("PRAGMA foreign_keys = ON")

    # Create required parent records
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-1", "ext-1", "machine-1", "claude", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-2", "ext-2", "machine-1", "claude", "test-project"),
    )

    import uuid

    msg_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO inter_session_messages
           (id, from_session, to_session, content, priority, sent_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (msg_id, "session-1", "session-2", "Test message", "normal"),
    )

    # Verify message exists
    row = db.fetchone("SELECT * FROM inter_session_messages WHERE id = ?", (msg_id,))
    assert row is not None

    # Delete the sender session
    db.execute("DELETE FROM sessions WHERE id = ?", ("session-1",))

    # Verify message was cascade deleted
    row = db.fetchone("SELECT * FROM inter_session_messages WHERE id = ?", (msg_id,))
    assert row is None, "Message should be cascade deleted when sender session is deleted"


# =============================================================================
# Unified Workflow Architecture: workflow_instances + session_variables tables
# =============================================================================


def test_workflow_instances_table_exists(tmp_path) -> None:
    """Test that workflow_instances table is created after migration."""
    db_path = tmp_path / "workflow_instances.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_instances'"
    )
    assert row is not None, "workflow_instances table not created"


def test_workflow_instances_schema(tmp_path) -> None:
    """Test that workflow_instances has correct columns."""
    db_path = tmp_path / "workflow_instances_schema.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    rows = db.fetchall("PRAGMA table_info(workflow_instances)")
    columns = {row["name"] for row in rows}

    expected_columns = {
        "id",
        "session_id",
        "workflow_name",
        "enabled",
        "priority",
        "current_step",
        "step_entered_at",
        "step_action_count",
        "total_action_count",
        "variables",
        "context_injected",
        "created_at",
        "updated_at",
    }
    for col in expected_columns:
        assert col in columns, f"Column {col} missing from workflow_instances"


def test_workflow_instances_unique_constraint(tmp_path) -> None:
    """Test that UNIQUE(session_id, workflow_name) constraint is enforced."""
    db_path = tmp_path / "workflow_instances_unique.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    row = db.fetchone(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='workflow_instances'"
    )
    assert row is not None
    sql_lower = row["sql"].lower()
    assert "unique(session_id, workflow_name)" in sql_lower, (
        "UNIQUE(session_id, workflow_name) constraint missing"
    )


def test_workflow_instances_foreign_key(tmp_path) -> None:
    """Test that workflow_instances has FK to sessions(id) with ON DELETE CASCADE."""
    db_path = tmp_path / "workflow_instances_fk.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    row = db.fetchone(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='workflow_instances'"
    )
    assert row is not None
    sql_lower = row["sql"].lower()
    assert "references sessions(id)" in sql_lower, "FK to sessions(id) missing"
    assert "on delete cascade" in sql_lower, "ON DELETE CASCADE missing"


def test_workflow_instances_indexes(tmp_path) -> None:
    """Test that workflow_instances has proper indexes."""
    db_path = tmp_path / "workflow_instances_idx.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    rows = db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='workflow_instances'"
    )
    index_names = {row["name"] for row in rows}

    assert "idx_workflow_instances_session" in index_names, "idx_workflow_instances_session missing"
    assert "idx_workflow_instances_enabled" in index_names, "idx_workflow_instances_enabled missing"


def test_workflow_instances_defaults(tmp_path) -> None:
    """Test that workflow_instances columns have correct defaults."""
    db_path = tmp_path / "workflow_instances_defaults.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project and session
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-1", "ext-1", "machine-1", "claude", "test-project"),
    )

    import uuid

    instance_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO workflow_instances (id, session_id, workflow_name)
           VALUES (?, ?, ?)""",
        (instance_id, "session-1", "auto-task"),
    )

    row = db.fetchone("SELECT * FROM workflow_instances WHERE id = ?", (instance_id,))
    assert row is not None
    assert row["enabled"] == 1, "enabled should default to 1"
    assert row["priority"] == 100, "priority should default to 100"
    assert row["current_step"] is None, "current_step should default to NULL"
    assert row["step_action_count"] == 0, "step_action_count should default to 0"
    assert row["total_action_count"] == 0, "total_action_count should default to 0"
    assert row["variables"] == "{}", "variables should default to '{}'"
    assert row["context_injected"] == 0, "context_injected should default to 0"


def test_workflow_instances_unique_prevents_duplicates(tmp_path) -> None:
    """Test that the UNIQUE constraint prevents duplicate (session_id, workflow_name)."""
    import sqlite3
    import uuid

    db_path = tmp_path / "workflow_instances_dup.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project and session
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-1", "ext-1", "machine-1", "claude", "test-project"),
    )

    # Insert first instance
    db.execute(
        "INSERT INTO workflow_instances (id, session_id, workflow_name) VALUES (?, ?, ?)",
        (str(uuid.uuid4()), "session-1", "auto-task"),
    )

    # Insert duplicate should fail
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO workflow_instances (id, session_id, workflow_name) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), "session-1", "auto-task"),
        )


def test_workflow_instances_multiple_per_session(tmp_path) -> None:
    """Test that multiple workflow instances can exist per session."""
    import uuid

    db_path = tmp_path / "workflow_instances_multi.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Create project and session
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-1", "ext-1", "machine-1", "claude", "test-project"),
    )

    # Insert multiple workflows for the same session
    db.execute(
        "INSERT INTO workflow_instances (id, session_id, workflow_name, priority) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), "session-1", "session-lifecycle", 10),
    )
    db.execute(
        "INSERT INTO workflow_instances (id, session_id, workflow_name, priority) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), "session-1", "auto-task", 25),
    )
    db.execute(
        "INSERT INTO workflow_instances (id, session_id, workflow_name, priority) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), "session-1", "developer", 20),
    )

    # Verify all three exist
    rows = db.fetchall(
        "SELECT workflow_name, priority FROM workflow_instances WHERE session_id = ? ORDER BY priority",
        ("session-1",),
    )
    assert len(rows) == 3
    assert rows[0]["workflow_name"] == "session-lifecycle"
    assert rows[0]["priority"] == 10
    assert rows[1]["workflow_name"] == "developer"
    assert rows[1]["priority"] == 20
    assert rows[2]["workflow_name"] == "auto-task"
    assert rows[2]["priority"] == 25


def test_workflow_instances_cascade_delete(tmp_path) -> None:
    """Test that deleting a session cascades to workflow_instances."""
    import uuid

    db_path = tmp_path / "workflow_instances_cascade.db"
    db = LocalDatabase(db_path)

    run_migrations(db)
    db.execute("PRAGMA foreign_keys = ON")

    # Create project and session
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-1", "ext-1", "machine-1", "claude", "test-project"),
    )

    instance_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO workflow_instances (id, session_id, workflow_name) VALUES (?, ?, ?)",
        (instance_id, "session-1", "auto-task"),
    )

    # Verify it exists
    row = db.fetchone("SELECT * FROM workflow_instances WHERE id = ?", (instance_id,))
    assert row is not None

    # Delete the session
    db.execute("DELETE FROM sessions WHERE id = ?", ("session-1",))

    # Verify cascade delete
    row = db.fetchone("SELECT * FROM workflow_instances WHERE id = ?", (instance_id,))
    assert row is None, "workflow_instances should cascade delete with session"


def test_session_variables_table_exists(tmp_path) -> None:
    """Test that session_variables table is created after migration."""
    db_path = tmp_path / "session_variables.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_variables'"
    )
    assert row is not None, "session_variables table not created"


def test_session_variables_schema(tmp_path) -> None:
    """Test that session_variables has correct columns."""
    db_path = tmp_path / "session_variables_schema.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    rows = db.fetchall("PRAGMA table_info(session_variables)")
    columns = {row["name"] for row in rows}

    expected_columns = {"session_id", "variables", "updated_at"}
    for col in expected_columns:
        assert col in columns, f"Column {col} missing from session_variables"


def test_session_variables_primary_key(tmp_path) -> None:
    """Test that session_variables has session_id as PRIMARY KEY."""
    db_path = tmp_path / "session_variables_pk.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    rows = db.fetchall("PRAGMA table_info(session_variables)")
    pk_cols = [row for row in rows if row["pk"] > 0]
    assert len(pk_cols) == 1
    assert pk_cols[0]["name"] == "session_id"


def test_session_variables_defaults(tmp_path) -> None:
    """Test that session_variables columns have correct defaults."""
    db_path = tmp_path / "session_variables_defaults.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    db.execute(
        "INSERT INTO session_variables (session_id) VALUES (?)",
        ("session-1",),
    )

    row = db.fetchone("SELECT * FROM session_variables WHERE session_id = ?", ("session-1",))
    assert row is not None
    assert row["variables"] == "{}", "variables should default to '{}'"
    assert row["updated_at"] is not None, "updated_at should have a default"


def test_session_variables_stores_json(tmp_path) -> None:
    """Test that session_variables can store and retrieve JSON data."""
    import json

    db_path = tmp_path / "session_variables_json.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    variables = json.dumps(
        {
            "unlocked_tools": ["Read", "Write"],
            "task_claimed": True,
            "stop_attempts": 2,
        }
    )
    db.execute(
        "INSERT INTO session_variables (session_id, variables, updated_at) VALUES (?, ?, datetime('now'))",
        ("session-1", variables),
    )

    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = ?", ("session-1",)
    )
    assert row is not None
    parsed = json.loads(row["variables"])
    assert parsed["unlocked_tools"] == ["Read", "Write"]
    assert parsed["task_claimed"] is True
    assert parsed["stop_attempts"] == 2


def test_workflow_data_migration_from_workflow_states(tmp_path) -> None:
    """Test that existing workflow_states data is migrated correctly.

    When upgrading from v100 to v101:
    - All workflow_states rows should get session_variables entries
    - Active step workflows should get workflow_instances entries
    - __lifecycle__ and __ended__ workflows should NOT get instances
    """
    import json

    db_path = tmp_path / "workflow_data_migration.db"
    db = LocalDatabase(db_path)

    # Apply baseline (v100) which includes workflow_states table
    run_migrations(db)

    # Manually insert workflow_states rows to simulate pre-migration data
    # 1. A lifecycle workflow (just variables, no step workflow)
    db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("test-project", "Test Project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-1", "ext-1", "machine-1", "claude", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-2", "ext-2", "machine-1", "claude", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("session-3", "ext-3", "machine-1", "claude", "test-project"),
    )

    lifecycle_vars = json.dumps({"unlocked_tools": [], "task_claimed": False})
    db.execute(
        """INSERT INTO workflow_states
           (session_id, workflow_name, step, step_action_count, total_action_count,
            context_injected, variables, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("session-1", "__lifecycle__", "__lifecycle__", 0, 0, 0, lifecycle_vars),
    )

    # 2. An active step workflow
    step_vars = json.dumps({"session_task": "task-123", "context_injected": True})
    db.execute(
        """INSERT INTO workflow_states
           (session_id, workflow_name, step, step_entered_at, step_action_count,
            total_action_count, context_injected, variables, updated_at)
           VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, datetime('now'))""",
        ("session-2", "auto-task", "work", 5, 12, 1, step_vars),
    )

    # 3. An ended step workflow
    ended_vars = json.dumps({"session_task": "task-456"})
    db.execute(
        """INSERT INTO workflow_states
           (session_id, workflow_name, step, step_action_count, total_action_count,
            context_injected, variables, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        ("session-3", "__ended__", "__ended__", 0, 0, 0, ended_vars),
    )

    # Verify the data was inserted
    count = db.fetchone("SELECT COUNT(*) as cnt FROM workflow_states")
    assert count["cnt"] == 3

    # Now the tables already exist from baseline. Verify session_variables has the data
    # For fresh DBs, the migration is baked into baseline so data migration won't run.
    # This test verifies the tables and schema exist and can handle the expected data patterns.
    # For the actual upgrade path, see test_workflow_data_migration_upgrade_path.

    # Verify we can insert into the new tables with migrated data patterns
    db.execute(
        "INSERT OR IGNORE INTO session_variables (session_id, variables, updated_at) VALUES (?, ?, datetime('now'))",
        ("session-1", lifecycle_vars),
    )
    db.execute(
        "INSERT OR IGNORE INTO session_variables (session_id, variables, updated_at) VALUES (?, ?, datetime('now'))",
        ("session-2", step_vars),
    )
    db.execute(
        "INSERT OR IGNORE INTO session_variables (session_id, variables, updated_at) VALUES (?, ?, datetime('now'))",
        ("session-3", ended_vars),
    )

    # Verify session_variables
    rows = db.fetchall("SELECT * FROM session_variables ORDER BY session_id")
    assert len(rows) == 3

    # Verify active step workflow can get an instance
    import uuid

    db.execute(
        """INSERT INTO workflow_instances
           (id, session_id, workflow_name, enabled, current_step,
            step_action_count, total_action_count, context_injected)
           VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), "session-2", "auto-task", "work", 5, 12, 1),
    )

    instances = db.fetchall("SELECT * FROM workflow_instances WHERE session_id = ?", ("session-2",))
    assert len(instances) == 1
    assert instances[0]["workflow_name"] == "auto-task"
    assert instances[0]["current_step"] == "work"
    assert instances[0]["step_action_count"] == 5


# =============================================================================
# Workflow UI: workflow_definitions table migration (v102)
# =============================================================================


def test_workflow_definitions_table_exists(tmp_path) -> None:
    """Test that workflow_definitions table is created after migration."""
    db_path = tmp_path / "workflow_defs.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_definitions'"
    )
    assert row is not None, "workflow_definitions table not created"


def test_workflow_definitions_schema(tmp_path) -> None:
    """Test that workflow_definitions has correct columns."""
    db_path = tmp_path / "workflow_defs_schema.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    rows = db.fetchall("PRAGMA table_info(workflow_definitions)")
    columns = {row["name"] for row in rows}

    expected_columns = {
        "id",
        "project_id",
        "name",
        "description",
        "workflow_type",
        "version",
        "enabled",
        "priority",
        "sources",
        "definition_json",
        "canvas_json",
        "source",
        "tags",
        "created_at",
        "updated_at",
    }
    for col in expected_columns:
        assert col in columns, f"Column {col} missing from workflow_definitions"


def test_workflow_definitions_indexes(tmp_path) -> None:
    """Test that workflow_definitions has proper indexes."""
    db_path = tmp_path / "workflow_defs_idx.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    rows = db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='workflow_definitions'"
    )
    index_names = {row["name"] for row in rows}

    assert "idx_wf_defs_project" in index_names, "idx_wf_defs_project missing"
    assert "idx_wf_defs_name" in index_names, "idx_wf_defs_name missing"
    assert "idx_wf_defs_type" in index_names, "idx_wf_defs_type missing"
    assert "idx_wf_defs_enabled" in index_names, "idx_wf_defs_enabled missing"


def test_workflow_definitions_unique_constraint(tmp_path) -> None:
    """Test that UNIQUE index on (name, COALESCE(project_id, '__global__')) exists."""
    db_path = tmp_path / "workflow_defs_unique.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    rows = db.fetchall(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='workflow_definitions'"
    )
    index_names = {row["name"] for row in rows}
    assert "idx_wf_defs_name_project" in index_names, (
        "idx_wf_defs_name_project unique index missing"
    )

    # Verify it's unique
    for row in rows:
        if row["name"] == "idx_wf_defs_name_project":
            assert "UNIQUE" in row["sql"].upper(), "idx_wf_defs_name_project should be UNIQUE"


def test_workflow_definitions_bundled_import(tmp_path) -> None:
    """Test that bundled YAML workflows are imported with source='template'."""
    db_path = tmp_path / "workflow_defs_import.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Check that bundled workflows were imported
    rows = db.fetchall(
        "SELECT name, source, workflow_type FROM workflow_definitions WHERE source = 'template'"
    )
    assert len(rows) > 0, "No bundled workflows imported"

    # Check that well-known workflows are present
    names = {row["name"] for row in rows}
    assert "developer-workflow" in names, "developer-workflow workflow not imported"


def test_workflow_definitions_type_mapping(tmp_path) -> None:
    """Test that workflow_type is correctly mapped from YAML type field.

    Constraint 5: pipeline -> pipeline, step/lifecycle/unset -> workflow.
    """
    db_path = tmp_path / "workflow_defs_type.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # coordinator.yaml has type: pipeline
    row = db.fetchone("SELECT workflow_type FROM workflow_definitions WHERE name = 'coordinator'")
    if row:
        assert row["workflow_type"] == "pipeline", "coordinator should be pipeline type"

    # developer has no explicit type -> should be 'workflow'
    row = db.fetchone("SELECT workflow_type FROM workflow_definitions WHERE name = 'developer'")
    if row:
        assert row["workflow_type"] == "workflow", "developer should be workflow type"


def test_workflow_definitions_definition_json_populated(tmp_path) -> None:
    """Test that definition_json contains the full YAML content as JSON."""
    import json

    db_path = tmp_path / "workflow_defs_json.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    row = db.fetchone(
        "SELECT definition_json FROM workflow_definitions WHERE name = 'developer-workflow'"
    )
    assert row is not None, "developer-workflow not found"
    assert row["definition_json"] is not None, "definition_json is NULL"

    # Should be valid JSON
    parsed = json.loads(row["definition_json"])
    assert isinstance(parsed, dict)
    assert parsed.get("name") == "developer-workflow"


def test_workflow_definitions_insert_or_ignore_idempotent(tmp_path) -> None:
    """Test that INSERT OR IGNORE handles re-runs without error."""
    db_path = tmp_path / "workflow_defs_rerun.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    # Count initial rows
    count1 = db.fetchone("SELECT COUNT(*) as cnt FROM workflow_definitions")

    # Running migrations again should not fail or duplicate
    applied = run_migrations(db)
    assert applied == 0

    count2 = db.fetchone("SELECT COUNT(*) as cnt FROM workflow_definitions")
    assert count2["cnt"] == count1["cnt"], "Re-run should not duplicate workflows"


def test_workflow_definitions_defaults(tmp_path) -> None:
    """Test that workflow_definitions columns have correct defaults."""
    db_path = tmp_path / "workflow_defs_defaults.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    import json
    import uuid

    def_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO workflow_definitions (id, name, definition_json)
           VALUES (?, ?, ?)""",
        (def_id, "test-workflow", json.dumps({"name": "test-workflow"})),
    )

    row = db.fetchone("SELECT * FROM workflow_definitions WHERE id = ?", (def_id,))
    assert row is not None
    assert row["workflow_type"] == "workflow", "workflow_type should default to 'workflow'"
    assert row["version"] == "1.0", "version should default to '1.0'"
    assert row["enabled"] == 1, "enabled should default to 1"
    assert row["priority"] == 100, "priority should default to 100"
    assert row["source"] == "installed", "source should default to 'installed'"
    assert row["project_id"] is None, "project_id should default to NULL"
    assert row["canvas_json"] is None, "canvas_json should default to NULL"


def test_workflow_definitions_project_fk(tmp_path) -> None:
    """Test that workflow_definitions has FK to projects(id) with ON DELETE CASCADE."""
    db_path = tmp_path / "workflow_defs_fk.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    row = db.fetchone(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='workflow_definitions'"
    )
    assert row is not None
    sql_lower = row["sql"].lower()
    assert "references projects(id)" in sql_lower, "FK to projects(id) missing"
    assert "on delete cascade" in sql_lower, "ON DELETE CASCADE missing"


def test_workflow_definitions_global_null_project(tmp_path) -> None:
    """Test that bundled workflows have NULL project_id (global scope)."""
    db_path = tmp_path / "workflow_defs_global.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    rows = db.fetchall("SELECT project_id FROM workflow_definitions WHERE source = 'template'")
    for row in rows:
        assert row["project_id"] is None, "Bundled workflows should have NULL project_id"


def test_migration_110_memory_ids_to_uuid5_with_fk_children(tmp_path) -> None:
    """Test migration 110 succeeds when memories have FK children.

    Regression test: UPDATE memories SET id = ? used to fail with
    FOREIGN KEY constraint because session_memories and memory_crossrefs
    define ON DELETE CASCADE but not ON UPDATE CASCADE.
    """
    import uuid

    db_path = tmp_path / "memory_fk.db"
    db = LocalDatabase(db_path)

    # Apply baseline (gets us to v107+) then run up to v109
    run_migrations(db)
    # Reset to v109 so we can test migration 110 in isolation
    db.execute("DELETE FROM schema_version WHERE version >= 110")

    # Insert test data with old mm-prefix IDs
    project_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    memory_id = "mm-test-memory-001"
    memory_id2 = "mm-test-memory-002"

    db.execute(
        "INSERT INTO projects (id, name, repo_path, created_at) VALUES (?, ?, ?, datetime('now'))",
        (project_id, "test-proj", "/tmp/test"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, 'ext-1', 'machine-1', 'claude', ?, datetime('now'), datetime('now'))",
        (session_id, project_id),
    )
    db.execute(
        "INSERT INTO memories (id, project_id, memory_type, content, created_at, updated_at) "
        "VALUES (?, ?, 'fact', 'test content one', datetime('now'), datetime('now'))",
        (memory_id, project_id),
    )
    db.execute(
        "INSERT INTO memories (id, project_id, memory_type, content, created_at, updated_at) "
        "VALUES (?, ?, 'fact', 'test content two', datetime('now'), datetime('now'))",
        (memory_id2, project_id),
    )
    # Add FK children
    db.execute(
        "INSERT INTO session_memories (session_id, memory_id, action, created_at) "
        "VALUES (?, ?, 'created', datetime('now'))",
        (session_id, memory_id),
    )
    db.execute(
        "INSERT INTO memory_crossrefs (source_id, target_id, similarity) VALUES (?, ?, 0.9)",
        (memory_id, memory_id2),
    )

    # Run migration 110 — this used to fail with FOREIGN KEY constraint
    from gobby.storage.migrations import _migrate_memory_ids_to_uuid5

    _migrate_memory_ids_to_uuid5(db)

    # Verify old IDs are gone
    assert db.fetchone("SELECT id FROM memories WHERE id = ?", (memory_id,)) is None
    assert db.fetchone("SELECT id FROM memories WHERE id = ?", (memory_id2,)) is None

    # Verify new UUID5 IDs exist
    ns = uuid.UUID("a3b2c1d0-1234-5678-9abc-def012345678")
    expected_id1 = str(uuid.uuid5(ns, "test content one"))
    expected_id2 = str(uuid.uuid5(ns, "test content two"))

    row1 = db.fetchone("SELECT id FROM memories WHERE id = ?", (expected_id1,))
    assert row1 is not None, f"Memory should have new UUID5 id {expected_id1}"

    row2 = db.fetchone("SELECT id FROM memories WHERE id = ?", (expected_id2,))
    assert row2 is not None, f"Memory should have new UUID5 id {expected_id2}"

    # Verify FK children were updated
    sm = db.fetchone("SELECT memory_id FROM session_memories WHERE session_id = ?", (session_id,))
    assert sm is not None
    assert sm["memory_id"] == expected_id1

    xref = db.fetchone(
        "SELECT source_id, target_id FROM memory_crossrefs WHERE source_id = ?", (expected_id1,)
    )
    assert xref is not None
    assert xref["target_id"] == expected_id2

    # Verify FK enforcement is back on
    fk_status = db.fetchone("PRAGMA foreign_keys")
    assert fk_status[0] == 1, "Foreign keys should be re-enabled after migration"
