from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from gobby.config.sessions import MemoryRecallConfig
from gobby.hooks.dispatchers.mcp import PROJECT_MEMORY_CLOSE_TAG, PROJECT_MEMORY_OPEN_TAG
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import Memory
from gobby.workflows.state_manager import SessionVariableManager

SESSION_ID = "hook-memory-recall-session"


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
        project_id="project-1",
        metadata={"_platform_session_id": SESSION_ID},
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
    manager._loop = None
    manager.logger = logging.getLogger("tests.memory_recall_context")
    manager._dedup_memory_results = _dedup_memory_results  # type: ignore[method-assign]
    return manager


def test_turn_start_memory_recall_context_excludes_low_score_candidates(
    temp_db: HubDatabase,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager(
        [
            _memory("weak", "Weak memory should not render.", 0.42),
            _memory("strong", "Strong memory should render.", 0.91),
        ]
    )
    llm_service = FakeLLMService({"memory_ids": ["weak", "strong"]})
    manager = _make_manager(temp_db, memory_manager, llm_service)

    context = manager._append_memory_recall_context(_event(), None)

    assert context is not None
    assert PROJECT_MEMORY_OPEN_TAG in context
    assert PROJECT_MEMORY_CLOSE_TAG in context
    assert "Strong memory should render." in context
    assert "Weak memory should not render." not in context
    assert memory_manager.calls[0]["min_score"] == 0.7
    assert memory_manager.calls[0]["session_id"] == SESSION_ID
    UUID(memory_manager.calls[0]["recall_request_id"])
    assert memory_manager.calls[0]["caller"] == "memory.recall"
    assert '"strong"' in llm_service.calls[0]["prompt"]
    assert '"weak"' not in llm_service.calls[0]["prompt"]
