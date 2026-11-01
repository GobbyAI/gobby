"""Landing authority comes from installed declarations, including for reused seats."""

from typing import Any

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.land_commit import _manages_task
from tests.fixtures.agent_sessions import AgentSessions
from tests.fixtures.isolated_checkout import insert_isolated_machine

pytestmark = pytest.mark.unit


def test_reused_spawned_agent_can_land_its_current_claim(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    sessions = AgentSessions(temp_db, project_id)
    parent = sessions.add("synthetic-parent")
    caller = sessions.add(
        "renamed-landing-seat",
        capabilities=("land_tasks",),
        definition_fields={"task_land_scope": {"own_claim_only": True}},
    )
    tasks = LocalTaskManager(temp_db)
    previous = tasks.create_task(
        project_id, "Original assignment", validation_criteria="Follow the landing grant."
    )
    current = tasks.create_task(
        project_id, "Current assignment", validation_criteria="Follow the landing grant."
    )
    run = LocalAgentRunManager(temp_db).create(
        parent, "test", "Reusable seat", child_session_id=caller, task_id=previous.id
    )
    temp_db.execute(
        "UPDATE sessions SET agent_run_id = %s, agent_depth = 1 WHERE id = %s", (run.id, caller)
    )
    current = tasks.claim_task(current.id, caller)

    assert _manages_task(temp_db, current, caller)
    assert not _manages_task(temp_db, previous, caller)


@pytest.mark.parametrize(
    ("capabilities", "scope", "allowed"),
    [
        (("land_tasks",), {"categories": ["docs"]}, False),
        (("land_tasks",), {"task_types": ["epic"]}, False),
        (("land_tasks",), {"own_claim_only": True}, False),
        ((), {}, False),
        (("land_tasks",), {}, True),
    ],
)
def test_root_landing_obeys_declared_scope_and_capability(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    capabilities: tuple[str, ...],
    scope: dict[str, object],
    allowed: bool,
) -> None:
    project_id = sample_project["id"]
    caller = AgentSessions(temp_db, project_id).add(
        "arbitrary-root-seat",
        capabilities=capabilities,
        definition_fields={"task_land_scope": scope},
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id, "Candidate", category="code", validation_criteria="Follow the landing grant."
    )

    assert _manages_task(temp_db, task, caller) is allowed


def test_remote_caller_cannot_land_in_the_daemons_checkout(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    caller = AgentSessions(temp_db, sample_project["id"]).add(
        "remote-seat", capabilities=("land_tasks",)
    )
    temp_db.execute(
        "UPDATE sessions SET machine_id = %s WHERE id = %s",
        (insert_isolated_machine(temp_db), caller),
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"], "Local candidate", validation_criteria="Local authority only."
    )

    assert not _manages_task(temp_db, task, caller)


@pytest.mark.parametrize("cross_project_authority", [False, True])
def test_foreign_landing_preserves_declared_lifecycle_authority(
    temp_db: HubDatabase, sample_project: dict[str, Any], cross_project_authority: bool
) -> None:
    project_id = sample_project["id"]
    AgentSessions(temp_db, project_id).add("local-target-session")
    other = LocalProjectManager(temp_db).create("coordinating-project")
    capabilities = (
        ("land_tasks", "coordinate_tasks") if cross_project_authority else ("land_tasks",)
    )
    caller = AgentSessions(temp_db, other.id).add("foreign-seat", capabilities=capabilities)
    task = LocalTaskManager(temp_db).create_task(
        project_id, "Local candidate", validation_criteria="Foreign access needs authority."
    )

    assert _manages_task(temp_db, task, caller) is cross_project_authority
