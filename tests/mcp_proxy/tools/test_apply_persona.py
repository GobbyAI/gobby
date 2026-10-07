"""Tests for live persona switching alongside shared definition activation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentStepWorkflowBody,
)
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows

pytestmark = pytest.mark.unit


async def test_live_switch_replaces_skills_and_preserves_existing_step_instance(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl
    from gobby.skills.discovery import get_session_skill_exclusions
    from gobby.storage.definitions import AgentDefinitionManager
    from gobby.storage.skills import LocalSkillManager
    from gobby.workflows.definitions import AgentSelector
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.workflows.step_instances import AgentStepInstanceManager
    from tests.mcp_proxy.tools.test_apply_agent_definition import register_session
    from tests.workflows.step_instance_fixtures import make_step_instance

    sid = register_session(temp_db, monkeypatch)
    skills = LocalSkillManager(temp_db)
    for name in ("first-skill", "second-skill"):
        skills.create_skill(name=name, description=name, content=name, enabled=True)
    for name, selected, excluded in (
        ("first", "first-skill", "second-skill"),
        ("second", "second-skill", "first-skill"),
    ):
        body = make_agent_definition(
            name=name,
            surfaces=["persona"],
            prompts={"persona": name},
            workflows=make_agent_workflows(
                skill_selectors=AgentSelector(
                    include=[f"name:{selected}"], exclude=[f"name:{excluded}"]
                ),
                skill_format="compact",
            ),
        )
        AgentDefinitionManager(temp_db).create(name=name, definition_json=body.model_dump_json())
    manager = SessionVariableManager(temp_db)
    seat = {
        "_agent_type": "seat",
        "_active_rule_names": ["seat-rule"],
        "_agent_blocked_tools": ["Write"],
        "seat_variable": 42,
        "step_workflow_complete": False,
    }
    manager.merge_variables(sid, seat)
    instances = AgentStepInstanceManager(temp_db)
    instances.save(make_step_instance(sid, agent_name="seat", current_step="execute"))
    instance = instances.get_for_session(sid)
    for name, selected, excluded in (
        ("first", "first-skill", "second-skill"),
        ("second", "second-skill", "first-skill"),
    ):
        result = await apply_persona_impl(name, temp_db, sid)
        assert result["success"] is True
        state = manager.get_variables(sid)
        assert state["_persona_name"] == name
        assert state["_active_skill_names"] == [selected]
        assert state["_skill_format"] == "compact"
        assert get_session_skill_exclusions(temp_db, sid, None) == {excluded}
        assert {key: state[key] for key in seat} == seat
        assert instances.get_for_session(sid) == instance


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


class TestBuildSessionPersonaChanges:
    """Tests for the narrow session persona helper."""

    def test_only_sets_persona_context_fields(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_session_persona_changes
        from gobby.workflows.definitions import WorkflowStep

        agent = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="planner",
            surfaces=["persona"],
            workflows=make_agent_workflows(
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

    def test_tech_writer_definition_separates_persona_from_agent_lifecycle(
        self,
        db: HubDatabase,
    ) -> None:
        from gobby.mcp_proxy.tools.apply_persona import build_session_persona_context

        path = get_bundled_agents_path() / "tech-writer.yaml"
        agent = AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))

        persona, _ = build_session_persona_context(agent, db, cli_source="codex")
        assert persona is not None
        assert "interactive technical-writing guidance" in persona
        assert "assigned_task_id" not in persona
        assert "end_agent_run" not in persona
        assert "submit_for_review" not in persona

        spawned = agent.prompt_for("agent")
        assert spawned is not None
        assert "assigned_task_id" in spawned
        assert "end_agent_run" in spawned
        assert "interactive technical-writing guidance" not in spawned


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
                    make_agent_definition(
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
                    make_agent_definition(
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
        reviewer = make_agent_definition(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="qa-reviewer",
            surfaces=["persona", "spawn"],
            workflows=make_agent_workflows(skill_format="compact"),
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
                make_agent_definition(
                    prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                    name="spawn-only",
                    surfaces=["spawn"],
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
                    make_agent_definition(
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

    async def test_unresolvable_task_id_refuses_instead_of_dropping_the_binding(
        self, db: HubDatabase
    ) -> None:
        """A bad task_id fails loudly; a persona missing assigned_task_id is never applied."""
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl
        from gobby.storage.tasks import TaskNotFoundError

        mock_task_manager = MagicMock()

        with (
            patch(
                "gobby.workflows.agent_resolver.resolve_agent_with_row",
                return_value=(
                    make_agent_definition(
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
                "gobby.workflows.state_manager.SessionVariableManager.merge_variables",
            ) as mock_merge,
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks.resolve_task_id_for_mcp",
                side_effect=TaskNotFoundError("Task #999 not found"),
            ),
        ):
            result = await apply_persona_impl(
                agent="test",
                db=db,
                session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa4001",
                task_id="#999",
                task_manager=mock_task_manager,
            )

        assert result["success"] is False
        assert "#999" in result["error"]
        mock_merge.assert_not_called()
