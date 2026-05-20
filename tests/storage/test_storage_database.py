"""Tests for the LocalDatabase storage layer."""

import gc
import sqlite3
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


class TestLocalDatabase:
    """Tests for LocalDatabase class."""

    def test_init_creates_directory(self, temp_dir: Path) -> None:
        """Test that database initialization creates parent directory."""
        db_path = temp_dir / "subdir" / "test.db"
        db = LocalDatabase(db_path)
        assert db_path.parent.exists()
        db.close()

    def test_init_with_explicit_path(self, temp_dir: Path) -> None:
        """Test database creation with explicit path."""
        db_path = temp_dir / "custom" / "test.db"
        db = LocalDatabase(db_path)
        assert db.db_path == db_path
        assert db_path.parent.exists()
        db.close()

    def test_execute_returns_cursor(self, temp_db: LocalDatabase) -> None:
        """Test that execute returns a cursor."""
        cursor = temp_db.execute("SELECT 1 as value")
        assert isinstance(cursor, sqlite3.Cursor)

    def test_fetchone_returns_row(self, temp_db: LocalDatabase) -> None:
        """Test fetchone returns a single row."""
        row = temp_db.fetchone("SELECT 1 as value, 'test' as name")
        assert row is not None
        assert row["value"] == 1
        assert row["name"] == "test"

    def test_fetchone_returns_none_for_no_results(self, temp_db: LocalDatabase) -> None:
        """Test fetchone returns None when no results."""
        row = temp_db.fetchone("SELECT * FROM projects WHERE id = 'nonexistent'")
        assert row is None

    def test_fetchall_returns_list(self, temp_db: LocalDatabase) -> None:
        """Test fetchall returns a list of rows."""
        rows = temp_db.fetchall("SELECT 1 as value UNION SELECT 2 UNION SELECT 3")
        assert len(rows) == 3
        values = [row["value"] for row in rows]
        assert sorted(values) == [1, 2, 3]

    def test_fetchall_returns_empty_list_for_no_results(self, temp_db: LocalDatabase) -> None:
        """Test fetchall returns empty list when no results."""
        rows = temp_db.fetchall("SELECT * FROM projects WHERE id = 'nonexistent'")
        assert rows == []

    def test_executemany(self, temp_db: LocalDatabase) -> None:
        """Test executemany with multiple parameter sets."""
        # Create test table
        temp_db.execute("CREATE TABLE test_items (id INTEGER, name TEXT)")

        # Insert multiple rows
        temp_db.executemany(
            "INSERT INTO test_items (id, name) VALUES ($1, $2)",
            [(1, "one"), (2, "two"), (3, "three")],
        )

        rows = temp_db.fetchall("SELECT * FROM test_items ORDER BY id")
        assert len(rows) == 3
        assert rows[0]["name"] == "one"
        assert rows[2]["name"] == "three"

    def test_numbered_placeholders_remap_for_local_sqlite(self, temp_db: LocalDatabase) -> None:
        """SQLite accepts storage's author-facing $N parameter style."""
        temp_db.execute("CREATE TABLE numbered_items (a TEXT, b TEXT)")
        temp_db.execute("INSERT INTO numbered_items (a, b) VALUES ($2, $1)", ("left", "right"))

        row = temp_db.fetchone(
            "SELECT a, b FROM numbered_items WHERE a = $1 AND b = $2",
            ("right", "left"),
        )

        assert row is not None
        assert row["a"] == "right"
        assert row["b"] == "left"

    def test_numbered_placeholders_remap_for_transaction_connection(
        self,
        temp_db: LocalDatabase,
    ) -> None:
        """Raw transaction connections use the same $N remapping."""
        temp_db.execute("CREATE TABLE numbered_tx_items (a TEXT, b TEXT)")

        with temp_db.transaction() as conn:
            conn.execute(
                "INSERT INTO numbered_tx_items (a, b) VALUES ($2, $1)",
                ("left", "right"),
            )

        row = temp_db.fetchone("SELECT a, b FROM numbered_tx_items WHERE b = $1", ("left",))
        assert row is not None
        assert row["a"] == "right"

    def test_transaction_commit(self, temp_db: LocalDatabase) -> None:
        """Test successful transaction commits."""
        temp_db.execute("CREATE TABLE test_tx (id INTEGER, value TEXT)")

        with temp_db.transaction():
            temp_db.execute("INSERT INTO test_tx VALUES (1, 'first')")
            temp_db.execute("INSERT INTO test_tx VALUES (2, 'second')")

        # Data should be committed
        rows = temp_db.fetchall("SELECT * FROM test_tx")
        assert len(rows) == 2

    def test_transaction_rollback_on_error(self, temp_db: LocalDatabase) -> None:
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

    def test_nested_transaction_uses_savepoint(self, temp_db: LocalDatabase) -> None:
        """Nested transactions should commit via savepoints instead of failing BEGIN."""
        temp_db.execute("CREATE TABLE test_nested_tx (id INTEGER PRIMARY KEY, value TEXT)")

        with temp_db.transaction():
            temp_db.execute("INSERT INTO test_nested_tx VALUES (1, 'outer')")
            with temp_db.transaction():
                temp_db.execute("INSERT INTO test_nested_tx VALUES (2, 'inner')")

        rows = temp_db.fetchall("SELECT id, value FROM test_nested_tx ORDER BY id")
        assert [(row["id"], row["value"]) for row in rows] == [(1, "outer"), (2, "inner")]

    def test_nested_transaction_can_roll_back_inner_scope(self, temp_db: LocalDatabase) -> None:
        """An inner savepoint rollback should not discard outer work if handled."""
        temp_db.execute("CREATE TABLE test_nested_rollback (id INTEGER PRIMARY KEY, value TEXT)")

        with temp_db.transaction():
            temp_db.execute("INSERT INTO test_nested_rollback VALUES (1, 'outer')")
            with pytest.raises(sqlite3.IntegrityError):
                with temp_db.transaction():
                    temp_db.execute("INSERT INTO test_nested_rollback VALUES (2, 'inner')")
                    temp_db.execute("INSERT INTO test_nested_rollback VALUES (2, 'duplicate')")
            temp_db.execute("INSERT INTO test_nested_rollback VALUES (3, 'outer-after')")

        rows = temp_db.fetchall("SELECT id, value FROM test_nested_rollback ORDER BY id")
        assert [(row["id"], row["value"]) for row in rows] == [
            (1, "outer"),
            (3, "outer-after"),
        ]

    def test_after_commit_runs_after_outer_commit(self, temp_db: LocalDatabase) -> None:
        """Callbacks registered in nested scopes should run only after outer commit."""
        events: list[str] = []

        with temp_db.transaction():
            temp_db.after_commit(lambda: events.append("outer"))
            with temp_db.transaction():
                temp_db.after_commit(lambda: events.append("inner"))
                assert events == []
            assert events == []

        assert events == ["outer", "inner"]

    def test_after_commit_discards_callbacks_on_rollback(self, temp_db: LocalDatabase) -> None:
        """Callbacks in rolled-back scopes should never run."""
        events: list[str] = []

        with pytest.raises(RuntimeError, match="boom"):
            with temp_db.transaction():
                temp_db.after_commit(lambda: events.append("outer"))
                with temp_db.transaction():
                    temp_db.after_commit(lambda: events.append("inner"))
                    raise RuntimeError("boom")

        assert events == []

    def test_thread_local_connections(self, temp_dir: Path) -> None:
        """Test that each thread gets its own connection."""
        db_path = temp_dir / "thread_test.db"
        db = LocalDatabase(db_path)

        # Initialize schema
        db.execute("CREATE TABLE test_threads (thread_id TEXT)")

        connections = []

        def worker(thread_id: str):
            conn = db.connection
            connections.append((thread_id, conn))

        threads = [threading.Thread(target=worker, args=(f"thread-{i}",)) for i in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Hold strong references to every connection object so none can be
        # GC'd and have its id() reused by a later one — that reuse would
        # otherwise cause a false collision and a flaky failure.
        conn_objs = [conn for _, conn in connections]
        assert len({id(c) for c in conn_objs}) == 3

        db.close()

    def test_close_connection(self, temp_dir: Path) -> None:
        """Test closing database connection."""
        db_path = temp_dir / "close_test.db"
        db = LocalDatabase(db_path)

        # Ensure connection is created
        _ = db.connection

        db.close()

        # Connection should be None after close
        assert not hasattr(db._local, "connection") or db._local.connection is None

    def test_row_factory_returns_dict_like_rows(self, temp_db: LocalDatabase) -> None:
        """Test that rows can be accessed like dicts."""
        row = temp_db.fetchone("SELECT 1 as a, 2 as b, 3 as c")
        assert row["a"] == 1
        assert row["b"] == 2
        assert row["c"] == 3

    def test_foreign_keys_enabled(self, temp_db: LocalDatabase) -> None:
        """Test that foreign keys are enabled."""
        row = temp_db.fetchone("PRAGMA foreign_keys")
        assert row[0] == 1

    def test_busy_timeout_configured(self, temp_db: LocalDatabase) -> None:
        """Connections should wait briefly instead of failing on immediate lock contention."""
        row = temp_db.fetchone("PRAGMA busy_timeout")
        assert row[0] == 10000

    def test_connection_count_tracks_open_and_closed_connections(self, temp_dir: Path) -> None:
        """connection_count reflects tracked connections and returns to zero on close."""
        db = LocalDatabase(temp_dir / "connection_count.db")

        assert db.connection_count == 0
        _ = db.connection
        assert db.connection_count == 1

        db.close()

        assert db.connection_count == 0

    def test_can_be_garbage_collected_after_refs_drop(self, temp_dir: Path) -> None:
        """weakref.finalize cleanup must not keep LocalDatabase alive."""
        db = LocalDatabase(temp_dir / "gc.db")
        _ = db.connection
        db_ref = weakref.ref(db)

        del db
        gc.collect()

        assert db_ref() is None

    def test_worker_thread_connections_close_when_threads_exit(self, temp_dir: Path) -> None:
        """Connections from short-lived workers are removed when worker threads exit."""
        db = LocalDatabase(temp_dir / "thread_exit_connections.db")
        db.execute("CREATE TABLE thread_exit_probe (id INTEGER PRIMARY KEY)")

        def query() -> None:
            db.fetchone("SELECT 1")

        for _ in range(6):
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(query).result(timeout=5)

        # CPython keeps thread-local objects alive until the worker thread is fully
        # joined and a collection pass runs, so force GC after the executors exit.
        gc.collect()

        assert db.connection_count == 1  # main thread only
        db.close()

    def test_close_closes_worker_thread_connections(self, temp_dir: Path) -> None:
        """close() closes connections opened from multiple worker threads."""
        db = LocalDatabase(temp_dir / "worker_connections.db")
        db.execute("CREATE TABLE worker_probe (id INTEGER PRIMARY KEY)")
        barrier = threading.Barrier(5)
        release = threading.Event()

        def open_connection() -> sqlite3.Connection:
            conn = db.connection
            conn.execute("SELECT 1")
            barrier.wait(timeout=5)
            release.wait(timeout=5)
            return conn

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(open_connection) for _ in range(4)]
            barrier.wait(timeout=5)

            assert db.connection_count == 5  # main thread + four live worker threads

            db.close()
            assert db.connection_count == 0

            release.set()
            connections = [future.result(timeout=5) for future in futures]

        for conn in connections:
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_close_is_idempotent_and_rejects_future_use(self, temp_dir: Path) -> None:
        """A closed LocalDatabase cannot reopen thread-local connections."""
        db = LocalDatabase(temp_dir / "closed.db")
        _ = db.connection

        db.close()
        db.close()

        assert db.connection_count == 0
        with pytest.raises(RuntimeError, match="LocalDatabase is closed"):
            _ = db.connection
