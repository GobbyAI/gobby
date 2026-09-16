from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from gobby.projects.fenced_vector_store import ProjectFencedVectorStore
from gobby.projects.vector_cleanup import ProjectVectorCleaner
from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager


def test_project_storage_uses_inclusive_24_hour_boundary(temp_db: HubDatabase) -> None:
    projects = LocalProjectManager(temp_db)
    exact = projects.create("exact-boundary-project")
    just_under = projects.create("just-under-boundary-project")
    projects.soft_delete(exact.id)
    projects.soft_delete(just_under.id)
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    temp_db.execute("UPDATE projects SET deleted_at = %s WHERE id = %s", (cutoff, exact.id))
    temp_db.execute(
        "UPDATE projects SET deleted_at = %s WHERE id = %s",
        (cutoff + timedelta(microseconds=1), just_under.id),
    )

    candidates = projects.list_purge_candidates(cutoff)

    assert [project.id for project in candidates] == [exact.id]


def test_cron_storage_can_park_and_remove_all_project_rows(temp_db: HubDatabase) -> None:
    project = LocalProjectManager(temp_db).create("cron-purge-project")
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id=project.id,
        name="project-job",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config={"handler": "test"},
        enabled=True,
        is_system=True,
    )

    parked = storage.disable_project_jobs(project.id)
    assert [row.id for row in parked] == [job.id]
    assert storage.get_job(job.id).enabled is False  # type: ignore[union-attr]
    assert storage.delete_project_jobs([job.id]) == 1
    assert storage.get_job(job.id) is None


@dataclass
class Collection:
    name: str


@dataclass
class Collections:
    collections: list[Collection]


class FakeVectorClient:
    def get_collections(self) -> Collections:
        return Collections(
            [
                Collection("memories@old"),
                Collection("memories@staged"),
                Collection("tool_embeddings@staged"),
                Collection("gobby_github_issues@staged"),
                Collection("unmanaged"),
            ]
        )


class FakeVectorStore:
    def __init__(self) -> None:
        self.deletes: list[tuple[dict[str, str], str]] = []
        self.id_deletes: list[tuple[list[str], str]] = []
        self.collection_deletes: list[str] = []
        self.alias_deletes: list[str] = []
        self.aliases = {"memories": "memories@old"}
        self.collections = [
            collection.name for collection in FakeVectorClient().get_collections().collections
        ]

    async def _ensure_initialized(self) -> FakeVectorClient:
        return FakeVectorClient()

    async def get_aliases(self) -> dict[str, str]:
        return dict(self.aliases)

    async def list_collection_names(self) -> list[str]:
        return list(self.collections)

    async def delete(self, *, filters: dict[str, str], collection_name: str) -> None:
        self.deletes.append((filters, collection_name))

    async def delete_many(self, ids: list[str], *, collection_name: str) -> None:
        self.id_deletes.append((ids, collection_name))

    async def delete_alias(self, alias_name: str) -> None:
        self.alias_deletes.append(alias_name)
        self.aliases.pop(alias_name, None)

    async def delete_collection(self, collection_name: str) -> None:
        self.collection_deletes.append(collection_name)
        if collection_name in self.collections:
            self.collections.remove(collection_name)


@pytest.mark.asyncio
async def test_vector_cleanup_covers_active_and_staged_physical_collections() -> None:
    vector_store = FakeVectorStore()
    cleaner = ProjectVectorCleaner(vector_store)  # type: ignore[arg-type]

    await cleaner.clear_project("project-1", ["memory-1", "memory-2"])

    assert {name for _filters, name in vector_store.deletes} == {
        "memories@old",
        "memories@staged",
        "tool_embeddings@staged",
    }
    assert {name for _ids, name in vector_store.id_deletes} == {
        "memories@old",
        "memories@staged",
        "tool_embeddings@staged",
    }
    assert all(filters == {"project_id": "project-1"} for filters, _ in vector_store.deletes)
    assert vector_store.collection_deletes == ["gobby_github_issues@staged"]
    assert "gobby_github_issues@staged" not in vector_store.collections


@pytest.mark.asyncio
async def test_shared_vector_writes_hold_project_or_global_fence() -> None:
    events: list[str] = []

    class Fence:
        @asynccontextmanager
        async def writer(self, project_id: str) -> AsyncIterator[None]:
            events.append(f"project:{project_id}:enter")
            yield
            events.append(f"project:{project_id}:exit")

        @asynccontextmanager
        async def global_writer(self) -> AsyncIterator[None]:
            events.append("global:enter")
            yield
            events.append("global:exit")

    class Inner:
        async def upsert(self, *_args: object) -> None:
            events.append("upsert")

        async def batch_upsert(self, *_args: object) -> None:
            events.append("batch-upsert")

        async def rebuild(self, *_args: object, **_kwargs: object) -> None:
            events.append("rebuild")

    store = ProjectFencedVectorStore(Inner(), Fence())  # type: ignore[arg-type]
    await store.upsert("point", [1.0], {"project_id": "project-1"})
    await store.batch_upsert(
        [
            ("point-1", [1.0], {"project_id": "project-2"}),
            ("point-2", [1.0], {"project_id": "project-1"}),
        ]
    )

    def snapshot_supplier() -> list[dict[str, str]]:
        events.append("snapshot")
        return []

    await store.rebuild_from_supplier(
        snapshot_supplier,
        lambda _text: [1.0],
    )

    assert events == [
        "project:project-1:enter",
        "upsert",
        "project:project-1:exit",
        "project:project-1:enter",
        "project:project-2:enter",
        "batch-upsert",
        "project:project-2:exit",
        "project:project-1:exit",
        "global:enter",
        "snapshot",
        "rebuild",
        "global:exit",
    ]
