"""Tests for apply_persona MCP tool and build_persona_changes shared logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentStepWorkflowBody,
    AgentWorkflows,
)

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


# ═══════════════════════════════════════════════════════════════════════
# build_persona_changes
# ═══════════════════════════════════════════════════════════════════════


class TestBuildPersonaChanges:
    """Tests for the shared build_persona_changes function."""

    def test_sets_agent_type_and_rules(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="developer",
        )
        changes, active_rules, active_skills = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["_agent_type"] == "developer"
        assert "_active_rule_names" in changes
        assert changes["is_spawned_agent"] is False

    def test_spawned_flag(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="worker",
        )
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
            is_spawned=True,
        )

        assert changes["is_spawned_agent"] is True

    def test_merges_agent_variables(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
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

    def test_skips_reserved_variables(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
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

    def test_blocked_tools(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
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

    def test_skill_format_override(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="compact",
            workflows=AgentWorkflows(skill_format="compact"),
        )
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id="sess-1",
            db=db,
        )

        assert changes["_skill_format"] == "compact"

    def test_step_workflow_not_created_for_caller_persona(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes
        from gobby.workflows.definitions import WorkflowStep

        # Create a project + session so FK constraints are satisfied
        db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        ("11111111-1111-4111-8111-111111110001", "test-project"),
        )
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4002"
        db.execute(
            "INSERT INTO sessions (id, external_id, project_id, machine_id, source, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                session_id,
                "ext-1",
                "11111111-1111-4111-8111-111111110001",
                "21000000-0000-4000-8000-000000000001",
                "test",
                "active",
            ),
        )

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="stepper",
            step_workflow=AgentStepWorkflowBody(
                steps=[
                    WorkflowStep(name="plan", instructions="Plan the work"),
                    WorkflowStep(name="execute", instructions="Do the work"),
                ],
            ),
        )
        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id=session_id,
            db=db,
        )

        assert "_step_workflow_name" not in changes
        assert "step_workflow_complete" not in changes

        from gobby.workflows.step_instances import AgentStepInstanceManager

        instance = AgentStepInstanceManager(db).get_for_session(session_id)
        assert instance is None

    @pytest.mark.parametrize(
        "task_variables",
        [{}, {"assigned_task_id": None, "active_task_id": None}],
        ids=["missing", "json-null"],
    )
    def test_step_workflow_not_created_for_taskless_spawn(
        self,
        db: HubDatabase,
        task_variables: dict[str, object],
    ) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes
        from gobby.workflows.definitions import WorkflowStep
        from gobby.workflows.state_manager import SessionVariableManager
        from gobby.workflows.step_instances import AgentStepInstanceManager

        db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        ("11111111-1111-4111-8111-111111110004", "taskless-project"),
        )
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4005"
        db.execute(
            "INSERT INTO sessions (id, external_id, project_id, machine_id, source, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                session_id,
                "ext-taskless",
                "11111111-1111-4111-8111-111111110004",
                "21000000-0000-4000-8000-000000000001",
                "test",
                "active",
            ),
        )
        SessionVariableManager(db).merge_variables(session_id, task_variables)
        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="stepper",
            step_workflow=AgentStepWorkflowBody(
                steps=[WorkflowStep(name="plan", instructions="Plan the work")],
            ),
        )

        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id=session_id,
            db=db,
            is_spawned=True,
        )

        assert "_step_workflow_name" not in changes
        assert "step_workflow_complete" not in changes
        instance = AgentStepInstanceManager(db).get_for_session(session_id)
        assert instance is None

    def test_spawned_session_preserves_existing_step_workflow(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes
        from gobby.workflows.definitions import WorkflowStep
        from gobby.workflows.state_manager import SessionVariableManager
        from gobby.workflows.step_instances import AgentStepInstanceManager
        from tests.workflows.step_instance_fixtures import make_step_instance

        db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        ("11111111-1111-4111-8111-111111110003", "test-project-preserve"),
        )
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4004"
        db.execute(
            "INSERT INTO sessions (id, external_id, project_id, machine_id, source, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                session_id,
                "ext-preserve",
                "11111111-1111-4111-8111-111111110003",
                "21000000-0000-4000-8000-000000000001",
                "codex",
                "active",
            ),
        )
        SessionVariableManager(db).merge_variables(session_id, {"assigned_task_id": "#20144"})

        instance_mgr = AgentStepInstanceManager(db)
        instance_mgr.save(
            make_step_instance(
                session_id,
                agent_name="stepper",
                current_step="execute",
                variables={"task_claimed": True},
            )
        )

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="stepper",
            step_workflow=AgentStepWorkflowBody(
                variables={"task_claimed": False, "loaded_skills": []},
                steps=[
                    WorkflowStep(name="claim", instructions="Claim the task"),
                    WorkflowStep(name="execute", instructions="Do the work"),
                ],
            ),
        )

        changes, _, _ = build_persona_changes(
            agent_body=agent,
            session_id=session_id,
            db=db,
            is_spawned=True,
        )

        instance = instance_mgr.get_for_session(session_id)
        assert instance is not None
        assert instance.current_step == "execute"
        assert instance.variables.get("task_claimed") is True

    def test_uses_preloaded_rules_and_skills(self, db: HubDatabase) -> None:
        """When enabled_rules and all_skills are passed, DB is not queried."""
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="test",
        )
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

    def test_db_variable_definitions(self, db: HubDatabase) -> None:
        """Variable definitions from the DB get applied."""
        from gobby.mcp_proxy.tools.apply_persona import build_persona_changes
        from gobby.storage.definitions import SessionVariableDefaultManager

        SessionVariableDefaultManager(db).create(name="my_db_var", default_value="from_db")

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="test",
        )
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

    def test_only_sets_persona_context_fields(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_session_persona_changes
        from gobby.workflows.definitions import WorkflowStep

        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="planner",
            surfaces=["persona"],
            workflows=AgentWorkflows(
                variables={"should_not_merge": "nope"},
                skill_format="compact",
            ),
            blocked_tools=["Write"],
            blocked_mcp_tools=["gobby-tasks:delete_task"],
            step_workflow=AgentStepWorkflowBody(
                steps=[WorkflowStep(name="plan", instructions="Plan")],
            ),
        )

        changes, active_skills = build_session_persona_changes(agent, db)

        assert changes == {
            "_persona_name": "planner",
            "_active_skill_names": None,
            "_skill_format": "compact",
            "_agent_context_injected": False,
            "_agent_identity_reinject": True,
        }
        assert active_skills is None

    def test_backend_definition_separates_persona_from_agent_lifecycle(
        self,
        db: HubDatabase,
    ) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_session_persona_context

        path = get_bundled_agents_path() / "backend-developer.yaml"
        agent = AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))

        persona, _ = build_session_persona_context(agent, db, cli_source="codex")
        assert persona is not None
        assert "interactive backend engineering guidance" in persona
        assert "assigned_task_id" not in persona
        assert "end_agent_run" not in persona
        assert "submit_for_review" not in persona

        spawned = agent.prompt_for("agent")
        assert spawned is not None
        assert "assigned_task_id" in spawned
        assert "end_agent_run" in spawned
        assert "interactive backend engineering guidance" not in spawned


# ═══════════════════════════════════════════════════════════════════════
# apply_persona_impl
# ═══════════════════════════════════════════════════════════════════════


class TestApplyPersonaImpl:
    """Tests for the apply_persona MCP tool implementation."""

    @pytest.mark.asyncio
    async def test_unknown_agent_errors(self, db: HubDatabase) -> None:
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
    async def test_no_session_errors(self, db: HubDatabase) -> None:
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
    async def test_happy_path(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent_with_row",
                return_value=(
                    AgentDefinitionBody(
                        prompts={
                            "persona": "Interactive guidance.",
                            "agent": "Run the assigned task.",
                        },
                        name="developer",
                        surfaces=["persona"],
                    ),
                    MagicMock(step_workflow_id=None),
                ),
            ),
            patch(
                "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
                return_value=(
                    {
                        "_persona_name": "developer",
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
                session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
            )

        assert result["success"] is True
        assert result["persona_applied"] == "developer"
        mock_build.assert_called_once()
        mock_merge.assert_called_once()

    @pytest.mark.asyncio
    async def test_merges_custom_variables(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent_with_row",
                return_value=(
                    AgentDefinitionBody(
                        prompts={
                            "persona": "Interactive guidance.",
                            "agent": "Run the assigned task.",
                        },
                        name="test",
                        surfaces=["persona"],
                    ),
                    MagicMock(step_workflow_id=None),
                ),
            ),
            patch(
                "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
                return_value=(
                    {
                        "_persona_name": "test",
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
                session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
                variables={"custom_key": "custom_val"},
            )

        assert result["success"] is True
        # Verify custom variables were merged into the changes dict
        call_args = mock_merge.call_args
        merged_changes = call_args[0][1]
        assert merged_changes["custom_key"] == "custom_val"

    @pytest.mark.asyncio
    async def test_stepful_persona_preserves_lifecycle_and_enforcement_state(
        self,
        db: HubDatabase,
    ) -> None:
        """A persona switch changes prompt and skills without adopting worker posture."""
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl
        from gobby.workflows.definitions import WorkflowStep
        from gobby.workflows.state_manager import SessionVariableManager
        from gobby.workflows.step_instances import AgentStepInstanceManager

        db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        ("11111111-1111-4111-8111-111111110006", "persona-project"),
        )
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4006"
        db.execute(
            "INSERT INTO sessions (id, external_id, project_id, machine_id, source, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                session_id,
                "ext-persona",
                "11111111-1111-4111-8111-111111110006",
                "21000000-0000-4000-8000-000000000001",
                "claude",
                "active",
            ),
        )
        state = SessionVariableManager(db)
        state.merge_variables(
            session_id,
            {
                "_agent_type": "default",
                "_active_rule_names": ["interactive-rule"],
                "_active_skill_names": ["old-skill"],
                "_skill_format": "verbose",
                "_agent_blocked_tools": ["ExistingTool"],
                "_agent_blocked_mcp_tools": ["existing-server:existing-tool"],
                "is_spawned_agent": False,
                "step_workflow_complete": False,
            },
        )
        reviewer = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="qa-reviewer",
            surfaces=["persona", "spawn"],
            workflows=AgentWorkflows(skill_format="compact"),
            blocked_tools=["Bash"],
            blocked_mcp_tools=["gobby-tasks:close_task"],
            step_workflow=AgentStepWorkflowBody(
                steps=[
                    WorkflowStep(name="claim", instructions="Claim the task"),
                    WorkflowStep(name="terminate", instructions="Call end_agent_run"),
                ],
            ),
        )

        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent_with_row",
                return_value=(
                    reviewer,
                    MagicMock(step_workflow_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                ),
            ),
        ):
            result = await apply_persona_impl(
                agent="qa-reviewer",
                db=db,
                session_id=session_id,
            )

        assert result["success"] is True
        assert result["mode"] == "persona"
        assert AgentStepInstanceManager(db).get_for_session(session_id) is None
        row = db.fetchone("SELECT COUNT(*) AS n FROM agent_step_instances")
        assert row is not None
        assert row["n"] == 0
        variables = state.get_variables(session_id)
        assert variables["_persona_name"] == "qa-reviewer"
        assert variables["_agent_type"] == "default"
        assert variables["_active_rule_names"] == ["interactive-rule"]
        assert variables["_active_skill_names"] is None
        assert variables["_skill_format"] == "compact"
        assert variables["_agent_blocked_tools"] == ["ExistingTool"]
        assert variables["_agent_blocked_mcp_tools"] == ["existing-server:existing-tool"]
        assert variables["is_spawned_agent"] is False
        assert variables["step_workflow_complete"] is False

    @pytest.mark.asyncio
    async def test_non_persona_capable_agent_errors(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        with patch(
            "gobby.workflows.agent_resolver.resolve_agent_with_row",
            return_value=(
                AgentDefinitionBody(
                    prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                    name="spawn-only",
                ),
                MagicMock(step_workflow_id=None),
            ),
        ):
            result = await apply_persona_impl(
                agent="spawn-only",
                db=db,
                session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
            )

        assert result["success"] is False
        assert "'persona' surface" in result["error"]

    @pytest.mark.asyncio
    async def test_with_task_id(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        mock_task = MagicMock()
        mock_task.seq_num = 42
        mock_task_manager = MagicMock()
        mock_task_manager.get_task.return_value = mock_task

        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent_with_row",
                return_value=(
                    AgentDefinitionBody(
                        prompts={
                            "persona": "Interactive guidance.",
                            "agent": "Run the assigned task.",
                        },
                        name="test",
                        surfaces=["persona"],
                    ),
                    MagicMock(step_workflow_id=None),
                ),
            ),
            patch(
                "gobby.mcp_proxy.tools.apply_persona.build_session_persona_changes",
                return_value=(
                    {
                        "_persona_name": "test",
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
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
                return_value="task-uuid",
            ),
        ):
            result = await apply_persona_impl(
                agent="test",
                db=db,
                session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
                task_id="#42",
                task_manager=mock_task_manager,
            )

        assert result["success"] is True
        call_args = mock_merge.call_args
        merged_changes = call_args[0][1]
        assert merged_changes["assigned_task_id"] == "#42"
        assert "session_task" not in merged_changes
