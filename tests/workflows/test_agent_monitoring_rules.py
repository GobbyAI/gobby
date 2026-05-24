"""Tests for removed build-coordinator progress-inspection guidance rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
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

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-session",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
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
