from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.dedup import DedupService
from gobby.memory.services.lifecycle import MemoryLifecycleService
from gobby.projects.fenced_vector_store import ProjectFencedVectorStore
from gobby.projects.write_fence import (
    ProjectWriteDrainTimeout,
    ProjectWriteFence,
    ProjectWriteRejected,
)


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
        await self.release_upsert.wait()
        self.upsert_finished.set()


def make_lifecycle_service(
    vector_store: object,
    *,
    embed_fn: object,
    dedup_service: DedupService | None = None,
    background_tasks: set[asyncio.Task[object]] | None = None,
) -> MemoryLifecycleService:
    return MemoryLifecycleService(
        config=MagicMock(),
        storage_provider=MagicMock(),
        backend_provider=MagicMock(),
        vector_store=vector_store,  # type: ignore[arg-type]
        embed_fn=embed_fn,  # type: ignore[arg-type]
        crossref_service=MagicMock(),
        dedup_service_provider=lambda: dedup_service,
        kg_service_provider=lambda: None,
        background_tasks=background_tasks if background_tasks is not None else set(),
        record_to_memory=MagicMock(),
        get_memory=MagicMock(),
        embed_and_upsert=AsyncMock(),
        vector_store_failure_logger=MagicMock(),
    )


async def wait_for_exclusive_claim(fence: ProjectWriteFence, project_id: str) -> None:
    async with fence._condition:
        await fence._condition.wait_for(lambda: project_id in fence._exclusive)


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
            await release_writer.wait()

    writer_task = asyncio.create_task(hold_writer())
    await writer_entered.wait()
    exclusive_entered = asyncio.Event()

    async def hold_exclusive() -> None:
        async with fence.exclusive("project", timeout=1.0):
            exclusive_entered.set()
            with pytest.raises(ProjectWriteRejected):
                async with fence.writer("project"):
                    pass

    exclusive_task = asyncio.create_task(hold_exclusive())
    async with fence._condition:
        await fence._condition.wait_for(lambda: "project" in fence._exclusive)
    assert not exclusive_entered.is_set()

    release_writer.set()
    await writer_task
    await exclusive_task
    assert exclusive_entered.is_set()


@pytest.mark.asyncio
async def test_global_writer_blocks_project_exclusive_until_release() -> None:
    fence = ProjectWriteFence(lambda _project_id: FakeProject())
    release_global = asyncio.Event()
    global_entered = asyncio.Event()

    async def hold_global_writer() -> None:
        async with fence.global_writer():
            global_entered.set()
            await release_global.wait()

    task = asyncio.create_task(hold_global_writer())
    await global_entered.wait()

    with pytest.raises(ProjectWriteDrainTimeout):
        async with fence.exclusive("project", timeout=0.01):
            pass

    release_global.set()
    await task
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
    await inner.upsert_started.wait()
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
    await purge_task
    assert inner.upsert_finished.is_set()
    assert exclusive_entered.is_set()


@pytest.mark.asyncio
async def test_spawned_dedup_task_holds_writer_admission_for_full_lifetime() -> None:
    project = FakeProject()
    fence = ProjectWriteFence(lambda _project_id: project)
    inner = BlockingVectorStore()
    vector_store = ProjectFencedVectorStore(inner, fence)  # type: ignore[arg-type]
    embed_started = asyncio.Event()
    release_embed = asyncio.Event()

    async def embed(_content: str) -> list[float]:
        embed_started.set()
        await release_embed.wait()
        return [0.1]

    dedup = DedupService(vector_store=vector_store, storage=MagicMock(), embed_fn=embed)
    background_tasks: set[asyncio.Task[object]] = set()
    lifecycle = make_lifecycle_service(
        vector_store,
        embed_fn=embed,
        dedup_service=dedup,
        background_tasks=background_tasks,
    )
    lifecycle.fire_background_dedup(
        content="content",
        project_id="project",
        is_global=False,
        memory_type="fact",
        tags=None,
        source_type="agent",
        source_session_id=None,
    )
    await embed_started.wait()
    project.deleted_at = datetime.now()
    exclusive_entered = asyncio.Event()

    async def purge() -> None:
        async with fence.exclusive("project", timeout=1.0):
            exclusive_entered.set()

    purge_task = asyncio.create_task(purge())
    await wait_for_exclusive_claim(fence, "project")
    assert not exclusive_entered.is_set()

    release_embed.set()
    await asyncio.gather(*background_tasks)
    await purge_task
    assert exclusive_entered.is_set()
