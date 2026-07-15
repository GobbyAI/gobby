"""Tests for the PostgreSQL hub database adapter."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import ClassVar

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
        row = temp_db.fetchone(
            "SELECT * FROM projects WHERE id = %s",
            ("00000000-0000-0000-0000-0000000000ff",),
        )

        assert row is None

    def test_fetchall_returns_rows(self, temp_db: HubDatabase) -> None:
        rows = temp_db.fetchall("SELECT value FROM (VALUES (1), (2), (3)) AS t(value)")

        assert [row["value"] for row in rows] == [1, 2, 3]

    def test_executemany(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE test_items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")

        temp_db.executemany(
            "INSERT INTO test_items (id, name) VALUES (%s, %s)",
            [(1, "one"), (2, "two"), (3, "three")],
        )

        rows = temp_db.fetchall("SELECT * FROM test_items ORDER BY id")
        assert [row["name"] for row in rows] == ["one", "two", "three"]

    def test_positional_placeholders_bind_in_order(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE positional_items (a TEXT, b TEXT)")
        temp_db.execute("INSERT INTO positional_items (a, b) VALUES (%s, %s)", ("left", "right"))

        row = temp_db.fetchone(
            "SELECT a, b FROM positional_items WHERE a = %s AND b = %s",
            ("left", "right"),
        )

        assert row is not None
        assert row["a"] == "left"
        assert row["b"] == "right"

    def test_native_psycopg_placeholders_are_used_directly(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE psycopg_items (a TEXT, b TEXT)")
        temp_db.execute("INSERT INTO psycopg_items (a, b) VALUES (%s, %s)", ("left", "right"))

        row = temp_db.fetchone(
            "SELECT a, b FROM psycopg_items WHERE a = %s AND b = %s",
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

    def test_savepoint_can_roll_back_inner_scope(self, temp_db: HubDatabase) -> None:
        temp_db.execute("CREATE TABLE test_nested_rollback (id INTEGER PRIMARY KEY, value TEXT)")

        with temp_db.transaction() as txn:
            txn.execute("INSERT INTO test_nested_rollback VALUES (1, 'outer')")
            savepoint = txn.savepoint("inner")
            try:
                txn.execute("INSERT INTO test_nested_rollback VALUES (2, 'inner')")
                txn.execute("INSERT INTO test_nested_rollback VALUES (2, 'duplicate')")
            except UniqueViolation:
                savepoint.rollback()
            else:
                savepoint.release()
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

    @pytest.mark.asyncio
    async def test_interleaved_tasks_isolate_lock_order_and_after_commit_callbacks(
        self,
        temp_db: HubDatabase,
    ) -> None:
        @dataclass(frozen=True)
        class OuterLock:
            PRIORITY: ClassVar[int] = 20
            name: str = "outer"

        @dataclass(frozen=True)
        class IndependentLock:
            PRIORITY: ClassVar[int] = 10
            name: str = "independent"

        first_entered = asyncio.Event()
        second_committed = asyncio.Event()
        callbacks_run: list[str] = []

        async def dry_run_like_transaction() -> None:
            with pytest.raises(RuntimeError, match="roll back dry run"):
                with temp_db.transaction_immediate(OuterLock()) as txn:
                    txn.after_commit(lambda: callbacks_run.append("dry-run"))
                    first_entered.set()
                    await second_committed.wait()
                    assert callbacks_run == ["committed"]
                    raise RuntimeError("roll back dry run")

        async def committed_transaction() -> None:
            await first_entered.wait()
            with temp_db.transaction_immediate(IndependentLock()) as txn:
                txn.after_commit(lambda: callbacks_run.append("committed"))
            second_committed.set()

        await asyncio.gather(dry_run_like_transaction(), committed_transaction())

        assert callbacks_run == ["committed"]

    def test_transaction_after_commit_from_another_thread_waits_for_commit(
        self,
        temp_db: HubDatabase,
    ) -> None:
        callbacks_run: list[str] = []

        with temp_db.transaction() as txn:
            worker = threading.Thread(
                target=txn.after_commit,
                args=(lambda: callbacks_run.append("committed"),),
            )
            worker.start()
            worker.join(timeout=2)

            assert not worker.is_alive()
            assert callbacks_run == []

        assert callbacks_run == ["committed"]

    def test_row_factory_returns_dict_like_rows(self, temp_db: HubDatabase) -> None:
        row = temp_db.fetchone("SELECT 1 AS a, 2 AS b, 3 AS c")

        assert row == {"a": 1, "b": 2, "c": 3}
