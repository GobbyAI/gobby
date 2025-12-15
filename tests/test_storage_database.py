"""Tests for the LocalDatabase storage layer."""

import sqlite3
import threading
from pathlib import Path

import pytest
from gobby.storage.database import LocalDatabase


class TestLocalDatabase:
    """Tests for LocalDatabase class."""

    def test_init_creates_directory(self, temp_dir: Path):
        """Test that database initialization creates parent directory."""
        db_path = temp_dir / "subdir" / "test.db"
        db = LocalDatabase(db_path)
        assert db_path.parent.exists()
        db.close()

    def test_init_with_explicit_path(self, temp_dir: Path):
        """Test database creation with explicit path."""
        db_path = temp_dir / "custom" / "test.db"
        db = LocalDatabase(db_path)
        assert db.db_path == db_path
        assert db_path.parent.exists()
        db.close()

    def test_execute_returns_cursor(self, temp_db: LocalDatabase):
        """Test that execute returns a cursor."""
        cursor = temp_db.execute("SELECT 1 as value")
        assert isinstance(cursor, sqlite3.Cursor)

    def test_fetchone_returns_row(self, temp_db: LocalDatabase):
        """Test fetchone returns a single row."""
        row = temp_db.fetchone("SELECT 1 as value, 'test' as name")
        assert row is not None
        assert row["value"] == 1
        assert row["name"] == "test"

    def test_fetchone_returns_none_for_no_results(self, temp_db: LocalDatabase):
        """Test fetchone returns None when no results."""
        row = temp_db.fetchone("SELECT * FROM projects WHERE id = 'nonexistent'")
        assert row is None

    def test_fetchall_returns_list(self, temp_db: LocalDatabase):
        """Test fetchall returns a list of rows."""
        rows = temp_db.fetchall("SELECT 1 as value UNION SELECT 2 UNION SELECT 3")
        assert len(rows) == 3
        values = [row["value"] for row in rows]
        assert sorted(values) == [1, 2, 3]

    def test_fetchall_returns_empty_list_for_no_results(self, temp_db: LocalDatabase):
        """Test fetchall returns empty list when no results."""
        rows = temp_db.fetchall("SELECT * FROM projects WHERE id = 'nonexistent'")
        assert rows == []

    def test_executemany(self, temp_db: LocalDatabase):
        """Test executemany with multiple parameter sets."""
        # Create test table
        temp_db.execute("CREATE TABLE test_items (id INTEGER, name TEXT)")

        # Insert multiple rows
        temp_db.executemany(
            "INSERT INTO test_items (id, name) VALUES (?, ?)",
            [(1, "one"), (2, "two"), (3, "three")],
        )

        rows = temp_db.fetchall("SELECT * FROM test_items ORDER BY id")
        assert len(rows) == 3
        assert rows[0]["name"] == "one"
        assert rows[2]["name"] == "three"

    def test_transaction_commit(self, temp_db: LocalDatabase):
        """Test successful transaction commits."""
        temp_db.execute("CREATE TABLE test_tx (id INTEGER, value TEXT)")

        with temp_db.transaction():
            temp_db.execute("INSERT INTO test_tx VALUES (1, 'first')")
            temp_db.execute("INSERT INTO test_tx VALUES (2, 'second')")

        # Data should be committed
        rows = temp_db.fetchall("SELECT * FROM test_tx")
        assert len(rows) == 2

    def test_transaction_rollback_on_error(self, temp_db: LocalDatabase):
        """Test transaction rolls back on error."""
        temp_db.execute("CREATE TABLE test_rollback (id INTEGER PRIMARY KEY, value TEXT)")
        temp_db.execute("INSERT INTO test_rollback VALUES (1, 'original')")

        with pytest.raises(sqlite3.IntegrityError):
            with temp_db.transaction():
                temp_db.execute("UPDATE test_rollback SET value = 'modified' WHERE id = 1")
                # This should fail due to duplicate primary key
                temp_db.execute("INSERT INTO test_rollback VALUES (1, 'duplicate')")

        # Original value should be preserved
        row = temp_db.fetchone("SELECT value FROM test_rollback WHERE id = 1")
        assert row["value"] == "original"

    def test_thread_local_connections(self, temp_dir: Path):
        """Test that each thread gets its own connection."""
        db_path = temp_dir / "thread_test.db"
        db = LocalDatabase(db_path)

        # Initialize schema
        db.execute("CREATE TABLE test_threads (thread_id TEXT)")

        connections = []

        def worker(thread_id: str):
            conn = db.connection
            connections.append((thread_id, id(conn)))

        threads = [
            threading.Thread(target=worker, args=(f"thread-{i}",))
            for i in range(3)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each thread should have a different connection object
        connection_ids = [conn_id for _, conn_id in connections]
        assert len(set(connection_ids)) == 3

        db.close()

    def test_close_connection(self, temp_dir: Path):
        """Test closing database connection."""
        db_path = temp_dir / "close_test.db"
        db = LocalDatabase(db_path)

        # Ensure connection is created
        _ = db.connection

        db.close()

        # Connection should be None after close
        assert not hasattr(db._local, "connection") or db._local.connection is None

    def test_row_factory_returns_dict_like_rows(self, temp_db: LocalDatabase):
        """Test that rows can be accessed like dicts."""
        row = temp_db.fetchone("SELECT 1 as a, 2 as b, 3 as c")
        assert row["a"] == 1
        assert row["b"] == 2
        assert row["c"] == 3

    def test_foreign_keys_enabled(self, temp_db: LocalDatabase):
        """Test that foreign keys are enabled."""
        row = temp_db.fetchone("PRAGMA foreign_keys")
        assert row[0] == 1
