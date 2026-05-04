"""Tests for apply_persona MCP tool and build_persona_changes shared logic."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentWorkflows,
)

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_apply_persona.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


# ═══════════════════════════════════════════════════════════════════════
# build_persona_changes
# ═══════════════════════════════════════════════════════════════════════


class TestBuildPersonaChanges:
    """Tests for the shared build_persona_changes function."""

    def test_sets_agent_type_and_rules(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(name="developer")
        changes, active_rules, active_skills = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["_agent_type"] == "developer"
        assert "_active_rule_names" in changes
        assert changes["is_spawned_agent"] is False

    def test_spawned_flag(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(name="worker")
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
            is_spawned=True,
        )

        assert changes["is_spawned_agent"] is True

    def test_merges_agent_variables(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            name="custom",
            workflows=AgentWorkflows(
                variables={"my_var": "hello", "another": 42},
            ),
        )
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["my_var"] == "hello"
        assert changes["another"] == 42

    def test_skips_reserved_variables(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            name="custom",
            workflows=AgentWorkflows(
                variables={"_reserved": "bad", "good_var": "ok"},
            ),
        )
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert "_reserved" not in changes
        assert changes["good_var"] == "ok"

    def test_blocked_tools(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            name="restricted",
            blocked_tools=["Write", "Bash"],
            blocked_mcp_tools=["gobby-tasks:delete_task"],
        )
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["_agent_blocked_tools"] == ["Write", "Bash"]
        assert changes["_agent_blocked_mcp_tools"] == ["gobby-tasks:delete_task"]

    def test_skill_format_override(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            name="compact",
            workflows=AgentWorkflows(skill_format="compact"),
        )
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["_skill_format"] == "compact"

    def test_step_workflow_not_created_for_caller_persona(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes
        from gobby.workflows.definitions import WorkflowStep

        # Create a project + session so FK constraints are satisfied
        db.execute(
            "INSERT INTO projects (id, name, repo_path) VALUES (?, ?, ?)",
            ("proj-1", "test-project", "/tmp/test"),
        )
        session_id = "sess-step-test"
        db.execute(
            "INSERT INTO sessions (id, external_id, project_id, machine_id, source, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, "ext-1", "proj-1", "machine-1", "test", "active"),
        )

        agent = AgentDefinitionBody(
            name="stepper",
            steps=[
                WorkflowStep(name="plan", instructions="Plan the work"),
                WorkflowStep(name="execute", instructions="Do the work"),
            ],
        )
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id=session_id,
            db=db,
        )

        assert "_step_workflow_name" not in changes
        assert "step_workflow_complete" not in changes

        from gobby.workflows.state_manager import WorkflowInstanceManager

        instance = WorkflowInstanceManager(db).get_instance(session_id, "stepper-steps")
        assert instance is None

    def test_step_workflow_creates_instance_for_spawned_session(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes
        from gobby.workflows.definitions import WorkflowStep
        from gobby.workflows.state_manager import WorkflowInstanceManager

        db.execute(
            "INSERT INTO projects (id, name, repo_path) VALUES (?, ?, ?)",
            ("proj-2", "test-project-2", "/tmp/test"),
        )
        session_id = "sess-spawned-step-test"
        db.execute(
            "INSERT INTO sessions (id, external_id, project_id, machine_id, source, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, "ext-2", "proj-2", "machine-1", "test", "active"),
        )

        agent = AgentDefinitionBody(
            name="stepper",
            steps=[
                WorkflowStep(name="plan", instructions="Plan the work"),
                WorkflowStep(name="execute", instructions="Do the work"),
            ],
        )
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id=session_id,
            db=db,
            is_spawned=True,
        )

        assert changes["_step_workflow_name"] == "stepper-steps"
        assert changes["step_workflow_complete"] is False
        instance = WorkflowInstanceManager(db).get_instance(session_id, "stepper-steps")
        assert instance is not None
        assert instance.workflow_name == "stepper-steps"
        assert instance.current_step == "plan"

    def test_uses_preloaded_rules_and_skills(self, db: LocalDatabase) -> None:
        """When enabled_rules and all_skills are passed, DB is not queried."""
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(name="test")
        changes, active_rules, active_skills = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
            enabled_rules=[],
            all_skills=[],
            enabled_variables=[],
        )

        assert changes["_agent_type"] == "test"
        assert active_rules == set()

    def test_db_variable_definitions(self, db: LocalDatabase) -> None:
        """Variable definitions from the DB get applied."""
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes
        from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

        # Insert a variable definition
        def_manager = LocalWorkflowDefinitionManager(db)
        def_manager.create(
            name="my_db_var",
            workflow_type="variable",
            definition_json=json.dumps({"value": "from_db"}),
            source="installed",
        )

        agent = AgentDefinitionBody(name="test")
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes.get("my_db_var") == "from_db"


# ═══════════════════════════════════════════════════════════════════════
# build_session_persona_changes
# ═══════════════════════════════════════════════════════════════════════


class TestBuildSessionPersonaChanges:
    """Tests for the narrow session persona helper."""

    def test_only_sets_persona_context_fields(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_session_persona_changes
        from gobby.workflows.definitions import WorkflowStep

        agent = AgentDefinitionBody(
            name="planner",
            surfaces=["persona"],
            workflows=AgentWorkflows(
                variables={"should_not_merge": "nope"},
                skill_format="compact",
            ),
            blocked_tools=["Write"],
            blocked_mcp_tools=["gobby-tasks:delete_task"],
            steps=[WorkflowStep(name="plan", instructions="Plan")],
        )

        changes, active_skills = build_session_persona_changes(agent, db)

        assert changes == {
            "_agent_type": "planner",
            "_active_skill_names": None,
            "_skill_format": "compact",
            "_agent_context_injected": False,
            "_agent_identity_reinject": True,
        }
        assert active_skills is None


# ═══════════════════════════════════════════════════════════════════════
# apply_persona_impl
# ═══════════════════════════════════════════════════════════════════════


class TestApplyPersonaImpl:
    """Tests for the apply_persona MCP tool implementation."""

    @pytest.mark.asyncio
    async def test_unknown_agent_errors(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        result = await apply_persona_impl(
            agent="nonexistent",
            db=db,
            session_id="sess-1",
        )

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_no_db_errors(self) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        result = await apply_persona_impl(
            agent="test",
            db=None,
            session_id="sess-1",
        )

        assert result["success"] is False
        assert "Database" in result["error"]

    @pytest.mark.asyncio
    async def test_no_session_errors(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        with patch(
            "gobby.utils.session_context.get_session_context",
            return_value=None,
        ):
            result = await apply_persona_impl(
                agent="test",
                db=db,
                session_id=None,
            )

        assert result["success"] is False
        assert "session" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_happy_path(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent",
                return_value=AgentDefinitionBody(name="developer", surfaces=["persona"]),
            ),
            patch(
                "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
                return_value=(
                    {
                        "_agent_type": "developer",
                        "_active_skill_names": [],
                        "_agent_context_injected": False,
                        "_agent_identity_reinject": True,
                    },
                    set(),
                ),
            ) as mock_build,
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
            ) as mock_merge,
        ):
            result = await apply_persona_impl(
                agent="developer",
                db=db,
                session_id="sess-1",
            )

        assert result["success"] is True
        assert result["persona_applied"] == "developer"
        mock_build.assert_called_once()
        mock_merge.assert_called_once()

    @pytest.mark.asyncio
    async def test_merges_custom_variables(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent",
                return_value=AgentDefinitionBody(name="test", surfaces=["persona"]),
            ),
            patch(
                "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
                return_value=(
                    {
                        "_agent_type": "test",
                        "_agent_context_injected": False,
                        "_agent_identity_reinject": True,
                    },
                    None,
                ),
            ),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
            ) as mock_merge,
        ):
            result = await apply_persona_impl(
                agent="test",
                db=db,
                session_id="sess-1",
                variables={"custom_key": "custom_val"},
            )

        assert result["success"] is True
        # Verify custom variables were merged into the changes dict
        call_args = mock_merge.call_args
        merged_changes = call_args[0][1]
        assert merged_changes["custom_key"] == "custom_val"

    @pytest.mark.asyncio
    async def test_non_persona_capable_agent_errors(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        with patch(
            "gobby.workflows.agent_resolver.resolve_agent",
            return_value=AgentDefinitionBody(name="spawn-only"),
        ):
            result = await apply_persona_impl(
                agent="spawn-only",
                db=db,
                session_id="sess-1",
            )

        assert result["success"] is False
        assert "persona-capable" in result["error"]

    @pytest.mark.asyncio
    async def test_with_task_id(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        mock_task = MagicMock()
        mock_task.seq_num = 42
        mock_task_manager = MagicMock()
        mock_task_manager.get_task.return_value = mock_task

        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent",
                return_value=AgentDefinitionBody(name="test", surfaces=["persona"]),
            ),
            patch(
                "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
                return_value=(
                    {
                        "_agent_type": "test",
                        "_agent_context_injected": False,
                        "_agent_identity_reinject": True,
                    },
                    None,
                ),
            ),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
            ) as mock_merge,
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
                return_value="task-uuid",
            ),
        ):
            result = await apply_persona_impl(
                agent="test",
                db=db,
                session_id="sess-1",
                task_id="#42",
                task_manager=mock_task_manager,
            )

        assert result["success"] is True
        call_args = mock_merge.call_args
        merged_changes = call_args[0][1]
        assert merged_changes["assigned_task_id"] == "#42"
        assert merged_changes["session_task"] == "#42"
