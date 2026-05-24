"""Tests for the PostgreSQL hub database adapter."""

from __future__ import annotations

import pytest
from psycopg.errors import UniqueViolation

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


class TestHubDatabase:
    """Tests for the active hub database contract."""

    def test_execute_returns_cursor(self, temp_db: HubDatabase) -> None:
        cursor = temp_db.execute("SELECT 1 AS value")

        assert cursor.fetchone() == {"value": 1}

    def test_fetchone_returns_row(self, temp_db: HubDatabase) -> None:
        row = temp_db.fetchone("SELECT 1 AS value, 'test' AS name")

        assert row is not None
        assert row["value"] == 1
        assert row["name"] == "test"

    def test_fetchone_returns_none_for_no_results(self, temp_db: HubDatabase) -> None:
        row = temp_db.fetchone("SELECT * FROM projects WHERE id = ?", ("missing",))

        assert row is None

    def test_fetchall_returns_rows(self, temp_db: HubDatabase) -> None:
        rows = temp_db.fetchall("SELECT value FROM (VALUES (1), (2), (3)) AS t(value)")

        assert [row["value"] for row in rows] == [1, 2, 3]

    def test_executemany(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE test_items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")

        temp_db.executemany(
            "INSERT INTO test_items (id, name) VALUES ($1, $2)",
            [(1, "one"), (2, "two"), (3, "three")],
        )

        rows = temp_db.fetchall("SELECT * FROM test_items ORDER BY id")
        assert [row["name"] for row in rows] == ["one", "two", "three"]

    def test_numbered_placeholders_remap(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE numbered_items (a TEXT, b TEXT)")
        temp_db.execute("INSERT INTO numbered_items (a, b) VALUES ($2, $1)", ("left", "right"))

        row = temp_db.fetchone(
            "SELECT a, b FROM numbered_items WHERE a = $1 AND b = $2",
            ("right", "left"),
        )

        assert row is not None
        assert row["a"] == "right"
        assert row["b"] == "left"

    def test_qmark_placeholders_remap(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE qmark_items (a TEXT, b TEXT)")
        temp_db.execute("INSERT INTO qmark_items (a, b) VALUES (?, ?)", ("left", "right"))

        row = temp_db.fetchone(
            "SELECT a, b FROM qmark_items WHERE a = ? AND b = ?",
            ("left", "right"),
        )

        assert row == {"a": "left", "b": "right"}

    def test_transaction_commit(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE test_tx (id INTEGER PRIMARY KEY, value TEXT)")

        with temp_db.transaction() as txn:
            txn.execute("INSERT INTO test_tx VALUES (1, 'first')")
            txn.execute("INSERT INTO test_tx VALUES (2, 'second')")

        rows = temp_db.fetchall("SELECT value FROM test_tx ORDER BY id")
        assert [row["value"] for row in rows] == ["first", "second"]

    def test_transaction_rollback_on_error(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE test_rollback (id INTEGER PRIMARY KEY, value TEXT)")
        temp_db.execute("INSERT INTO test_rollback VALUES (1, 'original')")

        with pytest.raises(UniqueViolation):
            with temp_db.transaction() as txn:
                txn.execute("UPDATE test_rollback SET value = 'modified' WHERE id = 1")
                txn.execute("INSERT INTO test_rollback VALUES (1, 'duplicate')")

        row = temp_db.fetchone("SELECT value FROM test_rollback WHERE id = 1")
        assert row is not None
        assert row["value"] == "original"

    def test_nested_transaction_can_roll_back_inner_scope(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE test_nested_rollback (id INTEGER PRIMARY KEY, value TEXT)")

        with temp_db.transaction() as txn:
            txn.execute("INSERT INTO test_nested_rollback VALUES (1, 'outer')")
            with pytest.raises(UniqueViolation):
                with temp_db.transaction() as nested:
                    nested.execute("INSERT INTO test_nested_rollback VALUES (2, 'inner')")
                    nested.execute("INSERT INTO test_nested_rollback VALUES (2, 'duplicate')")
            txn.execute("INSERT INTO test_nested_rollback VALUES (3, 'outer-after')")

        rows = temp_db.fetchall("SELECT id, value FROM test_nested_rollback ORDER BY id")
        assert [(row["id"], row["value"]) for row in rows] == [
            (1, "outer"),
            (3, "outer-after"),
        ]

    def test_after_commit_runs_after_outer_commit(self, temp_db: HubDatabase) -> None:
        events: list[str] = []

        with temp_db.transaction() as txn:
            txn.after_commit(lambda: events.append("outer"))
            with temp_db.transaction() as nested:
                nested.after_commit(lambda: events.append("inner"))
                assert events == []
            assert events == []

        assert events == ["outer", "inner"]

    def test_after_commit_discards_callbacks_on_rollback(self, temp_db: HubDatabase) -> None:
        events: list[str] = []

        with pytest.raises(RuntimeError, match="boom"):
            with temp_db.transaction() as txn:
                txn.after_commit(lambda: events.append("outer"))
                with temp_db.transaction() as nested:
                    nested.after_commit(lambda: events.append("inner"))
                    raise RuntimeError("boom")

        assert events == []

    def test_row_factory_returns_dict_like_rows(self, temp_db: HubDatabase) -> None:
        row = temp_db.fetchone("SELECT 1 AS a, 2 AS b, 3 AS c")

        assert row == {"a": 1, "b": 2, "c": 3}
