from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.lifecycle import MemoryLifecycleService
from gobby.projects.fenced_vector_store import ProjectFencedVectorStore
from gobby.projects.write_fence import (
    ProjectWriteDrainTimeout,
    ProjectWriteFence,
    ProjectWriteRejected,
)
from tests.projects.fence_helpers import TEST_WAIT_TIMEOUT_SECONDS, wait_for_exclusive_claim

pytestmark = pytest.mark.unit


@dataclass
class FakeProject:
    deleted_at: datetime | None = None


class BlockingVectorStore:
    def __init__(self) -> None:
        self.upsert_started = asyncio.Event()
        self.release_upsert = asyncio.Event()
        self.upsert_finished = asyncio.Event()
        self.search = AsyncMock(return_value=[])

    async def upsert(self, *_args: object, **_kwargs: object) -> None:
        self.upsert_started.set()
        await asyncio.wait_for(
            self.release_upsert.wait(),
            timeout=TEST_WAIT_TIMEOUT_SECONDS,
        )
        self.upsert_finished.set()


def make_lifecycle_service(
    vector_store: Any,
    *,
    embed_fn: Any,
    background_tasks: set[asyncio.Task[object]] | None = None,
) -> MemoryLifecycleService:
    return MemoryLifecycleService(
        config=MagicMock(),
        storage_provider=MagicMock(),
        backend_provider=MagicMock(),
        vector_store=vector_store,
        embed_fn=embed_fn,
        crossref_service=MagicMock(),
        kg_service_provider=lambda: None,
        background_tasks=background_tasks if background_tasks is not None else set(),
        record_to_memory=MagicMock(),
        get_memory=MagicMock(),
        embed_and_upsert=AsyncMock(),
        vector_store_failure_logger=MagicMock(),
    )


@pytest.mark.asyncio
async def test_writer_rejects_missing_and_soft_deleted_projects() -> None:
    projects = {
        "deleted": FakeProject(deleted_at=datetime.now()),
    }
    fence = ProjectWriteFence(projects.get)

    with pytest.raises(ProjectWriteRejected):
        async with fence.writer("missing"):
            pass

    with pytest.raises(ProjectWriteRejected):
        async with fence.writer("deleted"):
            pass


@pytest.mark.asyncio
async def test_exclusive_waits_for_admitted_writer_and_rejects_new_writers() -> None:
    projects = {"project": FakeProject()}
    fence = ProjectWriteFence(projects.get)
    writer_entered = asyncio.Event()
    release_writer = asyncio.Event()

    async def hold_writer() -> None:
        async with fence.writer("project"):
            writer_entered.set()
            await asyncio.wait_for(release_writer.wait(), timeout=TEST_WAIT_TIMEOUT_SECONDS)

    writer_task = asyncio.create_task(hold_writer())
    await asyncio.wait_for(writer_entered.wait(), timeout=TEST_WAIT_TIMEOUT_SECONDS)
    exclusive_entered = asyncio.Event()

    async def hold_exclusive() -> None:
        async with fence.exclusive("project", timeout=1.0):
            exclusive_entered.set()
            with pytest.raises(ProjectWriteRejected):
                async with fence.writer("project"):
                    pass

    exclusive_task = asyncio.create_task(hold_exclusive())
    await wait_for_exclusive_claim(fence, "project")
    assert not exclusive_entered.is_set()

    release_writer.set()
    await asyncio.wait_for(writer_task, timeout=TEST_WAIT_TIMEOUT_SECONDS)
    await asyncio.wait_for(exclusive_task, timeout=TEST_WAIT_TIMEOUT_SECONDS)
    assert exclusive_entered.is_set()


@pytest.mark.asyncio
async def test_cancelled_exclusive_drain_releases_project_claim() -> None:
    fence = ProjectWriteFence(lambda _project_id: FakeProject())
    writer_entered = asyncio.Event()
    release_writer = asyncio.Event()

    async def hold_writer() -> None:
        async with fence.writer("project"):
            writer_entered.set()
            await asyncio.wait_for(release_writer.wait(), timeout=TEST_WAIT_TIMEOUT_SECONDS)

    writer_task = asyncio.create_task(hold_writer())
    await asyncio.wait_for(writer_entered.wait(), timeout=TEST_WAIT_TIMEOUT_SECONDS)
    exclusive_task = asyncio.create_task(fence.exclusive("project", timeout=10).__aenter__())
    await wait_for_exclusive_claim(fence, "project")

    exclusive_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await exclusive_task

    async with fence.writer("project"):
        pass
    release_writer.set()
    await asyncio.wait_for(writer_task, timeout=TEST_WAIT_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_project_lookup_runs_before_condition_admission() -> None:
    lookup_called = asyncio.Event()

    def lookup(_project_id: str) -> FakeProject:
        lookup_called.set()
        return FakeProject()

    fence = ProjectWriteFence(lookup)

    async def write() -> None:
        async with fence.writer("project"):
            pass

    async with fence._condition:
        writer_task = asyncio.create_task(write())
        await asyncio.wait_for(lookup_called.wait(), timeout=TEST_WAIT_TIMEOUT_SECONDS)
        assert not writer_task.done()
    await asyncio.wait_for(writer_task, timeout=TEST_WAIT_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_global_writer_blocks_project_exclusive_until_release() -> None:
    fence = ProjectWriteFence(lambda _project_id: FakeProject())
    release_global = asyncio.Event()
    global_entered = asyncio.Event()

    async def hold_global_writer() -> None:
        async with fence.global_writer():
            global_entered.set()
            await asyncio.wait_for(release_global.wait(), timeout=TEST_WAIT_TIMEOUT_SECONDS)

    task = asyncio.create_task(hold_global_writer())
    await asyncio.wait_for(global_entered.wait(), timeout=TEST_WAIT_TIMEOUT_SECONDS)

    with pytest.raises(ProjectWriteDrainTimeout):
        async with fence.exclusive("project", timeout=0.01):
            pass

    release_global.set()
    await asyncio.wait_for(task, timeout=TEST_WAIT_TIMEOUT_SECONDS)
    async with fence.exclusive("project", timeout=0.1):
        pass


@pytest.mark.asyncio
async def test_global_writer_is_rejected_when_purge_already_owns_exclusive() -> None:
    fence = ProjectWriteFence(lambda _project_id: FakeProject())

    async with fence.exclusive("project", timeout=0.1):
        with pytest.raises(ProjectWriteRejected, match="purge"):
            async with fence.global_writer():
                pass


@pytest.mark.asyncio
async def test_memory_writer_finishes_after_purge_claims_exclusive() -> None:
    project = FakeProject()
    fence = ProjectWriteFence(lambda _project_id: project)
    inner = BlockingVectorStore()
    vector_store = ProjectFencedVectorStore(inner, fence)  # type: ignore[arg-type]
    service = make_lifecycle_service(vector_store, embed_fn=AsyncMock(return_value=[0.1]))

    write_task = asyncio.create_task(
        service.embed_and_upsert("memory-1", "content", {"project_id": "project"})
    )
    await asyncio.wait_for(inner.upsert_started.wait(), timeout=TEST_WAIT_TIMEOUT_SECONDS)
    project.deleted_at = datetime.now()
    exclusive_entered = asyncio.Event()

    async def purge() -> None:
        async with fence.exclusive("project", timeout=1.0):
            exclusive_entered.set()

    purge_task = asyncio.create_task(purge())
    await wait_for_exclusive_claim(fence, "project")
    assert not exclusive_entered.is_set()

    inner.release_upsert.set()
    assert await write_task is True
    await asyncio.wait_for(purge_task, timeout=TEST_WAIT_TIMEOUT_SECONDS)
    assert inner.upsert_finished.is_set()
    assert exclusive_entered.is_set()
