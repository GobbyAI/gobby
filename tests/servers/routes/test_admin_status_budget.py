"""Integration coverage for rich-status database worker lifetime."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.errors import QueryCanceled

from gobby.cli.installers.postgres import get_postgres_status
from gobby.code_index import bm25_health
from gobby.config.persistence import DatabasesConfig, MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.servers.routes.admin import _health
from gobby.servers.routes.admin._health import _collect_status_items, _StatusCollection
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.operation_deadline import (
    DatabaseOperationDeadlineExceeded,
    database_operation_deadline,
)
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.postgres_pool import _PostgresTransaction
from gobby.storage.hub.protocol import Cursor, HubDatabase

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_memory_status_uses_manager_database_executor(
    postgres_db: HubDatabase,
) -> None:
    executor = DatabaseExecutor(max_workers=1, thread_name_prefix="status-memory-db")
    manager = MemoryManager(
        db=postgres_db,
        config=MemoryConfig(enabled=True, backend="local"),
        run_db=executor.run,
    )
    try:
        stats = await manager.get_stats(include_vector_count=False)
        assert stats["total_count"] == 0
        assert executor.stats().completed == 1
    finally:
        await manager.close()
        executor.shutdown()
        executor.join()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "failure_field"),
    [
        (DatabaseOperationDeadlineExceeded, "timed_out"),
        (QueryCanceled, "timed_out"),
        (RuntimeError, "failed"),
    ],
)
async def test_falkordb_database_deadline_preserves_identity_and_later_status_recovers(
    error_type: type[Exception],
    failure_field: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        hub_backend=None,
        databases=DatabasesConfig.model_validate(
            {"falkordb": {"host": "localhost", "port": 16379, "password": "Valid-123"}}
        ),
    )
    server = MagicMock(
        config=config,
        _running=True,
        _start_time=None,
        _daemon=None,
        mcp_manager=None,
        _internal_manager=None,
        session_manager=None,
        task_manager=None,
        skill_manager=None,
        _background_tasks=set(),
        port=60887,
        test_mode=True,
        services=SimpleNamespace(
            config=config,
            database=SimpleNamespace(dialect="test"),
            db_executor_stats=lambda: None,
        ),
    )
    server.memory_manager = SimpleNamespace(
        _vector_store=None, get_stats=AsyncMock(return_value={"total_count": 0})
    )
    server.run_db = AsyncMock(side_effect=[error_type("database deadline"), True])
    monkeypatch.setattr("gobby.cli.services.is_falkordb_healthy", AsyncMock(return_value=True))
    monkeypatch.setattr("gobby.cli.utils.get_gobby_home", lambda: tmp_path)
    monkeypatch.setattr(
        _health,
        "read_ghook_runtime_diagnostic",
        lambda: SimpleNamespace(is_degraded=False, to_dict=lambda: {}),
    )
    for collector in (
        "_collect_process_metrics",
        "_collect_pipeline_stats",
        "_collect_agent_stats",
    ):
        monkeypatch.setattr(_health, collector, AsyncMock(return_value={}))

    app = FastAPI()
    router = APIRouter(prefix="/api/admin")
    _health.register_health_routes(router, server)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/api/admin/status")
        assert first.status_code == 200
        partial = first.json()
        assert partial["status"] == "degraded"
        assert partial["status_collection"]["complete"] is False
        assert "falkordb" in partial["status_collection"][failure_field]
        assert partial["memory"]["falkordb"] == {
            "configured": True,
            "installed": None,
            "healthy": False,
            "url": "redis://localhost:16379",
        }

        second = await client.get("/api/admin/status")
        assert second.status_code == 200
        recovered = second.json()
        assert recovered["status"] == "healthy"
        assert recovered["status_collection"]["complete"] is True
        assert recovered["memory"]["falkordb"] == {
            "configured": True,
            "installed": True,
            "healthy": True,
            "url": "redis://localhost:16379",
        }

    assert server.run_db.await_count == 2
    assert all(
        call.args[0].__name__ == "is_falkordb_installed" for call in server.run_db.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "failure_field"),
    [
        (DatabaseOperationDeadlineExceeded, "timed_out"),
        (QueryCanceled, "timed_out"),
        (RuntimeError, "failed"),
    ],
)
async def test_postgres_storage_failure_marks_collection_incomplete(
    error_type: type[Exception], failure_field: str
) -> None:
    server = MagicMock()
    server.run_db = AsyncMock(side_effect=error_type("database unavailable"))

    result = await _collect_status_items(
        {"postgres": _health._get_postgres_dashboard_status(server, {"backend": "postgres"})},
        budget_seconds=0.25,
    )

    assert result.complete is False
    assert "postgres" in result.to_dict(budget_seconds=0.25)[failure_field]
    assert result.values == {}
    assert server.run_db.await_count == 1


@pytest.mark.asyncio
async def test_postgres_dashboard_uses_managed_database_executor(
    postgres_db: HubDatabase,
) -> None:
    executor = DatabaseExecutor(max_workers=1, thread_name_prefix="status-postgres-db")
    runtime_db = PostgresHubDatabase(
        postgres_db.conninfo,
        runtime_role="gobby_daemon_runtime",
    )
    try:
        status = await get_postgres_status(database=runtime_db, run_db=executor.run)
        assert status["healthy"] is True
        assert status["dsn_db"]
        assert set(status["extensions"]) == {"pg_search", "pgaudit", "pgcrypto"}
        assert isinstance(status["code_index"]["healthy"], bool)
        assert executor.stats().completed == 1
    finally:
        executor.shutdown()
        executor.join()
        runtime_db.close()


@pytest.mark.asyncio
async def test_postgres_status_retains_index_diagnostics_after_sql_error(
    postgres_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = DatabaseExecutor(max_workers=1, thread_name_prefix="status-index-error-db")
    execute = _PostgresTransaction.execute

    def fail_verification(
        transaction: _PostgresTransaction,
        query: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Cursor:
        if "FROM pdb.verify_index" in query:
            return execute(transaction, "SELECT 1 / 0")
        return execute(transaction, query, params)

    monkeypatch.setattr(_PostgresTransaction, "execute", fail_verification)
    monkeypatch.setattr(
        bm25_health,
        "_required_index_names",
        lambda _conn: ("pg_catalog.pg_class_oid_index", "pg_catalog.qa_absent_bm25_index"),
    )
    try:
        with database_operation_deadline(timeout_seconds=2):
            status = await get_postgres_status(database=postgres_db, run_db=executor.run)

        assert status["healthy"] is True
        assert set(status["extensions"]) == {"pg_search", "pgaudit", "pgcrypto"}
        assert status["code_index"]["healthy"] is False
        assert status["code_index"]["repair_command"] == bm25_health.BM25_REPAIR_COMMAND
        first, second = status["code_index"]["indexes"]
        assert first["state"] == "error"
        assert "division by zero" in first["error"]
        assert second["state"] == "missing"
        assert await executor.run(postgres_db.fetchone, "SELECT 1 AS value") == {"value": 1}
    finally:
        executor.shutdown()
        executor.join()


@pytest.mark.asyncio
async def test_repeated_status_deadlines_drain_executor_and_allow_later_work(
    postgres_db: HubDatabase,
) -> None:
    executor = DatabaseExecutor(max_workers=1, thread_name_prefix="status-budget-db")

    def slow_query() -> None:
        with postgres_db.transaction() as txn:
            txn.execute("SELECT pg_sleep(1)")

    async def slow_collector() -> None:
        await executor.run(slow_query)

    async def collect_once() -> _StatusCollection:
        return await _collect_status_items(
            {"database": slow_collector()},
            budget_seconds=0.12,
        )

    try:
        results = await asyncio.gather(*(collect_once() for _ in range(3)))
        for result in results:
            assert result.complete is False
            assert set(result.timed_out) | set(result.failed) == {"database"}

        def healthy_query() -> int:
            with postgres_db.transaction() as txn:
                row = txn.execute("SELECT 1 AS value").fetchone()
            assert row is not None
            return int(row["value"])

        assert await asyncio.wait_for(executor.run(healthy_query), timeout=0.8) == 1
        stats = executor.stats()
        assert stats.active == 0
        assert stats.queued == 0
        assert stats.completed == stats.submitted
    finally:
        executor.shutdown()
        executor.join()
