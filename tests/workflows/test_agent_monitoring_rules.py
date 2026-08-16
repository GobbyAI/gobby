"""Tests for build-coordinator skill guidance rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
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


def _skill_fetch_template(name: str) -> str:
    return f'{{{{ skill_fetch_directive("{name}") }}}}'


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


class TestRequireBuildCoordinatorForGobbyBuild:
    def test_rule_structure(
        self,
        temp_db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        """Build command gate should retain the expected rule condition and guidance."""
        _sync_bundled(temp_db)
        row = manager.get_by_name("require-build-coordinator-for-gobby-build")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('build-coordinator')" in body.when
        assert "source != 'pipeline'" in body.when
        assert "is_spawned_agent" in body.when
        assert "session_type" in body.when
        assert "is_gobby_build_command" in body.when
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("build-coordinator")

    @pytest.mark.asyncio
    async def test_blocks_tmux_agent_gobby_build_before_skill_load(
        self, temp_db: HubDatabase
    ) -> None:
        """Terminal agents should load build-coordinator before running gobby build."""
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "Bash",
                "canonical_tool_kind": "execute",
                "tool_input": {"command": "uv run --frozen gobby build #15117 --clone"},
            }
        )

        response = await RuleEngine(temp_db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"is_spawned_agent": True},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert "require-build-coordinator-for-gobby-build" in response.reason
        assert skill_fetch_directive("build-coordinator") in response.reason

    @pytest.mark.asyncio
    async def test_normalized_bash_gobby_build_triggers_build_coordinator_rule(
        self, temp_db: HubDatabase
    ) -> None:
        _sync_bundled(temp_db)
        data: dict[str, object] = {
            "tool_name": "exec_command",
            "tool_input": {"command": "uv run gobby build #15117 --clone"},
        }
        normalize_tool_fields(data)

        response = await RuleEngine(temp_db).evaluate(
            _event(data),
            session_id=SESSION_ID,
            variables={"is_spawned_agent": True},
        )

        assert data["canonical_tool_kind"] == "execute"
        assert response.decision == "block"
        assert response.reason is not None
        assert "require-build-coordinator-for-gobby-build" in response.reason

    @pytest.mark.asyncio
    async def test_blocks_web_chat_gobby_build_before_skill_load(
        self, temp_db: HubDatabase
    ) -> None:
        """Web-chat agents should load build-coordinator before running gobby build."""
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "Bash",
                "canonical_tool_kind": "execute",
                "tool_input": {"command": "gobby build #15117"},
            },
            metadata={"session_type": "web_chat"},
        )

        response = await RuleEngine(temp_db).evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "block"
        assert response.reason is not None
        assert "require-build-coordinator-for-gobby-build" in response.reason

    @pytest.mark.asyncio
    async def test_allows_tmux_agent_gobby_build_after_skill_load(
        self, temp_db: HubDatabase
    ) -> None:
        """Terminal agents may run gobby build after build-coordinator is loaded."""
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
            session_id=SESSION_ID,
            variables={"is_spawned_agent": True, "loaded_skills": ["build-coordinator"]},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_allows_operator_gobby_build_without_skill_load(
        self, temp_db: HubDatabase
    ) -> None:
        """Human/operator sessions may run gobby build without agent skill gates."""
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "Bash",
                "canonical_tool_kind": "execute",
                "tool_input": {"command": "gobby build #15117"},
            }
        )

        response = await RuleEngine(temp_db).evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_allows_dispatcher_gobby_build_without_skill_load(
        self, temp_db: HubDatabase
    ) -> None:
        """Dispatcher-origin build commands are exempt from spawned-agent skill gates."""
        _sync_bundled(temp_db)
        event = _event(
            {
                "tool_name": "Bash",
                "canonical_tool_kind": "execute",
                "tool_input": {"command": "gobby build #15117"},
            },
            source=SessionSource.PIPELINE,
        )

        response = await RuleEngine(temp_db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"is_spawned_agent": True},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_allows_commands_that_only_mention_gobby_build(
        self, temp_db: HubDatabase
    ) -> None:
        """Commands that mention gobby build as text should not trip the gate."""
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
            session_id=SESSION_ID,
            variables={"loaded_skills": ["code-index"]},
        )

        assert response.decision == "allow"
