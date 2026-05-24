"""Tests for build-coordinator progress-inspection guidance rules."""

from __future__ import annotations

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


class TestRequireBuildCoordinatorMonitoringSkill:
    def test_rule_structure(self, temp_db, manager) -> None:
        _sync_bundled(temp_db)
        row = manager.get_by_name("require-build-coordinator-monitoring-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.event.value == "before_tool"
        assert "skill_loaded('build-coordinator')" in (body.when or "")
        assert "list_running_agents" in (body.when or "")
        assert "capture_output" in (body.when or "")
        assert "tmux capture-pane" in (body.when or "")
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert (
            body.effects[0].reason
            == 'Call get_skill(name="build-coordinator") on gobby-skills, then continue.'
        )

    @pytest.mark.asyncio
    async def test_blocks_monitoring_schema_before_skill_load(self, temp_db) -> None:
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "mcp__gobby__get_tool_schema",
                "mcp_tool": "get_tool_schema",
                "tool_input": {
                    "server_name": "gobby-agents",
                    "tool_name": "list_running_agents",
                },
            }
        )

        response = await RuleEngine(temp_db).evaluate(event, session_id="sid", variables={})

        assert response.decision == "block"
        assert "build-coordinator" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_skips_monitoring_schema_after_build_coordinator_load(self, temp_db) -> None:
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "mcp__gobby__get_tool_schema",
                "mcp_tool": "get_tool_schema",
                "tool_input": {
                    "server_name": "gobby-agents",
                    "tool_name": "list_running_agents",
                },
            }
        )

        response = await RuleEngine(temp_db).evaluate(
            event,
            session_id="sid",
            variables={"loaded_skills": ["build-coordinator"]},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_blocks_monitoring_tool_call_before_skill_load(self, temp_db) -> None:
        _sync_bundled(temp_db)
        event = _event(
            {
                "mcp_server": "gobby-agents",
                "mcp_tool": "get_running_agent",
                "tool_name": "mcp__gobby-agents__get_running_agent",
                "tool_input": {"run_id": "run-1"},
            }
        )

        response = await RuleEngine(temp_db).evaluate(event, session_id="sid", variables={})

        assert response.decision == "block"
        assert "build-coordinator" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_blocks_session_capture_raw_call_before_skill_load(self, temp_db) -> None:
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-sessions",
                    "tool_name": "capture_output",
                    "arguments": {"session_id": "child-session"},
                },
            }
        )

        response = await RuleEngine(temp_db).evaluate(event, session_id="sid", variables={})

        assert response.decision == "block"
        assert "build-coordinator" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_blocks_raw_tmux_monitoring_before_skill_load(self, temp_db) -> None:
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "Bash",
                "canonical_tool_kind": "execute",
                "tool_input": {"command": "tmux capture-pane -t gobby-agent -p -S -"},
            }
        )

        response = await RuleEngine(temp_db).evaluate(event, session_id="sid", variables={})

        assert response.decision == "block"
        assert "build-coordinator" in (response.reason or "")
