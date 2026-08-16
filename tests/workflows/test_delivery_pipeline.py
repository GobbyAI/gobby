"""Tests for inline inject_result delivery and review-lesson deduplication."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.memory_recall_delivery import MEMORY_RECALL_DELIVERIES_VARIABLE
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.delivery_formatting import finalize_staged_memory_delivery
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


def test_generic_recall_stages_then_renders_after_review_guidance(
    engine: RuleEngine,
    db: HubDatabase,
) -> None:
    event = _event()
    event.source = SessionSource.CLAUDE
    handled, formatted = engine._format_memory_backed_result(
        server="gobby-memory",
        tool="recall_memories_for_prompt",
        result={
            "success": True,
            "recall_request_id": "request-inline",
            "origin_turn_seq": 5,
            "project_id": PROJECT_ID,
            "memories": [
                {
                    "id": "memory-inline",
                    "content": "Keep the exact coordinator boundary.",
                    "memory_type": "fact",
                    "similarity": 0.99,
                    "search_via": "hybrid",
                }
            ],
        },
        event=event,
        platform_session_id=PLATFORM_SESSION_ID,
        variables=_variables(),
    )
    response = HookResponse(context="<review-guidance>Review first.</review-guidance>")

    finalize_staged_memory_delivery(
        event, response, database=db, logger=logging.getLogger(__name__)
    )

    assert handled is True
    assert formatted is None
    assert response.context is not None
    assert response.context.index("<review-guidance>") < response.context.index("<project-memory>")
    assert "Keep the exact coordinator boundary." in response.context
    assert "similarity" not in response.context
    assert "search_via" not in response.context
    assert _vars(db, PLATFORM_SESSION_ID)["injected_memory_ids"] == ["memory-inline"]
    assert MEMORY_RECALL_DELIVERIES_VARIABLE not in _vars(db, PLATFORM_SESSION_ID)


def test_claude_overflow_queues_body_and_injects_exact_instruction(
    engine: RuleEngine,
    db: HubDatabase,
) -> None:
    event = _event()
    event.source = SessionSource.CLAUDE
    engine._format_memory_backed_result(
        server="gobby-memory",
        tool="recall_memories_for_prompt",
        result={
            "success": True,
            "recall_request_id": "request-overflow",
            "origin_turn_seq": 5,
            "project_id": PROJECT_ID,
            "memories": [
                {
                    "id": "memory-overflow",
                    "content": "x" * 1_000,
                    "memory_type": "context",
                }
            ],
        },
        event=event,
        platform_session_id=PLATFORM_SESSION_ID,
        variables=_variables(),
    )
    response = HookResponse(context="r" * 9_400)

    finalize_staged_memory_delivery(
        event, response, database=db, logger=logging.getLogger(__name__)
    )

    instruction = (
        'call_tool("gobby-memory", "get_recall_memories", {"recall_request_id":"request-overflow"})'
    )
    assert response.context is not None
    assert instruction in response.context
    assert "<project-memory>" not in response.context
    variables = _vars(db, PLATFORM_SESSION_ID)
    queued = variables[MEMORY_RECALL_DELIVERIES_VARIABLE][0]
    assert queued["status"] == "pending"
    assert queued["memories"][0]["content"] == "x" * 1_000
    assert variables.get("injected_memory_ids", []) == []


def test_grok_overflow_queues_same_budget_as_claude(
    engine: RuleEngine,
    db: HubDatabase,
) -> None:
    event = _event()
    event.source = SessionSource.GROK
    engine._format_memory_backed_result(
        server="gobby-memory",
        tool="recall_memories_for_prompt",
        result={
            "success": True,
            "recall_request_id": "request-grok-overflow",
            "origin_turn_seq": 5,
            "project_id": PROJECT_ID,
            "memories": [
                {
                    "id": "memory-grok-overflow",
                    "content": "x" * 1_000,
                    "memory_type": "context",
                }
            ],
        },
        event=event,
        platform_session_id=PLATFORM_SESSION_ID,
        variables=_variables(),
    )
    response = HookResponse(context="r" * 9_400)

    finalize_staged_memory_delivery(
        event, response, database=db, logger=logging.getLogger(__name__)
    )

    instruction = (
        'call_tool("gobby-memory", "get_recall_memories", '
        '{"recall_request_id":"request-grok-overflow"})'
    )
    assert response.context is not None
    assert instruction in response.context
    assert "<project-memory>" not in response.context
    queued = _vars(db, PLATFORM_SESSION_ID)[MEMORY_RECALL_DELIVERIES_VARIABLE][0]
    assert queued["status"] == "pending"
    assert queued["memories"][0]["content"] == "x" * 1_000


def test_grok_handoff_sized_join_queues_instead_of_inlining(
    engine: RuleEngine,
    db: HubDatabase,
) -> None:
    event = _event()
    event.source = SessionSource.GROK
    engine._format_memory_backed_result(
        server="gobby-memory",
        tool="recall_memories_for_prompt",
        result={
            "success": True,
            "recall_request_id": "request-grok-join",
            "origin_turn_seq": 5,
            "project_id": PROJECT_ID,
            "memories": [
                {
                    "id": "memory-grok-join",
                    "content": "m" * 3_600,
                    "memory_type": "context",
                }
            ],
        },
        event=event,
        platform_session_id=PLATFORM_SESSION_ID,
        variables=_variables(),
    )
    response = HookResponse(context="h" * 8_000)

    finalize_staged_memory_delivery(
        event, response, database=db, logger=logging.getLogger(__name__)
    )

    assert response.context is not None
    assert "<project-memory>" not in response.context
    assert "get_recall_memories" in response.context
    queued = _vars(db, PLATFORM_SESSION_ID)[MEMORY_RECALL_DELIVERIES_VARIABLE][0]
    assert queued["status"] == "pending"


def test_grok_raised_limit_inlines_handoff_sized_join(
    engine: RuleEngine,
    db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.workflows.engine import delivery_formatting

    monkeypatch.setattr(
        delivery_formatting, "additional_context_limit_for", lambda _provider: 20_000
    )
    monkeypatch.setattr(delivery_formatting, "inline_context_budget_for", lambda _provider: 19_550)
    event = _event()
    event.source = SessionSource.GROK
    engine._format_memory_backed_result(
        server="gobby-memory",
        tool="recall_memories_for_prompt",
        result={
            "success": True,
            "recall_request_id": "request-grok-raised",
            "origin_turn_seq": 5,
            "project_id": PROJECT_ID,
            "memories": [
                {
                    "id": "memory-grok-raised",
                    "content": "m" * 3_600,
                    "memory_type": "context",
                }
            ],
        },
        event=event,
        platform_session_id=PLATFORM_SESSION_ID,
        variables=_variables(),
    )
    response = HookResponse(context="h" * 8_000)

    finalize_staged_memory_delivery(
        event, response, database=db, logger=logging.getLogger(__name__)
    )

    assert response.context is not None
    assert "m" * 3_600 in response.context
    assert MEMORY_RECALL_DELIVERIES_VARIABLE not in _vars(db, PLATFORM_SESSION_ID)


def test_existing_over_budget_does_not_grow_past_ship_limit(
    engine: RuleEngine,
    db: HubDatabase,
) -> None:
    event = _event()
    event.source = SessionSource.GROK
    engine._format_memory_backed_result(
        server="gobby-memory",
        tool="recall_memories_for_prompt",
        result={
            "success": True,
            "recall_request_id": "request-already-full",
            "origin_turn_seq": 5,
            "project_id": PROJECT_ID,
            "memories": [
                {
                    "id": "memory-already-full",
                    "content": "x" * 500,
                    "memory_type": "context",
                }
            ],
        },
        event=event,
        platform_session_id=PLATFORM_SESSION_ID,
        variables=_variables(),
    )
    existing = "e" * 9_900
    response = HookResponse(context=existing)

    finalize_staged_memory_delivery(
        event, response, database=db, logger=logging.getLogger(__name__)
    )

    assert response.context == existing
    queued = _vars(db, PLATFORM_SESSION_ID)[MEMORY_RECALL_DELIVERIES_VARIABLE][0]
    assert queued["status"] == "pending"


@pytest.mark.asyncio
async def test_inline_recall_rule_renders_args_once_and_delivers_at_turn_start(
    db: HubDatabase,
) -> None:
    sync_bundled_rules(db, get_bundled_rules_path())
    calls: list[dict[str, Any]] = []

    async def dispatcher(
        server: str,
        tool: str,
        args: dict[str, Any],
        _event: HookEvent,
    ) -> dict[str, Any]:
        calls.append({"server": server, "tool": tool, "args": args})
        return {
            "success": True,
            "result": {
                "success": True,
                "recall_request_id": "request-turn-start",
                "origin_turn_seq": 9,
                "project_id": PROJECT_ID,
                "memories": [
                    {
                        "id": "memory-turn-start",
                        "content": "Complete inline body.",
                        "memory_type": "fact",
                    }
                ],
            },
        }

    rule_engine = RuleEngine(db, mcp_dispatcher=dispatcher)
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "Implement the complete memory recall delivery change."},
        project_id=PROJECT_ID,
        metadata={"_platform_session_id": PLATFORM_SESSION_ID},
    )
    variables = {
        "_active_rule_names": ["memory-recall-on-prompt"],
        "parent_turn_seq": 9,
        "is_spawned_agent": False,
        "project": {"id": PROJECT_ID, "path": "/tmp/project"},
    }

    response = await rule_engine.evaluate(event, PLATFORM_SESSION_ID, variables)
    finalize_staged_memory_delivery(
        event, response, database=db, logger=logging.getLogger(__name__)
    )
    duplicate = await rule_engine.evaluate(event, PLATFORM_SESSION_ID, variables)

    assert calls == [
        {
            "server": "gobby-memory",
            "tool": "recall_memories_for_prompt",
            "args": {
                "prompt": "Implement the complete memory recall delivery change.",
                "source": "claude",
                "parent_turn_seq": "9",
                "is_spawned_agent": False,
            },
        }
    ]
    assert response.context is not None
    assert "Complete inline body." in response.context
    assert "get_recall_memories" not in response.context
    assert duplicate.context is None
