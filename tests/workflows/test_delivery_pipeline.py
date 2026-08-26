"""Tests for inline inject_result delivery and review-lesson deduplication."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.effects import _is_empty_inject_payload
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

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
    manager: RuleDefinitionManager,
    name: str,
    effect: RuleEffect,
) -> None:
    manager.create(
        name=name,
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[effect],
        ).model_dump_json(),
        priority=10,
        enabled=True,
    )


def test_is_empty_inject_payload_shapes() -> None:
    assert _is_empty_inject_payload({"success": True, "messages": [], "count": 0}) is True
    assert _is_empty_inject_payload({"success": True, "memories": []}) is True
    assert _is_empty_inject_payload({"success": True, "messages": ["x"], "count": 1}) is False
    assert (
        _is_empty_inject_payload(
            {"success": True, "memories": [{"id": "21000000-0000-4000-8000-000000000005"}]}
        )
        is False
    )


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
async def test_review_lessons_path_survives_orphan_removal(db: HubDatabase) -> None:
    sync_bundled_rules(db, get_bundled_rules_path())
    calls: list[tuple[str, str]] = []

    async def dispatcher(
        server: str,
        tool: str,
        _args: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        calls.append((server, tool))
        return {
            "success": True,
            "result": {
                "count": 1,
                "lessons": [
                    {
                        "memory_id": "lesson-survivor",
                        "pattern_id": "preserve-live-review-lessons",
                        "matched_file_path": "src/gobby/workflows/engine/effects.py",
                        "do": "Keep review-lesson guidance on the live delivery path.",
                    }
                ],
            },
        }

    rule_engine = RuleEngine(db, mcp_dispatcher=dispatcher)
    event = _event()
    event.data.update(
        {
            "canonical_tool_kind": "write",
            "canonical_file_paths": ["src/gobby/workflows/engine/effects.py"],
            "canonical_repo_mutation": True,
        }
    )

    response = await rule_engine.evaluate(
        event,
        session_id=PLATFORM_SESSION_ID,
        variables={
            "_active_rule_names": ["inject-review-lessons-for-touched-files"],
            "project": {"id": PROJECT_ID, "path": "/tmp/project"},
        },
    )

    assert calls == [("gobby-review-learning", "recall_review_lessons_for_files")]
    assert response.context is not None
    assert "<review-guidance>" in response.context
    assert "Keep review-lesson guidance on the live delivery path" in response.context
    assert _vars(db, PLATFORM_SESSION_ID)["injected_review_lesson_ids"] == ["lesson-survivor"]
    assert not hasattr(rule_engine, "_format_search_memories_result")
    assert not hasattr(rule_engine, "_injection_outcome_recorder")


@pytest.mark.asyncio
async def test_apply_effect_dispatch_switch_cancel_stale_helpers_no_op(
    db: HubDatabase,
) -> None:
    async def dispatcher(server: str, tool: str, args: dict[str, Any], event: Any) -> dict:
        return {"success": True, "result": {"success": True, "cancelled": 2, "count": 2}}

    manager = RuleDefinitionManager(db)
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
