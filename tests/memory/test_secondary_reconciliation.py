"""Secondary reconciliation must not wrap embed I/O in the Postgres work fence."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import psycopg
import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.memory.services import lifecycle as lifecycle_module
from gobby.memory.services import secondary_reconciliation as recon_module
from gobby.memory.vectorstore import VectorStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories_crud import _memory_lock_key
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_VECTOR = [0.1, 0.2, 0.3, 0.4] * 384


def _manager(db: HubDatabase, embed_fn: AsyncMock, vector_store: AsyncMock) -> MemoryManager:
    return MemoryManager(
        db=db,
        config=MemoryConfig(enabled=True, backend="local"),
        vector_store=vector_store,
        embed_fn=embed_fn,
    )


@pytest.fixture
def vector_store() -> AsyncMock:
    store = AsyncMock(spec=VectorStore)
    store.upsert = AsyncMock()
    store.search = AsyncMock(return_value=[])
    store.search_by_stored_vectors = AsyncMock(return_value={})
    store.delete = AsyncMock()
    store.set_payload = AsyncMock()
    store.count = AsyncMock(return_value=0)
    store.batch_upsert = AsyncMock()
    return store


@pytest.mark.asyncio
async def test_slow_embed_converges_outside_postgres_budget(
    hub_db: HubDatabase,
    vector_store: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(lifecycle_module, "MUTATOR_RECONCILIATION_BUDGET_SECONDS", 1.1)
    ready = asyncio.Event()
    asyncio.get_running_loop().call_later(1.5, ready.set)

    async def slow_embed(_content: str) -> list[float]:
        await ready.wait()
        return list(_VECTOR)

    manager = _manager(hub_db, AsyncMock(side_effect=slow_embed), vector_store)
    with caplog.at_level(logging.WARNING):
        memory = await asyncio.wait_for(
            manager.create_memory("slow embed still indexes", project_id=PERSONAL_PROJECT_ID),
            timeout=5,
        )

    stored = manager.get_memory(memory.id)
    assert stored is not None
    assert stored.vector_needs_reindex is False
    vector_store.upsert.assert_awaited()
    assert "bounded PostgreSQL work deadline expired" not in caplog.text
    assert "Fenced secondary reconciliation failed" not in caplog.text


@pytest.mark.asyncio
async def test_hung_embed_does_not_stamp(
    hub_db: HubDatabase,
    vector_store: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recon_module, "SECONDARY_RECONCILIATION_IO_SECONDS", 0.3)
    never = asyncio.Event()

    async def hung_embed(_content: str) -> list[float]:
        await never.wait()
        return list(_VECTOR)

    manager = _manager(hub_db, AsyncMock(side_effect=hung_embed), vector_store)
    memory = await asyncio.wait_for(
        manager.create_memory("hung embed leaves reindex intent", project_id=PERSONAL_PROJECT_ID),
        timeout=3,
    )

    stored = manager.get_memory(memory.id)
    assert stored is not None
    assert stored.vector_needs_reindex is True
    vector_store.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_lock_held_during_embed(
    hub_db: HubDatabase,
    vector_store: AsyncMock,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_embed(_content: str) -> list[float]:
        started.set()
        await release.wait()
        return list(_VECTOR)

    manager = _manager(hub_db, AsyncMock(return_value=list(_VECTOR)), vector_store)
    created = await manager.create_memory("lock probe seed", project_id=PERSONAL_PROJECT_ID)
    manager._lifecycle_service._embed_fn = blocked_embed
    reconcile = asyncio.create_task(manager.reconcile_memory_indices(created.id))
    await asyncio.wait_for(started.wait(), timeout=2)

    lock_key = _memory_lock_key(created.id)
    async with await psycopg.AsyncConnection.connect(hub_db.conninfo) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            row = await cursor.fetchone()
    assert row is not None
    assert row[0] is False

    release.set()
    assert await asyncio.wait_for(reconcile, timeout=3) is True

    async with await psycopg.AsyncConnection.connect(hub_db.conninfo) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            acquired = await cursor.fetchone()
            assert acquired is not None
            assert acquired[0] is True
            await cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            await conn.commit()


@pytest.mark.asyncio
async def test_rebuild_crossrefs_slow_embed_does_not_use_bounded_db(
    hub_db: HubDatabase,
    vector_store: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle_module, "MUTATOR_RECONCILIATION_BUDGET_SECONDS", 1.1)
    ready = asyncio.Event()
    asyncio.get_running_loop().call_later(1.5, ready.set)

    async def slow_embed(_content: str) -> list[float]:
        await ready.wait()
        return list(_VECTOR)

    manager = _manager(hub_db, AsyncMock(return_value=list(_VECTOR)), vector_store)
    memory = await manager.create_memory("crossref source", project_id=PERSONAL_PROJECT_ID)
    manager._lifecycle_service._embed_fn = slow_embed
    manager._lifecycle_service._crossref_service._embed_fn = slow_embed
    manager._lifecycle_service._config.auto_crossref = True

    created = await asyncio.wait_for(
        manager.rebuild_crossrefs_for_memory(memory),
        timeout=5,
    )
    assert created == 0


@pytest.mark.asyncio
async def test_memory_row_session_sets_timeout_before_advisory_lock(
    hub_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[str] = []
    real_connect = psycopg.AsyncConnection.connect

    async def tracking_connect(*args: Any, **kwargs: Any) -> psycopg.AsyncConnection[Any]:
        recorded.append(f"connect:{kwargs.get('connect_timeout')}")
        connection = await real_connect(*args, **kwargs)
        real_execute = connection.execute

        async def tracking_execute(query: Any, *exec_args: Any, **exec_kwargs: Any) -> Any:
            recorded.append(str(query))
            return await real_execute(query, *exec_args, **exec_kwargs)

        cast(Any, connection).execute = tracking_execute
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", tracking_connect)

    async with recon_module.memory_row_session(hub_db.conninfo, str(uuid.uuid4())):
        pass

    connect_timeouts = [item for item in recorded if item.startswith("connect:")]
    assert connect_timeouts
    assert connect_timeouts[0] != "connect:None"
    timeout_idx = next(i for i, item in enumerate(recorded) if "statement_timeout" in item)
    lock_idx = next(i for i, item in enumerate(recorded) if "pg_advisory_lock" in item)
    assert timeout_idx < lock_idx


@pytest.mark.asyncio
async def test_purge_hidden_commits_before_external_deletes(
    hub_db: HubDatabase,
    vector_store: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(hub_db, AsyncMock(return_value=list(_VECTOR)), vector_store)
    memory = await manager.create_memory("hidden for purge", project_id=PERSONAL_PROJECT_ID)
    with hub_db.transaction() as txn:
        txn.execute(
            "UPDATE memories SET deleted_at = %s WHERE id = %s",
            (datetime.now(UTC), memory.id),
        )

    order: list[str] = []
    real_slice = recon_module._run_sql_slice

    async def tracking_slice[T](
        connection: psycopg.AsyncConnection[Any],
        deadline_seconds: float,
        work: Callable[[psycopg.AsyncConnection[Any]], Awaitable[T]],
    ) -> T:
        result = await real_slice(connection, deadline_seconds, work)
        order.append("sql_committed")
        async with await psycopg.AsyncConnection.connect(hub_db.conninfo) as probe:
            async with probe.cursor() as cursor:
                await cursor.execute(
                    "SELECT id FROM memories WHERE id = %s FOR UPDATE NOWAIT",
                    (memory.id,),
                )
                row = await cursor.fetchone()
        assert row is not None
        return result

    async def tracking_delete(*_args: object, **_kwargs: object) -> None:
        order.append("external_delete")

    monkeypatch.setattr(recon_module, "_run_sql_slice", tracking_slice)
    monkeypatch.setattr(recon_module, "_delete_artifacts", tracking_delete)

    await manager._lifecycle_service.purge_secondary_indices(memory.id, require_hidden=True)
    assert order == ["sql_committed", "external_delete"]


@pytest.mark.asyncio
async def test_purge_hidden_splits_supersession_budget(
    hub_db: HubDatabase,
    vector_store: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle_module, "SUPERSESSION_CLEANUP_BUDGET_SECONDS", 4.0)
    manager = _manager(hub_db, AsyncMock(return_value=list(_VECTOR)), vector_store)
    memory = await manager.create_memory("budget split", project_id=PERSONAL_PROJECT_ID)
    with hub_db.transaction() as txn:
        txn.execute(
            "UPDATE memories SET deleted_at = %s WHERE id = %s",
            (datetime.now(UTC), memory.id),
        )

    sql_deadlines: list[float] = []
    io_timeouts: list[float] = []
    real_slice = recon_module._run_sql_slice
    real_wait_for = asyncio.wait_for

    async def tracking_slice[T](
        connection: psycopg.AsyncConnection[Any],
        deadline_seconds: float,
        work: Callable[[psycopg.AsyncConnection[Any]], Awaitable[T]],
    ) -> T:
        sql_deadlines.append(deadline_seconds)
        return await real_slice(connection, deadline_seconds, work)

    async def tracking_wait_for(awaitable: Awaitable[Any], *, timeout: float | None = None) -> Any:
        if timeout is not None:
            io_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(recon_module, "_run_sql_slice", tracking_slice)
    monkeypatch.setattr(asyncio, "wait_for", tracking_wait_for)

    await manager._lifecycle_service.purge_secondary_indices(memory.id, require_hidden=True)
    assert sql_deadlines == [2.0]
    assert io_timeouts
    # The SQL slice is capped at half the budget; artifact deletes then spend
    # whatever remains of the whole budget (#20364), not a fixed half.
    assert 0 < io_timeouts[0] <= 4.0
