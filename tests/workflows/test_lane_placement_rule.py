"""Tests for the bundled refuse-task-outside-lane rule and its lane helpers (#23814)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.workflows.definitions import RuleDefinitionBody, split_rule_definition_data
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.lane_placement import open_lane_epic_refs

pytestmark = pytest.mark.unit

SESSION_ID = "33333333-3333-4333-8333-333333333333"
RULE_FILE = (
    Path(__file__).parents[2]
    / "src/gobby/install/shared/workflows/rules/task-enforcement/refuse-task-outside-lane.yaml"
)
REFUSAL = "Retry under a lane"


@pytest.fixture
def tasks(temp_db: HubDatabase) -> LocalTaskManager:
    document = yaml.safe_load(RULE_FILE.read_text())
    for name, rule_data in document["rules"].items():
        body_data, metadata = split_rule_definition_data(rule_data)
        RuleDefinitionManager(temp_db).create(
            name=name,
            definition_json=RuleDefinitionBody.model_validate(body_data).model_dump_json(),
            priority=metadata["priority"],
            enabled=metadata["enabled"],
            tags=document["tags"],
        )
    return LocalTaskManager(temp_db)


@pytest.fixture
def project_id(sample_project: dict[str, Any]) -> str:
    return str(sample_project["id"])


def _epic(tasks: LocalTaskManager, project_id: str, title: str, **kwargs: Any) -> Task:
    return tasks.create_task(project_id=project_id, title=title, task_type="epic", **kwargs)


def _leaf(tasks: LocalTaskManager, project_id: str, parent: Task) -> Task:
    return tasks.create_task(
        project_id=project_id,
        title=f"Leaf under {parent.title}",
        parent_task_id=parent.id,
        validation_criteria="Leaf exists",
    )


async def _decide(
    tasks: LocalTaskManager,
    project_id: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    server: str = "gobby-tasks",
    variables: dict[str, Any] | None = None,
    proxy_tool: str = "mcp__gobby__call_tool",
) -> HookResponse:
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        project_id=project_id,
        data={
            "tool_name": proxy_tool,
            "tool_input": {"server_name": server, "tool_name": tool, "arguments": arguments},
        },
    )
    engine = RuleEngine(tasks.db, task_manager=tasks)
    return await engine.evaluate(event, session_id=SESSION_ID, variables=dict(variables or {}))


def _refused(response: HookResponse) -> bool:
    return response.decision == "block" and REFUSAL in (response.reason or "")


@pytest.mark.asyncio
async def test_task_outside_every_lane_is_refused_with_open_lanes_named(
    tasks: LocalTaskManager, project_id: str
) -> None:
    lane = _epic(tasks, project_id, "Lane A", labels=["lane"])
    loose = _epic(tasks, project_id, "Loose epic")
    tasks.close_task(_epic(tasks, project_id, "Closed lane", labels=["lane"]).id)
    before = len(tasks.list_tasks(project_id=project_id, limit=500))

    for arguments in ({"title": "No parent"}, {"title": "Loose", "parent_task_id": loose.id}):
        response = await _decide(tasks, project_id, "create_task", arguments)
        assert _refused(response), arguments
        assert f"#{lane.seq_num} Lane A" in (response.reason or "")
        assert "Closed lane" not in (response.reason or "")

    assert len(tasks.list_tasks(project_id=project_id, limit=500)) == before


@pytest.mark.asyncio
async def test_task_under_an_open_lane_or_its_descendants_is_created(
    tasks: LocalTaskManager, project_id: str
) -> None:
    lane = _epic(tasks, project_id, "Lane A", labels=["lane"])
    child_epic = _epic(tasks, project_id, "Plan epic", parent_task_id=lane.id)
    leaf = _leaf(tasks, project_id, child_epic)

    for parent_ref in (f"#{lane.seq_num}", child_epic.id, str(leaf.seq_num)):
        response = await _decide(
            tasks, project_id, "create_task", {"title": "Inside", "parent_task_id": parent_ref}
        )
        assert response.decision == "allow", parent_ref


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "refused"),
    [
        ({"title": "Lane 9", "task_type": "epic", "labels": ["lane"]}, False),
        ({"title": "Unlabeled epic", "task_type": "epic"}, True),
        ({"title": "Lane-labeled leaf", "task_type": "task", "labels": ["lane"]}, True),
    ],
)
async def test_only_a_lane_epic_is_created_without_a_parent(
    tasks: LocalTaskManager, project_id: str, arguments: dict[str, Any], refused: bool
) -> None:
    _epic(tasks, project_id, "Lane A", labels=["lane"])

    response = await _decide(tasks, project_id, "create_task", arguments)

    assert _refused(response) is refused


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variables",
    [
        {"_agent_type": "orchestrator"},
        {"_agent_type": "assistant"},
        {"_agent_type": "developer"},
        {"_agent_type": "default"},
        {},
    ],
)
async def test_no_session_role_or_agent_type_is_exempt(
    tasks: LocalTaskManager, project_id: str, variables: dict[str, Any]
) -> None:
    _epic(tasks, project_id, "Lane A", labels=["lane"])

    response = await _decide(
        tasks, project_id, "create_task", {"title": "Loose"}, variables=variables
    )

    assert _refused(response), variables


@pytest.mark.asyncio
async def test_project_without_an_open_lane_creates_tasks_as_before(
    tasks: LocalTaskManager, project_id: str, project_manager: LocalProjectManager
) -> None:
    unlaned = await _decide(tasks, project_id, "create_task", {"title": "No lanes yet"})
    assert unlaned.decision == "allow"

    closed_lane = _epic(tasks, project_id, "Closed lane", labels=["lane"])
    tasks.close_task(closed_lane.id)
    assert (await _decide(tasks, project_id, "create_task", {"title": "Loose"})).decision == (
        "allow"
    )

    _epic(tasks, project_id, "Lane A", labels=["lane"])
    other = project_manager.create("lane-free-project")
    elsewhere = {"title": "Personal", "project": other.name}
    assert (await _decide(tasks, project_id, "create_task", elsewhere)).decision == "allow"
    assert open_lane_epic_refs(tasks, other.id) == ""


@pytest.mark.asyncio
async def test_expansion_and_plan_import_check_their_target_parent(
    tasks: LocalTaskManager, project_id: str
) -> None:
    lane = _epic(tasks, project_id, "Lane A", labels=["lane"])
    inside = _epic(tasks, project_id, "Plan epic", parent_task_id=lane.id)
    outside = _epic(tasks, project_id, "Loose epic")
    ops = "gobby-tasks-ops"

    expand_inside = {"task_id": f"#{inside.seq_num}", "plan_file": ".gobby/plans/x.md"}
    assert (
        await _decide(tasks, project_id, "start_expansion_run", expand_inside, server=ops)
    ).decision == "allow"
    expand_outside = {"task_id": f"#{outside.seq_num}"}
    assert _refused(
        await _decide(tasks, project_id, "start_expansion_run", expand_outside, server=ops)
    )
    plan_import = {"input_ref": ".gobby/plans/x.md"}
    assert _refused(await _decide(tasks, project_id, "build_task", plan_import, server=ops))
    build_existing = {"input_ref": f"#{inside.seq_num}"}
    assert (
        await _decide(tasks, project_id, "build_task", build_existing, server=ops)
    ).decision == "allow"


@pytest.mark.asyncio
async def test_schema_lookup_for_create_task_is_not_a_creation(
    tasks: LocalTaskManager, project_id: str
) -> None:
    _epic(tasks, project_id, "Lane A", labels=["lane"])

    response = await _decide(
        tasks, project_id, "create_task", {}, proxy_tool="mcp__gobby__get_tool_schema"
    )

    assert response.decision == "allow"
