"""get_memory counts a direct fetch as access and tags it with the fetching task."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.memory.manager import MemoryManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"
UNKNOWN_SESSION_ID = "55555555-5555-4555-8555-555555555555"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@dataclass
class _World:
    db: HubDatabase
    project_id: str
    session_id: str
    memories: MemoryManager
    registry: InternalToolRegistry

    async def memory(self, content: str) -> str:
        memory = await self.memories.create_memory(content, project_id=self.project_id)
        return memory.id

    async def get(self, memory_id: str, session_id: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = await self.registry.call(
            "get_memory",
            {"memory_id": memory_id, "session_id": session_id or self.session_id},
        )
        return result

    def claim(self, title: str) -> str:
        tasks = LocalTaskManager(self.db)
        task = tasks.create_task(self.project_id, title, validation_criteria="Observable.")
        tasks.claim_task(task.id, self.session_id)
        return task.id

    def accessed(self, session_id: str | None = None) -> list[dict[str, Any]]:
        variables = SessionVariableManager(self.db).get_variables(session_id or self.session_id)
        records: list[dict[str, Any]] = variables.get("accessed_memory_ids", [])
        return records


@pytest.fixture
async def world(temp_db: HubDatabase, sample_project: dict[str, Any]) -> AsyncIterator[_World]:
    project_id = str(sample_project["id"])
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="get-memory-access",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    memories = MemoryManager(temp_db, MemoryConfig())
    registry = create_memory_registry(lambda: memories, session_manager=sessions)
    with patch(
        "gobby.utils.project_context.get_project_context",
        return_value={"id": project_id},
    ):
        yield _World(temp_db, project_id, str(session.id), memories, registry)


async def test_get_memory_records_access(world: _World) -> None:
    memory_id = await world.memory("Fetched directly")
    schema = world.registry.get_schema("get_memory")
    assert schema is not None
    assert "session_id" in schema["inputSchema"]["required"]
    assert inspect.iscoroutinefunction(world.registry._tools["get_memory"].func)

    first = await world.get(memory_id)
    second = await world.get(memory_id)

    assert first["success"] is True, first
    assert second["success"] is True, second
    # The row is read before this fetch is counted.
    assert (first["memory"]["access_count"], first["memory"]["surfaced_count"]) == (0, 0)
    assert (second["memory"]["access_count"], second["memory"]["surfaced_count"]) == (1, 0)
    stored = world.memories.get_memory(memory_id)
    assert stored is not None
    assert stored.access_count == 2
    assert stored.last_accessed_at is not None
    assert stored.surfaced_count == 0


def test_get_memory_is_not_classified_read_only(world: _World) -> None:
    # It writes access stats and a session variable; step allowlists reach it
    # through the capability-neutral set instead.
    metadata = world.registry.get_tool_metadata("get_memory")
    assert metadata is not None
    assert metadata.read_only is False


async def test_get_memory_records_accessed_id_with_claimed_task(world: _World) -> None:
    memory_id = await world.memory("Fetched under a task")
    task_id = world.claim("Fetching task")

    result = await world.get(memory_id)

    assert result["success"] is True, result
    assert world.accessed() == [{"memory_id": memory_id, "task_id": task_id}]


async def test_get_memory_untagged_without_claimed_task(world: _World) -> None:
    memory_id = await world.memory("Fetched with no task")

    result = await world.get(memory_id)

    assert result["success"] is True, result
    assert world.accessed() == [{"memory_id": memory_id, "task_id": None}]


async def test_get_memory_keeps_a_record_per_task(world: _World) -> None:
    memory_id = await world.memory("Fetched under two tasks")
    first_task = world.claim("First task")
    await world.get(memory_id)
    LocalTaskManager(world.db).release_task_claim(first_task)
    second_task = world.claim("Second task")

    await world.get(memory_id)
    await world.get(memory_id)

    assert world.accessed() == [
        {"memory_id": memory_id, "task_id": first_task},
        {"memory_id": memory_id, "task_id": second_task},
    ]


async def test_get_memory_unresolved_session_records_access_without_tracking(
    world: _World,
) -> None:
    memory_id = await world.memory("Fetched by an unknown session")

    result = await world.get(memory_id, session_id=UNKNOWN_SESSION_ID)

    assert result["success"] is True, result
    assert result["memory"]["id"] == memory_id
    stored = world.memories.get_memory(memory_id)
    assert stored is not None
    assert stored.access_count == 1
    assert world.accessed() == []


async def test_accessed_memory_ids_evict_oldest_at_cap(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_module = importlib.import_module("gobby.mcp_proxy.tools.memory")
    assert tool_module._ACCESSED_MEMORY_IDS_MAX == 1000
    monkeypatch.setattr(tool_module, "_ACCESSED_MEMORY_IDS_MAX", 2)
    memory_ids = [await world.memory(f"Capped fetch {index}") for index in range(3)]

    for memory_id in memory_ids:
        await world.get(memory_id)

    assert [record["memory_id"] for record in world.accessed()] == memory_ids[1:]
