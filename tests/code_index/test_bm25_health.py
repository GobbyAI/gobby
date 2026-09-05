from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from typing import Any

import psycopg
import pytest
from psycopg.errors import QueryCanceled, lookup

from gobby.code_index import bm25_health
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.storage.hub.protocol import Cursor, HubDatabase


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class FakeConnection:
    def __init__(
        self,
        *,
        damaged: set[str] | None = None,
        missing: set[str] | None = None,
        lock_available: bool = True,
        verify_error: psycopg.Error | None = None,
        schema: str = "public",
    ) -> None:
        self.damaged = set(damaged or ())
        self.missing = set(missing or ())
        self.lock_available = lock_available
        self.verify_error = verify_error
        self.schema = schema
        self.reindexed: list[str] = []
        self.executed: list[str] = []

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self) -> Any:
        return nullcontext()

    def execute(self, query: Any, params: tuple[Any, ...] | None = None) -> FakeResult:
        query_text = query if isinstance(query, str) else repr(query)
        self.executed.append(query_text)
        if "set_config" in query_text or "pg_advisory_unlock" in query_text:
            return FakeResult([(True,)])
        if "current_schema" in query_text:
            return FakeResult([(self.schema,)])
        if "pg_try_advisory_lock" in query_text:
            return FakeResult([(self.lock_available,)])
        if "to_regclass" in query_text:
            assert params is not None
            name = str(params[0])
            return FakeResult([(None if name in self.missing else name,)])
        if "pdb.verify_index" in query_text:
            if self.verify_error is not None:
                raise self.verify_error
            assert params is not None
            name = str(params[0])
            passed = name not in self.damaged or name in self.reindexed
            detail = None if passed else "invalid chunk style tag: 254"
            return FakeResult([("segment readability", passed, detail)])
        if "REINDEX INDEX" in query_text:
            for name in bm25_health.BM25_INDEXES:
                if name.rsplit(".", 1)[1] in query_text:
                    self.reindexed.append(name)
                    return FakeResult()
            raise AssertionError(f"unexpected REINDEX target: {query_text}")
        raise AssertionError(f"unexpected SQL: {query_text}")


def test_verify_preserves_corruption_error() -> None:
    conn = FakeConnection(verify_error=lookup("XX000")("invalid chunk style tag: 254"))

    status = bm25_health.verify_bm25_indexes(conn)

    assert status["healthy"] is False
    assert {item["state"] for item in status["indexes"]} == {"damaged"}
    assert all("invalid chunk style tag: 254" in item["error"] for item in status["indexes"])


def test_verify_uses_connection_schema() -> None:
    conn = FakeConnection(schema="isolated_test")

    status = bm25_health.verify_bm25_indexes(conn)

    assert status["healthy"] is True
    assert {item["name"] for item in status["indexes"]} == {
        "isolated_test.code_symbols_search_bm25",
        "isolated_test.code_content_search_bm25",
    }


def test_verification_propagates_cancellation_owned_by_request_deadline() -> None:
    error = QueryCanceled("statement timeout")
    conn = FakeConnection(verify_error=error)

    with database_operation_deadline(timeout_seconds=2):
        with pytest.raises(QueryCanceled) as raised:
            bm25_health.verify_bm25_indexes(conn)

    assert raised.value is error


def test_native_verification_retains_unowned_query_error_diagnostics() -> None:
    conn = FakeConnection(verify_error=QueryCanceled("standalone statement timeout"))

    status = bm25_health.verify_bm25_indexes(conn)

    assert status["healthy"] is False
    assert all(item["state"] == "error" for item in status["indexes"])
    assert all("standalone statement timeout" in item["error"] for item in status["indexes"])


def test_repair_reindexes_only_damaged_index(monkeypatch: Any) -> None:
    damaged = bm25_health.BM25_INDEXES[0]
    conn = FakeConnection(damaged={damaged})
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: conn)

    status = bm25_health.repair_bm25_indexes("postgresql://test", timeout_seconds=1)

    assert status["healthy"] is True
    assert conn.reindexed == [damaged]
    repaired = {item["name"]: item["repaired"] for item in status["indexes"]}
    assert repaired == {
        bm25_health.BM25_INDEXES[0]: True,
        bm25_health.BM25_INDEXES[1]: False,
    }


def test_repair_does_not_create_missing_index(monkeypatch: Any) -> None:
    missing = bm25_health.BM25_INDEXES[0]
    conn = FakeConnection(missing={missing})
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: conn)

    status = bm25_health.repair_bm25_indexes("postgresql://test", timeout_seconds=1)

    assert status["healthy"] is False
    assert conn.reindexed == []
    assert status["indexes"][0]["state"] == "missing"
    assert "setup/migrations" in status["indexes"][0]["error"]


def test_repair_lock_timeout_remains_degraded(monkeypatch: Any) -> None:
    damaged = bm25_health.BM25_INDEXES[0]
    conn = FakeConnection(damaged={damaged}, lock_available=False)
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: conn)

    status = bm25_health.repair_bm25_indexes("postgresql://test", timeout_seconds=0)

    assert status["healthy"] is False
    assert conn.reindexed == []
    assert "timed out waiting for BM25 repair lock" in status["indexes"][0]["error"]


@pytest.mark.integration
def test_hub_verification_sql_error_does_not_abort_later_checks(
    postgres_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    with database_operation_deadline(timeout_seconds=2):
        with postgres_db.transaction() as transaction:
            execute = transaction.execute

            def fail_verification(
                query: str, params: Sequence[Any] | Mapping[str, Any] = ()
            ) -> Cursor:
                if "FROM pdb.verify_index" in query:
                    return execute("SELECT 1 / 0")
                return execute(query, params)

            monkeypatch.setattr(transaction, "execute", fail_verification)
            first = bm25_health._verify_index(transaction, "pg_catalog.pg_class_oid_index")
            second = bm25_health._verify_index(transaction, "pg_catalog.qa_absent_bm25_index")

            assert first["state"] == "error"
            assert "division by zero" in first["error"]
            assert second["state"] == "missing"
            row = transaction.execute("SELECT 1 AS value").fetchone()
            assert row is not None and row["value"] == 1

    assert postgres_db.fetchone("SELECT 1 AS value") == {"value": 1}
