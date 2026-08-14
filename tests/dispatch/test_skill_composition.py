"""Pre-dispatch skill-composition validation tests."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.build.observability import explain_dispatch
from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.skill_composition import inspect_skill_composition
from gobby.dispatch.spawn import DispatchSpawnFailed, _with_skill_allowed_tools, spawn_agent
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentStepWorkflowBody,
    WorkflowStep,
)

pytestmark = pytest.mark.integration

TEST_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


def _skill(
    temp_db: HubDatabase,
    name: str,
    *,
    enabled: bool = True,
    allowed_tools: list[str] | None = None,
) -> None:
    LocalSkillManager(temp_db).create_skill(
        name=name,
        description=f"{name} test skill",
        content=f"# {name}",
        enabled=enabled,
        allowed_tools=allowed_tools,
    )


def _agent(name: str = "composition-agent") -> AgentDefinitionBody:
    return AgentDefinitionBody(
        name=name,
        step_workflow=AgentStepWorkflowBody(
            variables={"required_skills": ["required-skill"]},
            steps=[WorkflowStep(name="work", allowed_tools="all")],
        ),
    )


def test_skill_composition_reports_unknown_skill(temp_db: HubDatabase) -> None:
    _skill(temp_db, "required-skill")

    report = inspect_skill_composition(
        temp_db,
        project_id=TEST_PROJECT_ID,
        agent_body=_agent(),
        additional_skills=("missing-skill",),
    )

    assert report.valid is False
    assert report.unknown_skills == ("missing-skill",)
    assert report.disabled_skills == ()
    assert report.failure_reason == "skill_composition_invalid:unknown=missing-skill"


def test_skill_composition_reports_disabled_skill(temp_db: HubDatabase) -> None:
    _skill(temp_db, "required-skill", enabled=False)

    report = inspect_skill_composition(
        temp_db,
        project_id=TEST_PROJECT_ID,
        agent_body=_agent(),
        additional_skills=(),
    )

    assert report.valid is False
    assert report.unknown_skills == ()
    assert report.disabled_skills == ("required-skill",)
    assert report.failure_reason == "skill_composition_invalid:disabled=required-skill"


def test_skill_composition_clean_pass_through_reports_allowed_tools_union(
    temp_db: HubDatabase,
) -> None:
    _skill(temp_db, "required-skill", allowed_tools=["Read", "mcp__gobby__call_tool"])
    _skill(temp_db, "optional-skill", allowed_tools=["Read", "Bash"])

    report = inspect_skill_composition(
        temp_db,
        project_id=TEST_PROJECT_ID,
        agent_body=_agent(),
        additional_skills=("optional-skill", "required-skill"),
    )

    assert report.valid is True
    assert report.required_skills == ("required-skill",)
    assert report.additional_skills == ("optional-skill", "required-skill")
    assert report.checked_skills == ("required-skill", "optional-skill")
    assert report.allowed_tools == ("Bash", "Read", "mcp__gobby__call_tool")
    assert report.failure_reason is None


def test_composed_skill_tools_extend_restricted_steps_without_mutating_definition() -> None:
    agent = AgentDefinitionBody(
        name="restricted-agent",
        step_workflow=AgentStepWorkflowBody(
            steps=[
                WorkflowStep(name="work", allowed_tools=["Read"]),
                WorkflowStep(name="unrestricted", allowed_tools="all"),
            ],
        ),
    )

    composed = _with_skill_allowed_tools(agent, ("Bash", "Read"))

    assert composed is not None
    assert composed is not agent
    assert composed.step_workflow is not None
    assert composed.step_workflow.steps[0].allowed_tools == ["Read", "Bash"]
    assert composed.step_workflow.steps[1].allowed_tools == "all"
    assert agent.step_workflow is not None
    assert agent.step_workflow.steps[0].allowed_tools == ["Read"]


def test_skill_composition_uses_single_visible_skill_query(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skill(temp_db, "required-skill")
    _skill(temp_db, "optional-skill")
    calls: list[tuple[str | None, int | None]] = []
    original = LocalSkillManager.list_skills

    def tracked_list_skills(
        manager: LocalSkillManager,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        calls.append((kwargs.get("project_id"), kwargs.get("limit")))
        return original(manager, *args, **kwargs)

    monkeypatch.setattr(LocalSkillManager, "list_skills", tracked_list_skills)
    monkeypatch.setattr(
        LocalSkillManager,
        "get_by_name",
        lambda *_args, **_kwargs: pytest.fail("composition must use the visible-skill query"),
    )

    report = inspect_skill_composition(
        temp_db,
        project_id=TEST_PROJECT_ID,
        agent_body=_agent(),
        additional_skills=("optional-skill",),
    )

    assert report.valid is True
    assert calls == [(TEST_PROJECT_ID, -1)]


def test_skill_composition_skips_query_when_no_skills_are_checked(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        LocalSkillManager,
        "list_skills",
        lambda *_args, **_kwargs: pytest.fail("empty composition must not query skills"),
    )

    report = inspect_skill_composition(
        temp_db,
        project_id=TEST_PROJECT_ID,
        agent_body=None,
        additional_skills=(),
    )

    assert report.valid is True
    assert report.checked_skills == ()


@pytest.mark.asyncio
async def test_spawn_and_explain_share_unknown_skill_failure(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = LocalProjectManager(temp_db).create(
        name="skill-composition",
        repo_path="/tmp/skill-composition",
    )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=project.id,
        title="Composition target",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    task = task_manager.update_task(task.id, allow_automation=True, isolation="none")
    agent_body = _agent()
    LocalWorkflowDefinitionManager(temp_db).create(
        name=agent_body.name,
        definition_json=agent_body.model_dump_json(),
        workflow_type="agent",
        project_id=project.id,
    )
    _skill(temp_db, "required-skill")
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug=agent_body.name,
        prompt="go",
        additional_skills=("missing-skill",),
    )
    unexpected_spawn = AsyncMock(side_effect=AssertionError("must fail before agent spawn"))
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        unexpected_spawn,
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )
    offloaded: list[str] = []

    async def record_to_thread(
        func: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        offloaded.append(func.__name__)
        return func(*args, **kwargs)

    monkeypatch.setattr("gobby.dispatch.spawn.asyncio.to_thread", record_to_thread)

    with pytest.raises(
        DispatchSpawnFailed,
        match="skill_composition_invalid:unknown=missing-skill",
    ):
        await spawn_agent(action, db=temp_db, services=services)

    assert offloaded == ["resolve_agent", "inspect_skill_composition"]
    monkeypatch.setattr("gobby.build.observability._dispatch_block_reason", lambda *_args: None)
    monkeypatch.setattr("gobby.build.observability.dispatch_rules.evaluate", lambda *_args: action)
    explanation = explain_dispatch(task.id, db=temp_db, project_id=project.id)

    assert unexpected_spawn.await_count == 0
    assert explanation["eligible"] is False
    assert explanation["reason"] == "skill_composition_invalid:unknown=missing-skill"
    assert explanation["skill_composition"] == {
        "valid": False,
        "required_skills": ["required-skill"],
        "additional_skills": ["missing-skill"],
        "checked_skills": ["required-skill", "missing-skill"],
        "unknown_skills": ["missing-skill"],
        "disabled_skills": [],
        "allowed_tools": [],
        "configuration_errors": [],
        "failure_reason": "skill_composition_invalid:unknown=missing-skill",
    }
