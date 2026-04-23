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
    """Fresh databases apply the flattened launch baseline directly."""
    db_path = tmp_path / "migration_test.db"
    db = LocalDatabase(db_path)

    assert BASELINE_VERSION == 219
    assert MIGRATIONS == []
    assert get_current_version(db) == 0

    applied = run_migrations(db)

    assert applied == 1
    assert get_current_version(db) == 219
    versions = [row["version"] for row in db.fetchall("SELECT version FROM schema_version")]
    assert versions == [219]


def test_migrations_idempotency_at_launch_baseline(tmp_path) -> None:
    """Running migrations again on a 219 database does not add schema versions."""
    db_path = tmp_path / "idempotency.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    assert run_migrations(db) == 0
    assert get_current_version(db) == 219
    versions = [row["version"] for row in db.fetchall("SELECT version FROM schema_version")]
    assert versions == [219]


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


def test_migrations_recreate_missing_system_session(tmp_path) -> None:
    """Existing 219 databases self-heal the bootstrapped system session on startup."""
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


@pytest.mark.parametrize("legacy_version", [1, 218])
def test_pre_launch_sqlite_versions_are_unsupported(tmp_path, legacy_version) -> None:
    """Historical SQLite upgrades were dropped at the 219 launch baseline."""
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
    assert "SQLite launch baseline 219" in message
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
    db.execute("INSERT INTO schema_version (version) VALUES (220)")

    assert run_migrations(db) == 0
    assert get_current_version(db) == 220
    assert not _table_exists(db, "projects")


def test_flattened_baseline_core_tables_exist(tmp_path) -> None:
    """The 219 baseline includes representative storage domains."""
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
    }
    missing = {table for table in expected_tables if not _table_exists(db, table)}
    assert missing == set()


def test_flattened_baseline_launch_columns(tmp_path) -> None:
    """The 219 baseline exposes the canonical post-flattening columns."""
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
    assert {"claimed_by_session_id", "lifecycle_stage", "dispatch_failure_count"}.issubset(
        _column_names(db, "tasks")
    )
    assert {"graph_synced", "graph_sync_attempted_at"}.issubset(
        _column_names(db, "code_indexed_files")
    )
    assert {"callee_target_kind", "callee_symbol_id", "callee_name"}.issubset(
        _column_names(db, "code_calls")
    )
    assert {"model_family", "cache_creation_tokens", "cache_read_tokens"}.issubset(
        _column_names(db, "token_events")
    )

    assert "expansion_context" not in _column_names(db, "tasks")
    assert "expansion_status" not in _column_names(db, "tasks")
    assert "input_token_usd_per_1m" not in _column_names(db, "model_costs")
    assert "output_token_usd_per_1m" not in _column_names(db, "model_costs")


def test_flattened_baseline_indexes_and_constraints(tmp_path) -> None:
    """The 219 baseline includes indexes and FK semantics formerly added by migrations."""
    db_path = tmp_path / "baseline_indexes.db"
    db = LocalDatabase(db_path)

    run_migrations(db)

    assert {
        "idx_tasks_claimed_session",
        "idx_tasks_lifecycle_stage",
        "idx_tasks_closed_session",
    }.issubset(_index_names(db, "tasks"))
    assert {"idx_sessions_prune_status_updated_at", "idx_sessions_parent_session"}.issubset(
        _index_names(db, "sessions")
    )
    assert "idx_memories_source_session" in _index_names(db, "memories")
    assert "idx_token_events_dedup" in _index_names(db, "token_events")
    assert "idx_cc_target" in _index_names(db, "code_calls")

    rows = db.fetchall("PRAGMA foreign_key_list(tasks)")
    claimed_fk = next(row for row in rows if row["from"] == "claimed_by_session_id")
    assert claimed_fk["on_delete"] == "SET NULL"

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
