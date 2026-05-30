"""Tests for daemon-owned memory recall selection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.config.sessions import MemoryRecallConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.memory.recall import MemoryRecallRunner, is_memory_recall_eligible
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import Memory
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SESSION_ID = "session-memory-recall"


class FakeMemoryManager:
    def __init__(self, memories: list[Memory] | None = None, error: Exception | None = None):
        self.memories = memories or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def search_memories(self, **kwargs: Any) -> list[Memory]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.memories


class FakeLLMService:
    def __init__(self, response: Any | None = None):
        self.response = {"memory_ids": []} if response is None else response
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        caller: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "feature_config": feature_config,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "caller": caller,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


class LegacyLLMService:
    def __init__(self, response: str = '{"memory_ids":["mem-1"]}'):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def call_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        *,
        caller: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "feature_config": feature_config,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "max_tokens": max_tokens,
                "caller": caller,
            }
        )
        return self.response


def _event(
    *,
    event_type: HookEventType = HookEventType.BEFORE_AGENT,
    source: SessionSource = SessionSource.CLAUDE,
    prompt: str = "please use project memory to fix this regression today",
    metadata: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> HookEvent:
    event_data = {"prompt": prompt}
    if data:
        event_data.update(data)
    return HookEvent(
        event_type=event_type,
        session_id="external-memory-recall",
        source=source,
        timestamp=datetime.now(UTC),
        data=event_data,
        project_id="project-1",
        metadata={"_platform_session_id": SESSION_ID, **(metadata or {})},
    )


def _memory(memory_id: str, content: str = "Useful project convention") -> Memory:
    return Memory(
        id=memory_id,
        memory_type="fact",
        content=content,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        tags=["test"],
        similarity=0.91,
        search_via="semantic",
    )


def _variables(**overrides: Any) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "parent_turn_seq": 3,
        "is_spawned_agent": False,
    }
    variables.update(overrides)
    return variables


@pytest.mark.parametrize(
    ("event", "variables", "expected"),
    [
        (_event(), _variables(), True),
        (_event(prompt="too short"), _variables(), False),
        (_event(source=SessionSource.PIPELINE), _variables(), False),
        (_event(event_type=HookEventType.AFTER_AGENT), _variables(), False),
        (_event(metadata={"synthetic": True}), _variables(), False),
        (_event(metadata={"_platform_session_id": ""}), _variables(), False),
        (_event(data={"actor": "daemon"}), _variables(), False),
        (
            _event(prompt="Message from Gobby daemon: New activity available."),
            _variables(),
            False,
        ),
        (_event(), _variables(is_spawned_agent=True), False),
        (_event(), _variables(parent_turn_seq=None), False),
    ],
)
def test_recall_eligibility_only_accepts_real_parent_user_turns(
    event: HookEvent,
    variables: dict[str, Any],
    expected: bool,
) -> None:
    assert is_memory_recall_eligible(event, variables, MemoryRecallConfig()) is expected


@pytest.mark.asyncio
async def test_runner_selects_memory_with_json_feature_call_and_no_child_session(
    temp_db: HubDatabase,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager(
        [
            _memory("mem-1", "Use gcode before broad source reads."),
            _memory("mem-2", "Unrelated but plausible."),
        ]
    )
    llm = FakeLLMService({"memory_ids": ["mem-1", "mem-1", "mem-2"]})
    config = MemoryRecallConfig(candidate_limit=8, selected_limit=1, min_score=0.5)
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=config,
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is not None
    assert payload.origin_turn_seq == 3
    assert payload.memories == [
        {
            "id": "mem-1",
            "content": "Use gcode before broad source reads.",
            "type": "fact",
            "created_at": "2026-01-01T00:00:00+00:00",
            "tags": ["test"],
            "similarity": 0.91,
            "search_via": "semantic",
        }
    ]
    assert memory_manager.calls == [
        {
            "query": "please use project memory to fix this regression today",
            "project_id": "project-1",
            "limit": 8,
            "min_score": 0.5,
        }
    ]
    assert llm.calls[0]["caller"] == "memory.recall"
    assert llm.calls[0]["feature_config"] is config
    assert LocalAgentRunManager(temp_db).list_by_parent(SESSION_ID) == []


@pytest.mark.asyncio
async def test_runner_filters_already_injected_duplicates_before_llm(temp_db: HubDatabase) -> None:
    sv_mgr = SessionVariableManager(temp_db)
    sv_mgr.set_variable(SESSION_ID, "parent_turn_seq", 3)
    sv_mgr.set_variable(SESSION_ID, "injected_memory_ids", ["mem-1"])
    llm = FakeLLMService({"memory_ids": ["mem-2"]})
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1"), _memory("mem-2")]),  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is not None
    assert [memory["id"] for memory in payload.memories] == ["mem-2"]
    assert '"mem-1"' not in llm.calls[0]["prompt"]
    assert '"mem-2"' in llm.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_runner_skips_legacy_feature_only_service(temp_db: HubDatabase) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    llm = LegacyLLMService()
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1")]),  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is None
    assert llm.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_manager", "llm", "expected_llm_calls"),
    [
        (FakeMemoryManager([]), FakeLLMService({"memory_ids": ["mem-1"]}), 0),
        (FakeMemoryManager([_memory("mem-1")]), FakeLLMService("not-json"), 1),
        (FakeMemoryManager([_memory("mem-1")]), FakeLLMService({"memory_ids": "mem-1"}), 1),
    ],
)
async def test_runner_safe_empty_candidates_and_invalid_json(
    temp_db: HubDatabase,
    memory_manager: FakeMemoryManager,
    llm: FakeLLMService,
    expected_llm_calls: int,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is None
    assert len(llm.calls) == expected_llm_calls


@pytest.mark.asyncio
async def test_runner_safe_when_llm_fails(temp_db: HubDatabase) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    llm = FakeLLMService()
    llm.error = RuntimeError("llm unavailable")
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1")]),  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    assert await runner.run(_event(), SESSION_ID, _variables()) is None


@pytest.mark.asyncio
async def test_runner_drops_stale_turn_result(temp_db: HubDatabase) -> None:
    sv_mgr = SessionVariableManager(temp_db)
    sv_mgr.set_variable(SESSION_ID, "parent_turn_seq", 4)
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1")]),  # type: ignore[arg-type]
        llm_service=FakeLLMService({"memory_ids": ["mem-1"]}),
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables(parent_turn_seq=3))

    assert payload is None
