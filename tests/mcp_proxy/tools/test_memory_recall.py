"""Tests for mandatory batch memory recall retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from gobby.hooks.memory_recall_delivery import (
    MEMORY_RECALL_DELIVERIES_VARIABLE,
    MemoryRecallDeliveryQueue,
)
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.memory_recall import register_memory_recall_tool
from gobby.memory.manager import MemoryManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SESSION_ID = "796c13d5-34bd-4b6a-b60c-b022df873ad2"
OTHER_SESSION_ID = "4e8c86db-b06c-41cf-8866-c2722ac87658"
PROJECT_ID = "4a0cc9e8-ab87-48c0-9c55-84831e47c510"


@dataclass
class FakeMemory:
    id: str
    content: str
    memory_type: str = "fact"
    tags: list[str] | None = None


class FakeMemoryManager:
    def __init__(self, db: HubDatabase, memories: list[FakeMemory]) -> None:
        self.db = db
        self.memories = {memory.id: memory for memory in memories}
        self.calls: list[tuple[str, str | None]] = []
        self.failure_memory_id: str | None = None

    def get_memory(self, memory_id: str, project_id: str | None = None) -> FakeMemory | None:
        self.calls.append((memory_id, project_id))
        if memory_id == self.failure_memory_id:
            raise RuntimeError("database unavailable")
        return self.memories.get(memory_id)


def _create_sessions(db: HubDatabase) -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, "memory-recall-tool-test"),
    )
    for session_id in (SESSION_ID, OTHER_SESSION_ID):
        db.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
            (
                session_id,
                f"external-{session_id}",
                "machine-1",
                "codex",
                PROJECT_ID,
            ),
        )


def _queue(
    db: HubDatabase,
    *,
    recall_request_id: str,
    origin_turn_seq: int,
    memory_ids: list[str],
) -> None:
    queued = MemoryRecallDeliveryQueue(db).queue(
        SESSION_ID,
        recall_request_id=recall_request_id,
        origin_turn_seq=origin_turn_seq,
        project_id=PROJECT_ID,
        memories=[
            {
                "id": memory_id,
                "similarity": 0.9 - (rank / 100),
                "search_via": "semantic",
            }
            for rank, memory_id in enumerate(memory_ids)
        ],
    )
    assert queued is True


def _registry(manager: FakeMemoryManager) -> InternalToolRegistry:
    registry = InternalToolRegistry("test-memory-recall")
    register_memory_recall_tool(registry, cast(MemoryManager, manager))
    return registry


@pytest.mark.asyncio
async def test_returns_all_memories_in_rank_order_and_completes_request(
    temp_db: HubDatabase,
) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        recall_request_id="request-ranked",
        origin_turn_seq=11,
        memory_ids=["memory-first", "memory-second"],
    )
    SessionVariableManager(temp_db).set_variable(
        SESSION_ID,
        "injected_memory_ids",
        ["existing-memory"],
    )
    manager = FakeMemoryManager(
        temp_db,
        [
            FakeMemory("memory-first", "First body.", tags=["first"]),
            FakeMemory("memory-second", "Second body.", memory_type="pattern", tags=[]),
        ],
    )
    registry = _registry(manager)

    with session_context_for_test(SESSION_ID):
        result = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-ranked"},
        )
        repeated = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-ranked"},
        )

    assert result == repeated
    assert result["success"] is True
    assert result["origin_turn_seq"] == 11
    assert result["missing_memory_ids"] == []
    assert result["total_content_chars"] == len("First body.Second body.")
    assert [memory["id"] for memory in result["memories"]] == [
        "memory-first",
        "memory-second",
    ]
    assert result["memories"][0] == {
        "rank": 1,
        "id": "memory-first",
        "content": "First body.",
        "type": "fact",
        "tags": ["first"],
        "similarity": 0.9,
        "search_via": "semantic",
    }
    assert manager.calls == [
        ("memory-first", PROJECT_ID),
        ("memory-second", PROJECT_ID),
        ("memory-first", PROJECT_ID),
        ("memory-second", PROJECT_ID),
    ]

    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    delivery = variables[MEMORY_RECALL_DELIVERIES_VARIABLE][0]
    assert delivery["status"] == "complete"
    assert delivery["completed_at"]
    assert variables["injected_memory_ids"] == [
        "existing-memory",
        "memory-first",
        "memory-second",
    ]


@pytest.mark.asyncio
async def test_missing_memories_are_reported_without_deadlocking(
    temp_db: HubDatabase,
) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        recall_request_id="request-missing",
        origin_turn_seq=12,
        memory_ids=["memory-present", "memory-deleted"],
    )
    manager = FakeMemoryManager(temp_db, [FakeMemory("memory-present", "Present.")])

    with session_context_for_test(SESSION_ID):
        result = await _registry(manager).call(
            "get_recall_memories",
            {"recall_request_id": "request-missing"},
        )

    assert result["success"] is True
    assert result["missing_memory_ids"] == ["memory-deleted"]
    assert [memory["id"] for memory in result["memories"]] == ["memory-present"]
    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert variables[MEMORY_RECALL_DELIVERIES_VARIABLE][0]["status"] == "complete"
    assert variables["injected_memory_ids"] == ["memory-present"]


@pytest.mark.asyncio
async def test_all_missing_memories_still_complete_request(temp_db: HubDatabase) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        recall_request_id="request-all-missing",
        origin_turn_seq=13,
        memory_ids=["memory-deleted"],
    )

    with session_context_for_test(SESSION_ID):
        result = await _registry(FakeMemoryManager(temp_db, [])).call(
            "get_recall_memories",
            {"recall_request_id": "request-all-missing"},
        )

    assert result["success"] is True
    assert result["memories"] == []
    assert result["missing_memory_ids"] == ["memory-deleted"]
    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert variables[MEMORY_RECALL_DELIVERIES_VARIABLE][0]["status"] == "complete"
    assert variables["injected_memory_ids"] == []


@pytest.mark.asyncio
async def test_retrieval_failure_leaves_request_pending_for_retry(
    temp_db: HubDatabase,
) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        recall_request_id="request-retry",
        origin_turn_seq=13,
        memory_ids=["memory-retry"],
    )
    manager = FakeMemoryManager(temp_db, [FakeMemory("memory-retry", "Retry body.")])
    manager.failure_memory_id = "memory-retry"
    registry = _registry(manager)

    with session_context_for_test(SESSION_ID):
        failed = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-retry"},
        )
        failed_variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
        manager.failure_memory_id = None
        succeeded = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-retry"},
        )

    assert failed["success"] is False
    assert "database unavailable" in failed["error"]
    assert failed_variables[MEMORY_RECALL_DELIVERIES_VARIABLE][0]["status"] == "pending"
    assert succeeded["success"] is True
    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert variables[MEMORY_RECALL_DELIVERIES_VARIABLE][0]["status"] == "complete"


@pytest.mark.asyncio
async def test_pending_requests_must_be_retrieved_oldest_first(
    temp_db: HubDatabase,
) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        recall_request_id="request-first",
        origin_turn_seq=14,
        memory_ids=["memory-first"],
    )
    _queue(
        temp_db,
        recall_request_id="request-second",
        origin_turn_seq=15,
        memory_ids=["memory-second"],
    )
    manager = FakeMemoryManager(
        temp_db,
        [
            FakeMemory("memory-first", "First."),
            FakeMemory("memory-second", "Second."),
        ],
    )
    registry = _registry(manager)

    with session_context_for_test(SESSION_ID):
        out_of_order = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-second"},
        )
        first = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-first"},
        )
        second = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-second"},
        )

    assert out_of_order["success"] is False
    assert out_of_order["expected_recall_request_id"] == "request-first"
    assert first["success"] is True
    assert second["success"] is True


@pytest.mark.asyncio
async def test_request_is_scoped_to_ambient_session(temp_db: HubDatabase) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        recall_request_id="request-session",
        origin_turn_seq=16,
        memory_ids=["memory-session"],
    )
    registry = _registry(
        FakeMemoryManager(temp_db, [FakeMemory("memory-session", "Session body.")])
    )

    with session_context_for_test(OTHER_SESSION_ID):
        wrong_session = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-session"},
        )
    no_session = await registry.call(
        "get_recall_memories",
        {"recall_request_id": "request-session"},
    )

    assert wrong_session["success"] is False
    assert "current session" in wrong_session["error"]
    assert no_session["success"] is False
    assert "ambient Gobby session" in no_session["error"]


@pytest.mark.asyncio
async def test_queue_get_failure_returns_standard_error(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_get(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(MemoryRecallDeliveryQueue, "get", fail_get)
    with session_context_for_test(SESSION_ID):
        result = await _registry(FakeMemoryManager(temp_db, [])).call(
            "get_recall_memories",
            {"recall_request_id": "request-failed"},
        )

    assert result == {
        "success": False,
        "recall_request_id": "request-failed",
        "error": "Memory retrieval failed: queue unavailable",
    }


@pytest.mark.asyncio
async def test_queue_pending_failure_returns_standard_error(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        recall_request_id="request-pending-failed",
        origin_turn_seq=1,
        memory_ids=["memory-1"],
    )

    def fail_pending(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("pending unavailable")

    monkeypatch.setattr(MemoryRecallDeliveryQueue, "pending", fail_pending)
    with session_context_for_test(SESSION_ID):
        result = await _registry(FakeMemoryManager(temp_db, [])).call(
            "get_recall_memories",
            {"recall_request_id": "request-pending-failed"},
        )

    assert result == {
        "success": False,
        "recall_request_id": "request-pending-failed",
        "error": "Memory retrieval failed: pending unavailable",
    }


def test_main_memory_registry_includes_batch_recall_tool(temp_db: HubDatabase) -> None:
    manager = FakeMemoryManager(temp_db, [])

    registry = create_memory_registry(cast(MemoryManager, manager))

    assert registry.get_tool("get_recall_memories") is not None
