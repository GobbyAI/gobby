"""Secondary reconciliation must not wrap embed I/O in the Postgres work fence."""

from __future__ import annotations

import asyncio
import logging
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

pytestmark = pytest.mark.unit

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

    async def slow_embed(_content: str) -> list[float]:
        await asyncio.sleep(1.5)
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

    async def slow_embed(_content: str) -> list[float]:
        await asyncio.sleep(1.5)
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
