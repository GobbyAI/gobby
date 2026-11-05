"""Task-label delegation uses stored grants and admitted synthetic teams."""

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.agents.team_admission_records import TeamBinding
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.pipeline_state import StepStatus
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


@dataclass
class LabelTeam:
    tasks: LocalTaskManager
    task: Task
    caller: str
    registry: InternalToolRegistry


def _team(db: HubDatabase, project: str, *, granted: bool = True) -> LabelTeam:
    tasks = LocalTaskManager(db)
    task = tasks.create_task(
        project,
        "Fixture team task",
        labels=["fixture-existing"],
        validation_criteria="Fixture labels remain intact.",
    )
    seats = AgentSessions(db, project)
    caller = seats.add(
        "fixture-supervisor",
        capabilities=("add_team_task_labels",) if granted else (),
        definition_fields={
            "workflows": {"rule_selectors": {"include": []}, "variables": {"team_manager": True}}
        },
    )
    owner = seats.add("fixture-worker")
    executions = LocalPipelineExecutionManager(db, project)
    execution = executions.create_execution(
        "fixture-label-team",
        definition_json=json.dumps(
            {"name": "fixture-label-team", "steps": [{"id": "admit", "exec": "true"}]}
        ),
    )
    parent = SessionManager(db).register(
        external_id=f"pipeline-{execution.id}",
        source="pipeline",
        machine_id=require_machine_id(),
        project_id=project,
    )
    worktree = LocalWorktreeManager(db).create(
        project, "fixture-branch", f"/tmp/fixture-{execution.id}", task_id=task.id
    )
    binding = TeamBinding(
        execution.id,
        task.id,
        project,
        "fixture-branch",
        "main",
        worktree.id,
        worktree.worktree_path,
    )
    step = executions.create_step_execution(execution.id, "admit")
    executions.update_step_execution(
        step.id, status=StepStatus.COMPLETED, output_json=json.dumps({"team": asdict(binding)})
    )
    runs = LocalAgentRunManager(db)
    for child, name in ((caller, "fixture-supervisor"), (owner, "fixture-worker")):
        run = runs.create(
            parent.id,
            "codex",
            "Fixture work",
            agent_name=name,
            task_id=task.id,
            child_session_id=child,
        )
        runs.start(run.id)
        db.execute(
            "UPDATE sessions SET parent_session_id = %s, agent_run_id = %s, agent_depth = 1 "
            "WHERE id = %s",
            (parent.id, run.id, child),
        )
    tasks.claim_task(task.id, owner)
    return LabelTeam(tasks, tasks.get_task(task.id), caller, create_task_registry(tasks))


def _call(team: LabelTeam, name: str, **kwargs: Any) -> dict[str, Any]:
    tool = team.registry.get_tool(name)
    assert tool is not None
    with session_context_for_test(team.caller):
        result = tool(task_id=team.task.id, **kwargs)
    assert isinstance(result, dict)
    return result


def test_grant_allows_labelling_a_sibling_claim(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, str(sample_project["id"]))
    result = _call(team, "add_label", label="fixture-added")
    assert result.get("success") is True
    assert team.tasks.get_task(team.task.id).labels == ["fixture-existing", "fixture-added"]
    assert (
        team.tasks.get_task(team.task.id).claimed_by_session_id == team.task.claimed_by_session_id
    )


@pytest.mark.parametrize("labels", [[], ["fixture-added"], ["fixture-existing", "fixture-added"]])
def test_scoped_updates_preserve_existing_labels(
    temp_db: HubDatabase, sample_project: dict[str, Any], labels: list[str]
) -> None:
    team = _team(temp_db, str(sample_project["id"]))
    result = _call(team, "update_task", labels=labels)
    assert "error" not in result
    assert team.tasks.get_task(team.task.id).labels == list(
        dict.fromkeys(["fixture-existing", *labels])
    )


@pytest.mark.parametrize("tool", ["add_label", "update_task"])
def test_team_membership_does_not_replace_a_declared_grant(
    temp_db: HubDatabase, sample_project: dict[str, Any], tool: str
) -> None:
    team = _team(temp_db, str(sample_project["id"]), granted=False)
    arguments: dict[str, Any] = (
        {"label": "fixture-added"} if tool == "add_label" else {"labels": ["fixture-added"]}
    )
    result = _call(team, tool, **arguments)
    assert "error" in result
    assert team.tasks.get_task(team.task.id).labels == ["fixture-existing"]


@pytest.mark.parametrize("tool", ["add_label", "update_task"])
def test_grant_cannot_edit_a_different_teams_claim(
    temp_db: HubDatabase, sample_project: dict[str, Any], tool: str
) -> None:
    project = str(sample_project["id"])
    team = _team(temp_db, project)
    foreign = team.tasks.create_task(
        project, "Other fixture task", labels=["fixture-other"], validation_criteria="Other work."
    )
    outsider = (
        SessionManager(temp_db)
        .register("fixture-outsider", require_machine_id(), "test", project_id=project)
        .id
    )
    team.tasks.claim_task(foreign.id, outsider)
    arguments: dict[str, Any] = (
        {"label": "fixture-added"} if tool == "add_label" else {"labels": ["fixture-added"]}
    )
    result = _call(replace(team, task=foreign), tool, **arguments)
    assert "error" in result
    assert team.tasks.get_task(foreign.id).labels == ["fixture-other"]


def test_label_grant_does_not_authorize_other_metadata(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    team = _team(temp_db, str(sample_project["id"]))
    result = _call(team, "update_task", labels=["fixture-added"], title="Changed fixture title")
    assert "error" in result
    current = team.tasks.get_task(team.task.id)
    assert current.title == team.task.title
    assert current.labels == ["fixture-existing"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("granted", "scope", "patch", "allowed"),
    [
        (True, "team", {"labels": ["fixture-added"]}, True),
        (True, "team", {"labels": [], "title": None}, True),
        (False, "team", {"labels": ["fixture-added"]}, False),
        (True, "foreign", {"labels": ["fixture-added"]}, False),
        (True, "team", {"labels": [], "title": "Other title"}, False),
        (True, "team", {"labels": "fixture-added"}, False),
        (True, "team", {"labels": [3]}, False),
        (True, "team", {}, False),
        (True, "missing", {"labels": []}, False),
    ],
)
async def test_synthetic_rule_respects_label_grant_and_scope(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    granted: bool,
    scope: str,
    patch: dict[str, Any],
    allowed: bool,
) -> None:
    project = str(sample_project["id"])
    team = _team(temp_db, project, granted=granted)
    definitions = AgentDefinitionManager(temp_db)
    caller = definitions.get_by_name("fixture-supervisor", project)
    assert caller is not None
    body = dict(caller.definition_json)
    body["workflows"]["rule_selectors"]["include"] = ["name:fixture-label-update"]
    definitions.update(caller.id, definition_json=body)
    RuleDefinitionManager(temp_db).create(
        "fixture-label-update",
        {
            "event": "before_tool",
            "when": "not task_label_update_allowed("
            "event.metadata.get('_platform_session_id'), tool_input)",
            "effects": [
                {
                    "type": "block",
                    "mcp_tools": ["gobby-tasks:update_task"],
                    "reason": "Synthetic label scope refusal",
                }
            ],
        },
    )
    target = team.task
    if scope == "foreign":
        target = team.tasks.create_task(project, "Other fixture task")
    arguments = {"task_id": "#999999" if scope == "missing" else f"#{target.seq_num}", **patch}
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-tasks",
            "tool_name": "update_task",
            "arguments": arguments,
        },
    }
    normalize_tool_fields(data)
    response = await RuleEngine(temp_db, task_manager=team.tasks).evaluate(
        HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=team.caller,
            project_id=project,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data=data,
            metadata={"_platform_session_id": team.caller},
        ),
        session_id=team.caller,
        variables={"_agent_type": "fixture-supervisor"},
    )
    assert response.decision == ("allow" if allowed else "block")
    if not allowed:
        assert response.reason is not None and "Synthetic label scope refusal" in response.reason
