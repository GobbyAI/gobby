from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# EXTERNAL_SESSION_ID would fail with `invalid input syntax for type uuid`.
EXTERNAL_SESSION_ID = "11111111-1111-4111-8111-111111111111"
PLATFORM_SESSION_ID = "22222222-2222-4222-8222-222222222222"


def _sync_bundled(db: HubDatabase) -> None:
    sync_bundled_rules(db, get_bundled_rules_path())


def _event(data: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": PLATFORM_SESSION_ID},
    )


def _lesson(memory_id: str = "lesson-1") -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "pattern_id": "service-config-propagate-db-errors",
        "matched_file_path": "crates/gcode/src/config/services.rs",
        "do": "Propagate real DB read failures.",
        "avoid": "Collapsing DB read failures into None.",
    }


class TestReviewLearningRule:
    def test_rule_structure(self, temp_db: HubDatabase) -> None:
        _sync_bundled(temp_db)
        manager = LocalWorkflowDefinitionManager(temp_db)

        row = manager.get_by_name("inject-review-lessons-for-touched-files")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.event.value == "before_tool"
        assert body.group == "review-learning"
        assert body.when is not None
        assert "canonical_file_paths" in body.when
        assert "canonical_repo_mutation" in body.when
        effect = body.resolved_effects[0]
        assert effect.type == "mcp_call"
        assert effect.server == "gobby-review-learning"
        assert effect.tool == "recall_review_lessons_for_files"
        assert effect.background is False
        assert effect.inject_result is True

    @pytest.mark.asyncio
    async def test_broad_read_injects_compact_review_guidance(self, temp_db: HubDatabase) -> None:
        _sync_bundled(temp_db)
        calls: list[dict[str, Any]] = []

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: HookEvent
        ) -> dict[str, Any]:
            calls.append({"server": server, "tool": tool, "args": args, "event": event})
            return {"success": True, "result": {"count": 1, "lessons": [_lesson()]}}

        engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
        response = await engine.evaluate(
            _event(
                {
                    "tool_name": "Bash",
                    "canonical_tool_kind": "read",
                    "canonical_file_paths": ["crates/gcode/src/config/services.rs"],
                    "canonical_code_navigation_broad": True,
                }
            ),
            session_id=EXTERNAL_SESSION_ID,
            variables={},
        )

        assert response.context is not None
        assert "<review-guidance>" in response.context
        assert "Do: Propagate real DB read failures" in response.context
        assert calls[0]["server"] == "gobby-review-learning"
        assert calls[0]["tool"] == "recall_review_lessons_for_files"
        assert json.loads(calls[0]["args"]["file_paths_json"]) == [
            "crates/gcode/src/config/services.rs"
        ]
        assert calls[0]["args"]["session_id"] == PLATFORM_SESSION_ID

    @pytest.mark.asyncio
    async def test_mutation_injects_before_write(self, temp_db: HubDatabase) -> None:
        _sync_bundled(temp_db)

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: HookEvent
        ) -> dict[str, Any]:
            return {"success": True, "result": {"count": 1, "lessons": [_lesson()]}}

        engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
        response = await engine.evaluate(
            _event(
                {
                    "tool_name": "Edit",
                    "canonical_tool_kind": "write",
                    "canonical_file_paths": ["crates/gcode/src/config/services.rs"],
                    "canonical_repo_mutation": True,
                }
            ),
            session_id=EXTERNAL_SESSION_ID,
            variables={},
        )

        assert response.context is not None
        assert "service-config-propagate-db-errors" in response.context

    @pytest.mark.asyncio
    async def test_unrelated_file_result_injects_nothing(self, temp_db: HubDatabase) -> None:
        _sync_bundled(temp_db)

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: HookEvent
        ) -> dict[str, Any]:
            return {"success": True, "result": {"count": 0, "lessons": [], "message": ""}}

        engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
        response = await engine.evaluate(
            _event(
                {
                    "tool_name": "Edit",
                    "canonical_tool_kind": "write",
                    "canonical_file_paths": ["src/unrelated.py"],
                    "canonical_repo_mutation": True,
                }
            ),
            session_id=EXTERNAL_SESSION_ID,
            variables={},
        )

        assert response.context is None

    @pytest.mark.asyncio
    async def test_repeated_access_does_not_spam(self, temp_db: HubDatabase) -> None:
        _sync_bundled(temp_db)

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: HookEvent
        ) -> dict[str, Any]:
            return {"success": True, "result": {"count": 1, "lessons": [_lesson()]}}

        engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
        event = _event(
            {
                "tool_name": "Edit",
                "canonical_tool_kind": "write",
                "canonical_file_paths": ["crates/gcode/src/config/services.rs"],
                "canonical_repo_mutation": True,
            }
        )

        first = await engine.evaluate(event, session_id=EXTERNAL_SESSION_ID, variables={})
        second = await engine.evaluate(event, session_id=EXTERNAL_SESSION_ID, variables={})

        assert first.context is not None
        assert second.context is None

    @pytest.mark.asyncio
    async def test_narrow_read_does_not_trigger(self, temp_db: HubDatabase) -> None:
        _sync_bundled(temp_db)
        calls = 0

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: HookEvent
        ) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {"success": True, "result": {"count": 1, "lessons": [_lesson()]}}

        engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
        response = await engine.evaluate(
            _event(
                {
                    "tool_name": "Bash",
                    "canonical_tool_kind": "read",
                    "canonical_file_paths": ["crates/gcode/src/config/services.rs"],
                    "canonical_code_navigation_broad": False,
                    "canonical_narrow_source_context": True,
                }
            ),
            session_id=EXTERNAL_SESSION_ID,
            variables={},
        )

        assert response.context is None
        assert calls == 0


@pytest.mark.asyncio
async def test_class_recall_formatter_routing(temp_db: HubDatabase) -> None:
    body = RuleDefinitionBody(
        event=RuleTriggerEvent.BEFORE_TOOL,
        effects=[
            RuleEffect(
                type="mcp_call",
                server="gobby-review-learning",
                tool="recall_review_lessons_by_class",
                arguments={
                    "lesson_domain": "plan",
                    "lesson_types": ["missing-section"],
                },
                inject_result=True,
            )
        ],
    )
    LocalWorkflowDefinitionManager(temp_db).create(
        name="test-class-recall-routing",
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        enabled=True,
    )

    async def dispatcher(
        server: str, tool: str, args: dict[str, Any], event: HookEvent
    ) -> dict[str, Any]:
        del server, tool, args, event
        return {
            "success": True,
            "result": {
                "count": 1,
                "lessons": [
                    {
                        "memory_id": "class-routing-lesson",
                        "pattern_id": "plan-review:missing-section:correctness:stale-section",
                        "do": "Check every required plan section.",
                        "avoid": "Leaving stale sections unreviewed.",
                    }
                ],
                "message": "ignored upstream rendering",
            },
        }

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
    first = await engine.evaluate(_event({"tool_name": "Read"}), EXTERNAL_SESSION_ID, {})
    second = await engine.evaluate(_event({"tool_name": "Read"}), EXTERNAL_SESSION_ID, {})

    assert first.context is not None
    assert "<review-guidance>" in first.context
    assert "matched lesson class" in first.context
    assert "Check every required plan section" in first.context
    assert second.context is None
