from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from gobby.config.sessions import MemoryRecallConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.memories import Memory
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
        memory_type="fact",
        content=content,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
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


def _make_manager(
    temp_db: HubDatabase,
    memory_manager: FakeMemoryManager,
    llm_service: FakeLLMService,
) -> HookManager:
    def _dedup_memory_results(result: dict[str, Any], _session_id: str) -> dict[str, Any]:
        return result

    manager = HookManager.__new__(HookManager)
    manager._config = SimpleNamespace(
        memory_recall=MemoryRecallConfig(candidate_limit=8, selected_limit=3, min_score=0.7)
    )
    manager._database = temp_db
    manager._memory_manager = memory_manager
    manager._llm_service = llm_service
    manager._inter_session_msg_manager = InterSessionMessageManager(temp_db)
    manager._memory_recall_tasks = {}
    manager._memory_recall_lock = threading.Lock()
    manager._memory_recall_closing = False
    manager._loop = None
    manager.logger = logging.getLogger("tests.memory_recall_context")
    manager._dedup_memory_results = _dedup_memory_results  # type: ignore[method-assign]
    return manager


def test_turn_start_memory_recall_schedules_deferred_message(
    temp_db: HubDatabase,
) -> None:
    _create_session(temp_db)
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager(
        [
            _memory("weak", "Weak memory should not render.", 0.42),
            _memory("strong", "Strong memory should render.", 0.91),
        ]
    )
    llm_service = FakeLLMService({"memory_ids": ["weak", "strong"]})
    manager = _make_manager(temp_db, memory_manager, llm_service)
    scheduled: dict[str, Any] = {}

    def _schedule_memory_recall_task(
        key: tuple[str, int],
        coro: Any,
    ) -> concurrent.futures.Future[Any]:
        scheduled["key"] = key
        scheduled["coro"] = coro
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        future.set_result(None)
        return future

    manager._schedule_memory_recall_task = _schedule_memory_recall_task  # type: ignore[method-assign]

    context = manager._append_memory_recall_context(_event(), "existing workflow context")

    assert context == "existing workflow context"
    assert scheduled["key"] == (SESSION_ID, 3)
    assert memory_manager.calls == []
    assert llm_service.calls == []

    asyncio.run(scheduled["coro"])

    messages = InterSessionMessageManager(temp_db).get_undelivered_messages(SESSION_ID)
    assert len(messages) == 1
    assert messages[0].message_type == "memory_recall"
    assert messages[0].metadata_json is not None
    assert json.loads(messages[0].metadata_json) == json.loads(messages[0].content)
    payload = json.loads(messages[0].content)
    assert payload["type"] == "memory_recall"
    assert payload["producer"] == "daemon_memory_recall"
    assert payload["origin_turn_seq"] == 3
    UUID(payload["recall_request_id"])
    rendered = json.dumps(payload["memories"])
    assert "Strong memory should render." in rendered
    assert "Weak memory should not render." not in rendered
    assert memory_manager.calls[0]["min_score"] == 0.7
    assert memory_manager.calls[0]["session_id"] == SESSION_ID
    UUID(memory_manager.calls[0]["recall_request_id"])
    assert memory_manager.calls[0]["caller"] == "memory.recall"
    assert '"strong"' in llm_service.calls[0]["prompt"]
    assert '"weak"' not in llm_service.calls[0]["prompt"]


def test_memory_recall_context_skips_duplicate_turn_schedule(
    temp_db: HubDatabase,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    manager = _make_manager(
        temp_db,
        FakeMemoryManager([]),
        FakeLLMService({"memory_ids": []}),
    )
    scheduled: list[tuple[str, int]] = []

    def _schedule_memory_recall_task(
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

    manager._schedule_memory_recall_task = _schedule_memory_recall_task  # type: ignore[method-assign]

    first = manager._append_memory_recall_context(_event(), "existing context")
    second = manager._append_memory_recall_context(_event(), "existing context")

    assert first == "existing context"
    assert second == "existing context"
    assert scheduled == [(SESSION_ID, 3)]
