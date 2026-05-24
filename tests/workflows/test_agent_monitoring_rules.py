"""Tests for build-coordinator skill guidance rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def manager(temp_db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(temp_db)


def _sync_bundled(db: HubDatabase) -> None:
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")


def _event(data: dict[str, object]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )


class TestRemovedBuildCoordinatorMonitoringSkillRule:
    def test_removed_rule_is_not_synced(self, temp_db, manager) -> None:
        manager.create(
            name="require-build-coordinator-monitoring-skill",
            workflow_type="rule",
            definition_json=json.dumps(
                {
                    "event": "before_tool",
                    "effects": [
                        {
                            "type": "block",
                            "reason": (
                                'Call get_skill(name="build-coordinator") on '
                                "gobby-skills, then continue."
                            ),
                        }
                    ],
                }
            ),
            source="installed",
            tags=["gobby"],
        )

        _sync_bundled(temp_db)

        assert manager.get_by_name("require-build-coordinator-monitoring-skill") is None

    @pytest.mark.asyncio
    async def test_generic_monitoring_inspection_is_allowed_without_build_coordinator(
        self, temp_db
    ) -> None:
        _sync_bundled(temp_db)

        event = _event(
            {
                "tool_name": "mcp__gobby__get_tool_schema",
                "mcp_tool": "get_tool_schema",
                "tool_input": {
                    "server_name": "gobby-sessions",
                    "tool_name": "get_session",
                },
            },
        )

        response = await RuleEngine(temp_db).evaluate(event, session_id="sid", variables={})

        assert response.decision == "allow"


class TestRequireBuildCoordinatorForGobbyBuild:
    def test_rule_structure(self, temp_db, manager) -> None:
        _sync_bundled(temp_db)
        row = manager.get_by_name("require-build-coordinator-for-gobby-build")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('build-coordinator')" in body.when
        assert "is_gobby_build_command" in body.when
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert (
            body.effects[0].reason
            == 'Call get_skill(name="build-coordinator") on gobby-skills, then continue.'
        )

    @pytest.mark.asyncio
    async def test_blocks_gobby_build_before_skill_load(self, temp_db) -> None:
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "Bash",
                "canonical_tool_kind": "execute",
                "tool_input": {"command": "uv run --frozen gobby build #15117 --clone"},
            }
        )

        response = await RuleEngine(temp_db).evaluate(event, session_id="sid", variables={})

        assert response.decision == "block"
        assert response.reason is not None
        assert "require-build-coordinator-for-gobby-build" in response.reason
        assert 'Call get_skill(name="build-coordinator") on gobby-skills' in response.reason

    @pytest.mark.asyncio
    async def test_allows_gobby_build_after_skill_load(self, temp_db) -> None:
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "Bash",
                "canonical_tool_kind": "execute",
                "tool_input": {"command": "gobby build #15117"},
            }
        )

        response = await RuleEngine(temp_db).evaluate(
            event,
            session_id="sid",
            variables={"loaded_skills": ["build-coordinator"]},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_allows_commands_that_only_mention_gobby_build(self, temp_db) -> None:
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "Bash",
                "canonical_tool_kind": "execute",
                "tool_input": {"command": 'rg "gobby build" src tests'},
            }
        )

        response = await RuleEngine(temp_db).evaluate(
            event,
            session_id="sid",
            variables={"loaded_skills": ["code-index"]},
        )

        assert response.decision == "allow"
