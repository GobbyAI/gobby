"""Tests for inline inject_result delivery and memory dedup formatting."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.effects import _is_empty_inject_payload
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

# Session/project id columns are native uuid in PostgreSQL; synthetic ids like
# PLATFORM_SESSION_ID would fail with `invalid input syntax for type uuid`.
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
EXTERNAL_SESSION_ID = "11111111-1111-4111-8111-111111111111"
PLATFORM_SESSION_ID = "22222222-2222-4222-8222-222222222222"
IGNORED_SESSION_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    temp_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, "delivery-pipeline"),
    )
    return temp_db


@pytest.fixture
def engine(db: HubDatabase) -> RuleEngine:
    return RuleEngine(db)


def _vars(db: HubDatabase, session_id: str) -> dict[str, Any]:
    return SessionVariableManager(db).get_variables(session_id)


def _set_injected(db: HubDatabase, session_id: str, ids: list[str]) -> None:
    SessionVariableManager(db).set_variable(session_id, "injected_memory_ids", ids)


def _memory(mid: str, *, tags: list[str] | None = None) -> dict[str, Any]:
    memory: dict[str, Any] = {"id": mid, "content": f"content-sentinel-{mid}"}
    if tags is not None:
        memory["tags"] = tags
    return memory


def _variables(**overrides: Any) -> dict[str, Any]:
    variables = {"parent_turn_seq": 5}
    variables.update(overrides)
    return variables


def _event(
    session_id: str = EXTERNAL_SESSION_ID, platform_session_id: str = PLATFORM_SESSION_ID
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Read"},
        metadata={"_platform_session_id": platform_session_id},
    )


def _insert_rule(
    manager: LocalWorkflowDefinitionManager,
    name: str,
    effect: RuleEffect,
) -> None:
    manager.create(
        name=name,
        workflow_type="rule",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[effect],
        ).model_dump_json(),
        priority=10,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_inline_memory_formatter_runs_outside_event_loop_thread(
    db: HubDatabase,
) -> None:
    async def dispatcher(
        _server: str,
        _tool: str,
        _args: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        return {"success": True, "result": {"memories": [_memory("m1")]}}

    manager = LocalWorkflowDefinitionManager(db)
    _insert_rule(
        manager,
        "off-loop-memory-formatting",
        RuleEffect(
            type="mcp_call",
            server="gobby-memory",
            tool="search_memories",
            inject_result=True,
        ),
    )
    engine = RuleEngine(db, mcp_dispatcher=dispatcher)
    loop_thread_id = threading.get_ident()
    formatter_threads: list[int] = []

    def format_result(*_args: object) -> str:
        formatter_threads.append(threading.get_ident())
        return "formatted memory"

    with patch.object(engine, "_format_search_memories_result", side_effect=format_result):
        response = await engine.evaluate(
            _event(),
            session_id=PLATFORM_SESSION_ID,
            variables={"project": {"id": PROJECT_ID, "path": "/tmp/project"}},
        )

    assert response.decision == "allow"
    assert len(formatter_threads) == 1
    assert formatter_threads[0] != loop_thread_id


def test_is_empty_inject_payload_shapes() -> None:
    assert _is_empty_inject_payload({"success": True, "messages": [], "count": 0}) is True
    assert _is_empty_inject_payload({"success": True, "memories": []}) is True
    assert _is_empty_inject_payload({"success": True, "messages": ["x"], "count": 1}) is False
    assert _is_empty_inject_payload({"success": True, "memories": [_memory("m1")]}) is False


async def _append_id(db: HubDatabase, session_id: str, mid: str) -> None:
    await asyncio.to_thread(
        SessionVariableManager(db).append_to_set_variable,
        session_id,
        "injected_memory_ids",
        [mid],
    )


@pytest.mark.asyncio
async def test_concurrent_append_race_safe(db: HubDatabase) -> None:
    await asyncio.gather(
        _append_id(db, PLATFORM_SESSION_ID, "m1"), _append_id(db, PLATFORM_SESSION_ID, "m2")
    )

    assert _vars(db, PLATFORM_SESSION_ID)["injected_memory_ids"] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_session_key_uses_platform_session_id(
    db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dispatcher(server: str, tool: str, args: dict[str, Any], event: Any) -> dict:
        return {"success": True, "result": {"memories": [_memory("m1")]}}

    manager = LocalWorkflowDefinitionManager(db)
    _insert_rule(
        manager,
        "search-memory-inline",
        RuleEffect(
            type="mcp_call",
            server="gobby-memory",
            tool="search_memories",
            arguments={"query": "q"},
            inject_result=True,
        ),
    )
    engine = RuleEngine(db, mcp_dispatcher=dispatcher)

    response = await engine.evaluate(_event(), session_id=IGNORED_SESSION_ID, variables={})

    assert response.context is not None
    assert "content-sentinel-m1" in response.context
    assert _vars(db, PLATFORM_SESSION_ID)["injected_memory_ids"] == ["m1"]
    assert "injected_memory_ids" not in _vars(db, EXTERNAL_SESSION_ID)


def test_search_memories_formatter_dedup(engine: RuleEngine, db: HubDatabase) -> None:
    first = engine._format_search_memories_result(
        {"memories": [_memory("m1")]},
        PLATFORM_SESSION_ID,
        _variables(),
    )
    assert first is not None
    assert "content-sentinel-m1" in first
    assert _vars(db, PLATFORM_SESSION_ID)["injected_memory_ids"] == ["m1"]

    second = engine._format_search_memories_result(
        {"memories": [_memory("m1"), _memory("m2")]},
        PLATFORM_SESSION_ID,
        _variables(),
    )

    assert second is not None
    assert "content-sentinel-m1" not in second
    assert "content-sentinel-m2" in second
    assert _vars(db, PLATFORM_SESSION_ID)["injected_memory_ids"] == ["m1", "m2"]
    assert (
        engine._format_search_memories_result({"memories": []}, PLATFORM_SESSION_ID, _variables())
        is None
    )


def test_search_memories_formatter_skips_review_lessons_before_tracking(
    engine: RuleEngine,
    db: HubDatabase,
) -> None:
    formatted = engine._format_search_memories_result(
        {
            "memories": [
                _memory("review-raw", tags=["review-lesson", "confirmed"]),
                _memory("m1"),
            ]
        },
        PLATFORM_SESSION_ID,
        _variables(),
    )

    assert formatted is not None
    assert "content-sentinel-review-raw" not in formatted
    assert "content-sentinel-m1" in formatted
    assert _vars(db, PLATFORM_SESSION_ID)["injected_memory_ids"] == ["m1"]


def test_review_lesson_formatter_dedup(engine: RuleEngine, db: HubDatabase) -> None:
    first = engine._format_review_lessons_result(
        {
            "lessons": [
                {
                    "memory_id": "lesson-1",
                    "pattern_id": "pattern-a",
                    "matched_file_path": "src/app.py",
                    "do": "Keep the coordinator boundary.",
                }
            ]
        },
        PLATFORM_SESSION_ID,
        _variables(),
    )

    assert first is not None
    assert "pattern-a" in first
    assert _vars(db, PLATFORM_SESSION_ID)["injected_review_lesson_ids"] == ["lesson-1"]

    second = engine._format_review_lessons_result(
        {
            "lessons": [
                {
                    "memory_id": "lesson-1",
                    "pattern_id": "pattern-a",
                    "matched_file_path": "src/app.py",
                    "do": "Keep the coordinator boundary.",
                },
                {
                    "memory_id": "lesson-2",
                    "pattern_id": "pattern-b",
                    "matched_file_path": "src/other.py",
                    "do": "Propagate read errors.",
                },
            ]
        },
        PLATFORM_SESSION_ID,
        _variables(),
    )

    assert second is not None
    assert "pattern-a" not in second
    assert "pattern-b" in second
    assert _vars(db, PLATFORM_SESSION_ID)["injected_review_lesson_ids"] == ["lesson-1", "lesson-2"]


@pytest.mark.asyncio
async def test_apply_effect_dispatch_switch_cancel_stale_helpers_no_op(
    db: HubDatabase,
) -> None:
    async def dispatcher(server: str, tool: str, args: dict[str, Any], event: Any) -> dict:
        return {"success": True, "result": {"success": True, "cancelled": 2, "count": 2}}

    manager = LocalWorkflowDefinitionManager(db)
    _insert_rule(
        manager,
        "cancel-stale-inline",
        RuleEffect(
            type="mcp_call",
            server="gobby-agents",
            tool="cancel_stale_helpers",
            arguments={},
            inject_result=True,
        ),
    )
    engine = RuleEngine(db, mcp_dispatcher=dispatcher)

    response = await engine.evaluate(_event(), session_id=IGNORED_SESSION_ID, variables={})

    assert response.context is None
    assert response.metadata.get("mcp_calls", []) == []


class TestInjectionOutcomeCapture:
    """Contract-§5 durable injection outcomes at the delivery chain (#17196)."""

    @staticmethod
    def _engine_with_recorder(
        db: HubDatabase,
    ) -> tuple[RuleEngine, list[dict[str, Any]]]:
        recorded: list[dict[str, Any]] = []
        engine = RuleEngine(db, injection_outcome_recorder=recorded.extend)
        return engine, recorded

    def test_inline_search_memories_result_records_outcomes(self, db: HubDatabase) -> None:
        engine, recorded = self._engine_with_recorder(db)

        result = engine._format_search_memories_result(
            {
                "memories": [{"id": "m1", "content": "hit", "type": "pattern"}],
                "recall_request_id": "req-inline",
                "project_id": PROJECT_ID,
            },
            PLATFORM_SESSION_ID,
            _variables(),
        )

        assert result is not None
        assert len(recorded) == 1
        assert recorded[0]["recall_request_id"] == "req-inline"
        assert recorded[0]["caller"] == "mcp_proxy.memory.search_memories"
        assert recorded[0]["outcome"] == "injected"
        assert recorded[0]["injection_position"] == 0
        assert recorded[0]["injection_group"] == "pattern"

    def test_no_rows_without_recall_request_id(self, db: HubDatabase) -> None:
        engine, recorded = self._engine_with_recorder(db)

        result = engine._format_search_memories_result(
            {"memories": [{"id": "m1", "content": "hit"}]},
            PLATFORM_SESSION_ID,
            _variables(),
        )

        assert result is not None
        assert recorded == []
