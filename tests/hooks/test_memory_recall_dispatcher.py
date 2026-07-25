from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from gobby.config.sessions import MemoryRecallConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.memory_recall_delivery import (
    MAX_MEMORY_RECALL_DELIVERIES,
    MEMORY_RECALL_DELIVERIES_VARIABLE,
    MemoryRecallDeliveryQueue,
)
from gobby.hooks.memory_recall_dispatcher import MemoryRecallDispatcher
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.memories import Memory, MemoryType
from gobby.workflows.state_manager import SessionVariableManager

# sessions.id, session_variables.session_id, and projects.id are native uuid columns.
SESSION_ID = "ffffffff-0000-4000-8000-000000000001"
PROJECT_ID = "ffffffff-0000-4000-8000-000000000002"

pytestmark = pytest.mark.unit


class FakeMemoryManager:
    def __init__(self, memories: list[Memory]) -> None:
        self.memories = memories
        self.calls: list[dict[str, Any]] = []

    async def search_memories(self, **kwargs: Any) -> list[Memory]:
        self.calls.append(kwargs)
        return self.memories


class FakeLLMService:
    def __init__(self, response: dict[str, list[str]]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        caller: str | None = None,
    ) -> dict[str, list[str]]:
        self.calls.append(
            {
                "feature_config": feature_config,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "caller": caller,
            }
        )
        return self.response


def _memory(memory_id: str, content: str, similarity: Any) -> Memory:
    return Memory(
        id=memory_id,
        memory_type=MemoryType.FACT,
        content=content,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        tags=["test"],
        similarity=similarity,
        search_via="semantic",
    )


def _event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="external-hook-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "please use project memory to fix this regression today"},
        project_id=PROJECT_ID,
        metadata={"_platform_session_id": SESSION_ID},
    )


def _create_session(db: HubDatabase, session_id: str = SESSION_ID) -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, "test-project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, "
        "updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (session_id, "external-hook-session", "machine-1", "claude", PROJECT_ID),
    )


def _make_dispatcher(
    temp_db: HubDatabase,
    memory_manager: FakeMemoryManager,
    llm_service: FakeLLMService,
) -> MemoryRecallDispatcher:
    return MemoryRecallDispatcher(
        config=SimpleNamespace(
            memory_recall=MemoryRecallConfig(candidate_limit=8, selected_limit=3, min_score=0.7)
        ),
        database=temp_db,
        memory_manager=cast(Any, memory_manager),
        llm_service=cast(Any, llm_service),
        loop=None,
        logger=logging.getLogger("tests.memory_recall_dispatcher"),
    )


def test_schedule_queues_ranked_memory_references_without_mailbox_row(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_session(temp_db)
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager(
        [
            _memory("weak", "Weak memory should not render.", 0.42),
            _memory("strong", "Strong memory should render.", 0.91),
            _memory("keyword", "Keyword memory should render second.", None),
        ]
    )
    llm_service = FakeLLMService({"memory_ids": ["strong", "keyword", "weak"]})
    dispatcher = _make_dispatcher(temp_db, memory_manager, llm_service)
    scheduled: dict[str, Any] = {}

    def schedule_task(
        key: tuple[str, int],
        coro: Any,
    ) -> concurrent.futures.Future[Any]:
        scheduled["key"] = key
        scheduled["coro"] = coro
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        future.set_result(None)
        return future

    monkeypatch.setattr(dispatcher, "_schedule_task", schedule_task)

    dispatcher.schedule(_event())

    assert scheduled["key"] == (SESSION_ID, 3)
    assert memory_manager.calls == []
    assert llm_service.calls == []

    asyncio.run(scheduled["coro"])

    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    deliveries = variables[MEMORY_RECALL_DELIVERIES_VARIABLE]
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery["origin_turn_seq"] == 3
    assert delivery["project_id"] == PROJECT_ID
    assert delivery["status"] == "pending"
    UUID(delivery["recall_request_id"])
    assert delivery["references"] == [
        {
            "memory_id": "strong",
            "rank": 1,
            "similarity": 0.91,
            "search_via": "semantic",
        },
        {
            "memory_id": "keyword",
            "rank": 2,
            "search_via": "semantic",
        },
    ]
    serialized = json.dumps(delivery)
    assert "Strong memory should render." not in serialized
    assert "Keyword memory should render second." not in serialized
    assert '"tags"' not in serialized
    assert InterSessionMessageManager(temp_db).get_undelivered_messages(SESSION_ID) == []
    assert memory_manager.calls[0]["min_score"] == 0.7
    assert memory_manager.calls[0]["session_id"] == SESSION_ID
    UUID(memory_manager.calls[0]["recall_request_id"])
    assert memory_manager.calls[0]["caller"] == "memory.recall"
    assert '"strong"' in llm_service.calls[0]["prompt"]
    assert '"weak"' not in llm_service.calls[0]["prompt"]


def test_delivery_queue_upserts_duplicate_turn_and_keeps_last_sixteen(
    temp_db: HubDatabase,
) -> None:
    _create_session(temp_db)
    queue = MemoryRecallDeliveryQueue(temp_db)
    for origin_turn_seq in range(MAX_MEMORY_RECALL_DELIVERIES + 2):
        queue.queue(
            SESSION_ID,
            recall_request_id=f"request-{origin_turn_seq}",
            origin_turn_seq=origin_turn_seq,
            project_id=PROJECT_ID,
            memories=[
                {
                    "id": f"memory-{origin_turn_seq}",
                    "content": "must never be persisted",
                    "tags": ["private"],
                    "similarity": 0.8,
                    "ranking_score": origin_turn_seq / 10,
                }
            ],
        )
    queue.queue(
        SESSION_ID,
        recall_request_id="replacement-request",
        origin_turn_seq=MAX_MEMORY_RECALL_DELIVERIES + 1,
        project_id=PROJECT_ID,
        memories=[
            {
                "id": "replacement-memory",
                "content": "replacement body",
                "search_via": "keyword|semantic",
            }
        ],
    )

    deliveries = SessionVariableManager(temp_db).get_variables(SESSION_ID)[
        MEMORY_RECALL_DELIVERIES_VARIABLE
    ]
    assert len(deliveries) == MAX_MEMORY_RECALL_DELIVERIES
    assert [item["origin_turn_seq"] for item in deliveries] == list(
        range(2, MAX_MEMORY_RECALL_DELIVERIES + 2)
    )
    replacement = deliveries[-1]
    assert replacement["recall_request_id"] == "replacement-request"
    assert replacement["references"] == [
        {
            "memory_id": "replacement-memory",
            "rank": 1,
            "search_via": "keyword|semantic",
        }
    ]
    serialized = json.dumps(deliveries)
    assert "must never be persisted" not in serialized
    assert "replacement body" not in serialized
    assert '"tags"' not in serialized


def test_schedule_skips_duplicate_turn(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_session(temp_db)
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    dispatcher = _make_dispatcher(
        temp_db,
        FakeMemoryManager([]),
        FakeLLMService({"memory_ids": []}),
    )
    scheduled: list[tuple[str, int]] = []

    def schedule_task(
        key: tuple[str, int],
        coro: Any,
    ) -> concurrent.futures.Future[Any]:
        scheduled.append(key)
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        future.set_result(None)
        return future

    monkeypatch.setattr(dispatcher, "_schedule_task", schedule_task)

    dispatcher.schedule(_event())
    dispatcher.schedule(_event())

    assert scheduled == [(SESSION_ID, 3)]


def test_shutdown_uses_shared_deadline(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher = _make_dispatcher(
        temp_db,
        FakeMemoryManager([]),
        FakeLLMService({"memory_ids": []}),
    )
    timeouts: list[float] = []

    class RecordingFuture(concurrent.futures.Future[None]):
        def cancel(self) -> bool:
            return False

        def result(self, timeout: float | None = None) -> None:
            assert timeout is not None
            timeouts.append(timeout)
            raise concurrent.futures.TimeoutError

    dispatcher._tasks[("first", 1)] = RecordingFuture()
    dispatcher._tasks[("second", 2)] = RecordingFuture()
    dispatcher._tasks[("third", 3)] = RecordingFuture()
    clock = iter([100.0, 100.0, 102.0, 106.0])
    monkeypatch.setattr(
        "gobby.hooks.memory_recall_dispatcher.time.monotonic",
        lambda: next(clock),
    )

    dispatcher.shutdown()

    assert timeouts == [5.0, 3.0, 0.0]
