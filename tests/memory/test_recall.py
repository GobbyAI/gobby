"""Tests for substantive parent-prompt memory recall."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.config.feature_base import FeatureProfile
from gobby.config.sessions import MemoryRecallConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.memory.generation_schemas import RECALL_CLASSIFICATION_SCHEMA
from gobby.memory.recall import (
    MAX_QUERY_CHARS,
    MAX_QUERY_TERMS,
    MemoryRecallRunner,
    scrub_memory_recall_query,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import Memory
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

PROJECT_ID = "44444444-4444-4444-8444-444444444444"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
EXTERNAL_SESSION_ID = "external-memory-recall"


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
    def __init__(self, response: Any):
        self.response = response
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        json_schema: dict[str, Any],
        caller: str | None = None,
        total_timeout_seconds: float | None = None,
    ) -> Any:
        self.calls.append(
            {
                "feature_config": feature_config,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "json_schema": json_schema,
                "caller": caller,
                "total_timeout_seconds": total_timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


def _event(
    prompt: str = "Implement the parser fix and add focused tests for the failing path.",
    *,
    event_type: HookEventType = HookEventType.BEFORE_AGENT,
    source: SessionSource = SessionSource.CLAUDE,
    metadata: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> HookEvent:
    event_data = {"prompt": prompt, **(data or {})}
    return HookEvent(
        event_type=event_type,
        session_id=EXTERNAL_SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=event_data,
        project_id=PROJECT_ID,
        metadata={"_platform_session_id": SESSION_ID, **(metadata or {})},
    )


def _memory(
    memory_id: str,
    content: str = "Useful project convention.",
    *,
    similarity: float | None = 0.91,
    tags: list[str] | None = None,
) -> Memory:
    return Memory(
        id=memory_id,
        memory_type="fact",
        content=content,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        project_id=PROJECT_ID,
        tags=tags or ["test"],
        similarity=similarity,
        search_via="hybrid",
    )


def _variables(**overrides: Any) -> dict[str, Any]:
    return {"parent_turn_seq": 3, "is_spawned_agent": False, **overrides}


def _runner(
    db: HubDatabase,
    manager: FakeMemoryManager,
    llm: FakeLLMService | None,
) -> MemoryRecallRunner:
    return MemoryRecallRunner(
        db=db,
        memory_manager=manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )


@pytest.fixture
def persisted_session(temp_db: HubDatabase) -> None:
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (PROJECT_ID, "memory-recall-test"),
    )
    temp_db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (
            SESSION_ID,
            EXTERNAL_SESSION_ID,
            "21000000-0000-4000-8000-000000000002",
            SessionSource.CLAUDE.value,
            PROJECT_ID,
        ),
    )


@pytest.mark.parametrize(
    ("event", "variables"),
    [
        (_event("ok"), _variables()),
        (_event("approved"), _variables()),
        (_event("continue"), _variables()),
        (_event("what is the status?"), _variables()),
        (_event("are you done?"), _variables()),
        (_event("status update please"), _variables()),
        (_event("wait"), _variables()),
        (_event("load the python skill"), _variables()),
        (_event("compact this session"), _variables()),
        (_event("/gobby help"), _variables()),
        (_event(metadata={"synthetic": True}), _variables()),
        (_event(), _variables(is_spawned_agent=True)),
        (_event(source=SessionSource.PIPELINE), _variables()),
        (_event(event_type=HookEventType.AFTER_AGENT), _variables()),
    ],
)
@pytest.mark.asyncio
async def test_hard_skips_without_classifier_or_search(
    temp_db: HubDatabase,
    event: HookEvent,
    variables: dict[str, Any],
) -> None:
    manager = FakeMemoryManager([_memory("m1")])
    llm = FakeLLMService({"substantive": True, "reason": "task"})

    result = await _runner(temp_db, manager, llm).run(event, SESSION_ID, variables)

    assert result is None
    assert llm.calls == []
    assert manager.calls == []


@pytest.mark.asyncio
async def test_classifier_approval_runs_one_low_feature_and_one_hybrid_search(
    temp_db: HubDatabase,
) -> None:
    manager = FakeMemoryManager([_memory(f"m{index}") for index in range(1, 6)])
    llm = FakeLLMService({"substantive": True, "reason": "task"})

    result = await _runner(temp_db, manager, llm).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == [
        "m1",
        "m2",
        "m3",
    ]
    assert len(llm.calls) == 1
    assert llm.calls[0]["caller"] == "memory.recall.classify"
    assert llm.calls[0]["json_schema"] == RECALL_CLASSIFICATION_SCHEMA
    assert llm.calls[0]["feature_config"].profile == FeatureProfile.LOW
    assert len(manager.calls) == 1
    assert manager.calls[0]["caller"] == "memory.recall"
    assert manager.calls[0]["tags_none"] == ["review_lesson"]


@pytest.mark.asyncio
async def test_classifier_rejection_does_not_search(temp_db: HubDatabase) -> None:
    manager = FakeMemoryManager([_memory("m1")])
    llm = FakeLLMService({"substantive": False, "reason": "conversational"})

    result = await _runner(temp_db, manager, llm).run(_event(), SESSION_ID, _variables())

    assert result is None
    assert len(llm.calls) == 1
    assert llm.calls[0]["total_timeout_seconds"] == 60
    assert manager.calls == []


@pytest.mark.asyncio
async def test_classifier_boolean_wins_when_true_reason_is_non_substantive(
    temp_db: HubDatabase,
) -> None:
    manager = FakeMemoryManager([_memory("m1")])
    llm = FakeLLMService({"substantive": True, "reason": "status_question"})

    result = await _runner(temp_db, manager, llm).run(
        _event("Which lane?"),
        SESSION_ID,
        _variables(),
    )

    assert result is not None
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_classifier_boolean_wins_when_false_reason_is_substantive(
    temp_db: HubDatabase,
) -> None:
    manager = FakeMemoryManager([_memory("m1")])
    llm = FakeLLMService({"substantive": False, "reason": "technical_question"})
    prompt = (
        "Please inspect this complicated regression and implement the complete focused "
        "durable repair today."
    )

    result = await _runner(temp_db, manager, llm).run(
        _event(prompt),
        SESSION_ID,
        _variables(),
    )

    assert result is None
    assert manager.calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"substantive": "yes", "reason": "task"},
        {"substantive": True, "reason": "unknown"},
        {"substantive": True},
    ],
)
@pytest.mark.asyncio
async def test_malformed_classifier_uses_eight_token_fallback(
    temp_db: HubDatabase,
    response: Any,
) -> None:
    manager = FakeMemoryManager([_memory("m1")])
    llm = FakeLLMService(response)
    prompt = (
        "Please inspect this complicated regression and implement the complete focused "
        "durable repair today."
    )

    result = await _runner(temp_db, manager, llm).run(_event(prompt), SESSION_ID, _variables())

    assert result is not None
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_classifier_failure_uses_technical_action_fallback(temp_db: HubDatabase) -> None:
    manager = FakeMemoryManager([_memory("m1")])
    llm = FakeLLMService({"substantive": True, "reason": "task"})
    llm.error = RuntimeError("offline")

    result = await _runner(temp_db, manager, llm).run(
        _event("Fix src/gobby/memory/recall.py"), SESSION_ID, _variables()
    )

    assert result is not None
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_classifier_timeout_uses_heuristic_fallback(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError

    monkeypatch.setattr("gobby.memory.recall.asyncio.wait_for", timeout)
    manager = FakeMemoryManager([_memory("m1")])
    llm = FakeLLMService({"substantive": True, "reason": "task"})

    result = await _runner(temp_db, manager, llm).run(
        _event("Why does ParserError happen?"), SESSION_ID, _variables()
    )

    assert result is not None
    assert len(manager.calls) == 1


def test_scrubber_considers_full_prompt_and_preserves_technical_tail() -> None:
    prompt = " ".join(f"ordinary{index}" for index in range(160))
    prompt += " investigate src/gobby/memory/recall.py ParserError --strict final_marker"

    query = scrub_memory_recall_query(prompt)

    assert len(query) <= MAX_QUERY_CHARS
    assert len(query.split()) <= MAX_QUERY_TERMS
    assert "src/gobby/memory/recall.py" in query
    assert "ParserError" in query
    assert "--strict" in query
    assert "final_marker" in query


@pytest.mark.asyncio
async def test_filters_review_duplicates_and_injected_ids_in_rank_order(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "injected_memory_ids", ["m2"])
    manager = FakeMemoryManager(
        [
            _memory("m1"),
            _memory("m1"),
            _memory("review", tags=["review_lesson"]),
            _memory("m2"),
            _memory("m3", similarity=None),
        ]
    )
    llm = FakeLLMService({"substantive": True, "reason": "technical_question"})

    result = await _runner(temp_db, manager, llm).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == [
        "m1",
        "m3",
    ]


@pytest.mark.asyncio
async def test_candidates_below_min_score_floor_are_dropped(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """The default floor (p10 of the logged distribution) trims low-signal hits."""
    floor = MemoryRecallConfig().min_score
    assert floor == 0.45
    manager = FakeMemoryManager(
        [
            _memory("strong", similarity=0.6),
            _memory("weak", similarity=floor - 0.01),
            _memory("unscored", similarity=None),
        ]
    )
    llm = FakeLLMService({"substantive": True, "reason": "technical_question"})

    result = await _runner(temp_db, manager, llm).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["strong", "unscored"]


@pytest.mark.asyncio
async def test_search_failure_allows_turn_to_continue(temp_db: HubDatabase) -> None:
    manager = FakeMemoryManager(error=RuntimeError("search unavailable"))
    llm = FakeLLMService({"substantive": True, "reason": "task"})

    result = await _runner(temp_db, manager, llm).run(_event(), SESSION_ID, _variables())

    assert result is None
    assert len(llm.calls) == 1
    assert len(manager.calls) == 1
