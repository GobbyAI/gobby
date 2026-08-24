from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# EXTERNAL_SESSION_ID would fail with `invalid input syntax for type uuid`.
EXTERNAL_SESSION_ID = "11111111-1111-4111-8111-111111111111"
PLATFORM_SESSION_ID = "22222222-2222-4222-8222-222222222222"
CLASS_INJECTION_RULE_FILES = (
    "inject-plan-enhancer-lessons.yaml",
    "inject-plan-reviewer-lessons.yaml",
    "inject-planner-lessons.yaml",
    "inject-qa-reviewer-lessons.yaml",
)


@pytest.mark.parametrize("filename", CLASS_INJECTION_RULE_FILES)
def test_class_injection_rule_source_uses_canonical_schema(
    filename: str,
    temp_db: HubDatabase,
) -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/rules/review-learning"
        / filename
    )
    source = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    assert {"review-learning", "gobby", "default"} <= set(source["tags"])
    rules = cast(dict[str, dict[str, Any]], source["rules"])
    assert len(rules) == 1
    rule_name, rule = next(iter(rules.items()))
    assert rule_name.startswith("inject-")
    assert {
        "description",
        "event",
        "enabled",
        "priority",
        "when",
        "effect",
    } <= rule.keys()
    assert rule["when"] == "True"
    assert "effects" not in rule
    assert rule["effect"]["type"] == "mcp_call"
    _sync_bundled(temp_db)
    row = RuleDefinitionManager(temp_db).get_by_name(rule_name)
    assert row is not None
    body = RuleDefinitionBody.model_validate(row.definition_json)
    assert body.when == "True"
    assert len(body.resolved_effects) == 1
    assert body.resolved_effects[0].type == "mcp_call"


def _sync_bundled(db: HubDatabase) -> None:
    sync_bundled_rules(db, get_bundled_rules_path())


def _sync_file_recall_rule(db: HubDatabase) -> None:
    _sync_bundled(db)
    with db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
            ("inject-review-lessons-for-touched-files",),
        )


def _event(data: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": PLATFORM_SESSION_ID},
    )


def _turn_start_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
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
        _sync_file_recall_rule(temp_db)
        manager = RuleDefinitionManager(temp_db)

        row = manager.get_by_name("inject-review-lessons-for-touched-files")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.event.value == "before_tool"
        assert body.group == "review-learning"
        assert body.when is not None
        assert "canonical_file_paths" in body.when
        assert "canonical_repo_mutation" in body.when
        assert len(body.resolved_effects) == 1
        effect = body.resolved_effects[0]
        assert effect.type == "mcp_call"
        assert effect.server == "gobby-review-learning"
        assert effect.tool == "recall_review_lessons_for_files"
        assert effect.background is False
        assert effect.inject_result is True

    @pytest.mark.asyncio
    async def test_broad_read_injects_compact_review_guidance(self, temp_db: HubDatabase) -> None:
        _sync_file_recall_rule(temp_db)
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
                },
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
        assert "session_id" not in calls[0]["args"]

    @pytest.mark.asyncio
    async def test_mutation_injects_before_write(self, temp_db: HubDatabase) -> None:
        _sync_file_recall_rule(temp_db)
        lesson_id = f"lesson-{uuid4()}"
        calls: list[tuple[str, str]] = []

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: HookEvent
        ) -> dict[str, Any]:
            calls.append((server, tool))
            return {"success": True, "result": {"count": 1, "lessons": [_lesson(lesson_id)]}}

        engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
        response = await engine.evaluate(
            _event(
                {
                    "tool_name": "Edit",
                    "tool_input": {
                        "file_path": "crates/gcode/src/config/services.rs",
                        "old_string": "old",
                        "new_string": "new",
                    },
                    "canonical_tool_kind": "write",
                    "canonical_file_paths": ["crates/gcode/src/config/services.rs"],
                    "canonical_repo_mutation": True,
                }
            ),
            session_id=EXTERNAL_SESSION_ID,
            variables={},
        )

        assert calls == [("gobby-review-learning", "recall_review_lessons_for_files")]
        assert response.context is not None
        assert "service-config-propagate-db-errors" in response.context

    @pytest.mark.asyncio
    async def test_unrelated_file_result_injects_nothing(self, temp_db: HubDatabase) -> None:
        _sync_file_recall_rule(temp_db)

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: HookEvent
        ) -> dict[str, Any]:
            return {"success": True, "result": {"count": 0, "lessons": [], "message": ""}}

        engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
        response = await engine.evaluate(
            _event(
                {
                    "tool_name": "Edit",
                    "tool_input": {
                        "file_path": "src/unrelated.py",
                        "old_string": "old",
                        "new_string": "new",
                    },
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
        _sync_file_recall_rule(temp_db)
        lesson_id = f"lesson-{uuid4()}"

        async def dispatcher(
            server: str, tool: str, args: dict[str, Any], event: HookEvent
        ) -> dict[str, Any]:
            return {"success": True, "result": {"count": 1, "lessons": [_lesson(lesson_id)]}}

        engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
        event = _event(
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "crates/gcode/src/config/services.rs",
                    "old_string": "old",
                    "new_string": "new",
                },
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
        _sync_file_recall_rule(temp_db)
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
    RuleDefinitionManager(temp_db).create(
        name="test-class-recall-routing",
        definition_json=body.model_dump_json(),
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


@pytest.mark.asyncio
async def test_class_injection_agent_scoping(temp_db: HubDatabase) -> None:
    _sync_bundled(temp_db)
    manager = RuleDefinitionManager(temp_db)
    expected_rules = {
        "inject-plan-reviewer-lessons": (
            ["plan-adversary", "plan-adversary-taskless"],
            "plan",
            ["reviewer-miss"],
        ),
        "inject-planner-lessons": (["planner"], "plan", ["fixer-induced-defect"]),
        "inject-plan-enhancer-lessons": (
            ["plan-enhancer", "plan-enhancer-taskless"],
            "plan",
            ["fixer-induced-defect"],
        ),
        "inject-qa-reviewer-lessons": (["qa-reviewer"], "code", ["qa-miss"]),
    }

    expected_by_agent: dict[str, tuple[str, list[str]]] = {}
    for rule_name, (agent_scope, lesson_domain, lesson_types) in expected_rules.items():
        row = manager.get_by_name(rule_name)
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event == RuleTriggerEvent.TURN_START
        assert body.group == "review-learning"
        assert body.agent_scope == agent_scope
        assert len(body.resolved_effects) == 1
        effect = body.resolved_effects[0]
        assert effect.type == "mcp_call"
        assert effect.server == "gobby-review-learning"
        assert effect.tool == "recall_review_lessons_by_class"
        assert effect.arguments == {
            "lesson_domain": lesson_domain,
            "lesson_types": lesson_types,
            "limit": 3,
        }
        assert effect.background is False
        assert effect.inject_result is True
        expected_by_agent.update(dict.fromkeys(agent_scope, (lesson_domain, lesson_types)))

    class_calls: list[dict[str, Any]] = []

    async def dispatcher(
        server: str, tool: str, args: dict[str, Any], event: HookEvent
    ) -> dict[str, Any]:
        del event
        if tool != "recall_review_lessons_by_class":
            return {"success": True, "result": {}}
        assert server == "gobby-review-learning"
        class_calls.append(args)
        if args["lesson_types"] == ["qa-miss"]:
            return {"success": True, "result": {"count": 0, "lessons": []}}
        return {
            "success": True,
            "result": {
                "count": 1,
                "lessons": [
                    {
                        "memory_id": f"class-lesson-{len(class_calls)}",
                        "pattern_id": "plan-review:reviewer-miss:correctness:stale-section",
                        "do": "Apply the matched class lesson.",
                        "avoid": "Repeating the matched review failure.",
                    }
                ],
            },
        }

    engine = RuleEngine(temp_db, mcp_dispatcher=dispatcher)
    base_variables = {
        "brevity_disabled": True,
        "restraint_disabled": True,
        "skill_discovery_instructions_shown": True,
        "memory_nudge_fired": True,
        "servers_listed": True,
    }
    for agent_type, (lesson_domain, lesson_types) in expected_by_agent.items():
        before = len(class_calls)
        response = await engine.evaluate(
            _turn_start_event(),
            session_id=EXTERNAL_SESSION_ID,
            variables={**base_variables, "_agent_type": agent_type},
        )
        assert len(class_calls) == before + 1
        assert class_calls[-1] == {
            "lesson_domain": lesson_domain,
            "lesson_types": lesson_types,
            "limit": 3,
        }
        if lesson_types == ["qa-miss"]:
            assert "<review-guidance>" not in (response.context or "")
        else:
            assert "<review-guidance>" in (response.context or "")

    for variables in (
        {**base_variables, "_agent_type": "developer"},
        base_variables,
    ):
        before = len(class_calls)
        response = await engine.evaluate(
            _turn_start_event(),
            session_id=EXTERNAL_SESSION_ID,
            variables=variables,
        )
        assert len(class_calls) == before
        assert "<review-guidance>" not in (response.context or "")
