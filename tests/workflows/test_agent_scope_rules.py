"""Tests for agent-scoped rules and agent-specific rule YAML files.

Covers:
- RuleEngine agent_scope filtering (_agent_type variable)
- Developer rules (no-coderabbit, require-tests-pass) scoped to developer
- QA rules (no-code-writing) scoped to qa
- Coordinator rules (no-code-writing) scoped to coordinator
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_agent_scope.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _make_event(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _insert_rule(
    manager: LocalWorkflowDefinitionManager,
    name: str,
    body: RuleDefinitionBody,
    priority: int = 100,
) -> str:
    row = manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        priority=priority,
        enabled=True,
    )
    return row.id


# ═══════════════════════════════════════════════════════════════════════
# RuleEngine agent_scope filtering
# ═══════════════════════════════════════════════════════════════════════


class TestAgentScopeFiltering:
    """RuleEngine filters rules by agent_scope based on _agent_type variable."""

    @pytest.mark.asyncio
    async def test_global_rule_fires_without_agent_type(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rules with no agent_scope fire for any session."""
        _insert_rule(
            manager,
            "global-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", tools=["Write"], reason="Global block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_global_rule_fires_with_agent_type(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rules with no agent_scope fire even when _agent_type is set."""
        _insert_rule(
            manager,
            "global-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", tools=["Write"], reason="Global block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {"_agent_type": "developer"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_scoped_rule_fires_for_matching_agent(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rules with agent_scope fire when _agent_type matches."""
        _insert_rule(
            manager,
            "dev-only-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["developer"],
                effects=[RuleEffect(type="block", tools=["Write"], reason="Dev block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {"_agent_type": "developer"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert "Dev block" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_scoped_rule_skipped_for_non_matching_agent(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rules with agent_scope are skipped when _agent_type doesn't match."""
        _insert_rule(
            manager,
            "dev-only-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["developer"],
                effects=[RuleEffect(type="block", tools=["Write"], reason="Dev block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {"_agent_type": "qa"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_scoped_rule_skipped_without_agent_type(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rules with agent_scope are skipped when no _agent_type is set."""
        _insert_rule(
            manager,
            "dev-only-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["developer"],
                effects=[RuleEffect(type="block", tools=["Write"], reason="Dev block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_multi_scope_rule_matches_any(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Rules with multiple agent_scope entries match any of them."""
        _insert_rule(
            manager,
            "dev-qa-block",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["developer", "qa"],
                effects=[RuleEffect(type="block", tools=["Write"], reason="Shared block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})

        # Should match for developer
        variables: dict[str, Any] = {"_agent_type": "developer"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)
        assert response.decision == "block"

        # Should match for qa
        variables = {"_agent_type": "qa"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)
        assert response.decision == "block"

        # Should NOT match for coordinator
        variables = {"_agent_type": "coordinator"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)
        assert response.decision == "allow"


# ═══════════════════════════════════════════════════════════════════════
# Developer agent rules
# ═══════════════════════════════════════════════════════════════════════


class TestDeveloperAgentRules:
    """Developer-scoped rules: no-coderabbit, require-tests-pass."""

    @pytest.mark.asyncio
    async def test_no_coderabbit_blocks_for_developer(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """no-coderabbit blocks coderabbit MCP tools for developer agents."""
        _insert_rule(
            manager,
            "no-coderabbit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["developer"],
                effects=[
                    RuleEffect(
                        type="block",
                        mcp_tools=["coderabbit:*"],
                        reason="Developers don't review their own code.",
                    )
                ],
            ),
            priority=50,
        )

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__coderabbit__review",
                "mcp_tool": "review",
                "mcp_server": "coderabbit",
            },
        )
        variables: dict[str, Any] = {"_agent_type": "developer"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert "review" in (response.reason or "").lower()

    @pytest.mark.asyncio
    async def test_no_coderabbit_allows_for_qa(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """no-coderabbit does not block QA agents."""
        _insert_rule(
            manager,
            "no-coderabbit",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["developer"],
                effects=[
                    RuleEffect(
                        type="block",
                        mcp_tools=["coderabbit:*"],
                        reason="Developers don't review their own code.",
                    )
                ],
            ),
            priority=50,
        )

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={
                "tool_name": "mcp__coderabbit__review",
                "mcp_tool": "review",
                "mcp_server": "coderabbit",
            },
        )
        variables: dict[str, Any] = {"_agent_type": "qa"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_require_tests_blocks_git_commit_for_developer(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """require-tests-pass blocks git commit for developer agents."""
        _insert_rule(
            manager,
            "require-tests-pass",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["developer"],
                effects=[
                    RuleEffect(
                        type="block",
                        tools=["Bash"],
                        command_pattern=r"git\s+commit",
                        reason="Run tests before committing.",
                    )
                ],
            ),
            priority=50,
        )

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Bash", "command": "git commit -m 'fix'"},
        )
        variables: dict[str, Any] = {"_agent_type": "developer"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"


# ═══════════════════════════════════════════════════════════════════════
# QA agent rules
# ═══════════════════════════════════════════════════════════════════════


class TestQAAgentRules:
    """QA-scoped rules: no-code-writing (only test files)."""

    @pytest.mark.asyncio
    async def test_no_code_writing_blocks_edit_for_qa(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """no-code-writing blocks Edit/Write for QA agents."""
        _insert_rule(
            manager,
            "qa-no-code-writing",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["qa"],
                effects=[
                    RuleEffect(
                        type="block",
                        tools=["Edit", "Write"],
                        reason="QA agents can only edit test files.",
                    )
                ],
            ),
            priority=50,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        variables: dict[str, Any] = {"_agent_type": "qa"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_no_code_writing_allows_for_developer(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """no-code-writing does not block developer agents."""
        _insert_rule(
            manager,
            "qa-no-code-writing",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["qa"],
                effects=[
                    RuleEffect(
                        type="block",
                        tools=["Edit", "Write"],
                        reason="QA agents can only edit test files.",
                    )
                ],
            ),
            priority=50,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Edit"})
        variables: dict[str, Any] = {"_agent_type": "developer"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"


# ═══════════════════════════════════════════════════════════════════════
# Coordinator agent rules
# ═══════════════════════════════════════════════════════════════════════


class TestCoordinatorAgentRules:
    """Coordinator-scoped rules: no-code-writing (orchestrate only)."""

    @pytest.mark.asyncio
    async def test_no_code_writing_blocks_for_coordinator(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Coordinator can't use Edit/Write/NotebookEdit."""
        _insert_rule(
            manager,
            "coordinator-no-code-writing",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["coordinator"],
                effects=[
                    RuleEffect(
                        type="block",
                        tools=["Edit", "Write", "NotebookEdit"],
                        reason="Coordinators orchestrate, don't write code.",
                    )
                ],
            ),
            priority=50,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {"_agent_type": "coordinator"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_no_code_writing_allows_for_developer(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Coordinator no-code-writing doesn't affect developer agents."""
        _insert_rule(
            manager,
            "coordinator-no-code-writing",
            RuleDefinitionBody(
                event=RuleEvent.BEFORE_TOOL,
                agent_scope=["coordinator"],
                effects=[
                    RuleEffect(
                        type="block",
                        tools=["Edit", "Write", "NotebookEdit"],
                        reason="Coordinators orchestrate, don't write code.",
                    )
                ],
            ),
            priority=50,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {"_agent_type": "developer"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
