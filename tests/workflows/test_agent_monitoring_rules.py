"""Tests for retired monitoring skill rule cleanup."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def manager(temp_db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(temp_db)


def _sync_bundled(db: HubDatabase) -> None:
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")


def _event(
    data: dict[str, object],
    *,
    source: SessionSource = SessionSource.CODEX,
    metadata: dict[str, object] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata=metadata or {},
    )


class TestRemovedBuildCoordinatorMonitoringSkillRule:
    def test_removed_rule_is_not_synced(
        self,
        temp_db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        """Deprecated monitoring-skill gate should stay removed after sync."""
        manager.create(
            name="require-build-coordinator-monitoring-skill",
            definition_json=json.dumps(
                {
                    "event": "before_tool",
                    "effects": [
                        {
                            "type": "block",
                            "reason": skill_fetch_directive("build-coordinator"),
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
        self, temp_db: HubDatabase
    ) -> None:
        """Generic inspection commands should not require build-coordinator skill."""
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

        response = await RuleEngine(temp_db).evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "allow"
