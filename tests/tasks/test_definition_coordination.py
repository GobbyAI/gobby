"""Installed permissions, rather than definition names, authorize coordinators."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks._authorization import require_claim_authority
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.lifecycle_access import is_operator_coordinator
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager


@pytest.mark.parametrize("name", ["dispatcher", "orchestrator", "assistant"])
@pytest.mark.parametrize("capability", [None, "coordinate_tasks", "manage_task_claims"])
def test_coordinator_permissions_come_from_definition(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    name: str,
    capability: str | None,
) -> None:
    sessions = SessionManager(temp_db)
    project = sample_project["id"]
    caller, owner = (
        sessions.register(
            external_id=external_id,
            source="codex",
            project_id=project,
            machine_id=require_machine_id(),
        )
        for external_id in ("coordinator", "owner")
    )
    AgentDefinitionManager(temp_db).create(
        name,
        {
            "name": name,
            "prompts": {"agent": "Coordinate assigned work."},
            "workflows": {"rule_selectors": {"include": []}},
            "capabilities": [capability] if capability else [],
        },
        project_id=project,
    )
    SessionVariableManager(temp_db).set_variable(caller.id, "_agent_type", name)
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project,
        "Owned work",
        claimed_by_session_id=owner.id,
        validation_criteria="Only a definition with claim-management authority may edit this task.",
    )

    assert is_operator_coordinator(tasks, caller) is (capability == "coordinate_tasks")
    with session_context_for_test(caller.id):
        refusal = require_claim_authority(tasks, task, "add_label")
    assert (refusal is None) is (capability == "manage_task_claims")


@pytest.mark.parametrize("restriction", ["child", "foreign_machine", "disabled"])
def test_coordinator_grants_preserve_local_root_and_enabled_boundaries(
    temp_db: HubDatabase, sample_project: dict[str, Any], restriction: str
) -> None:
    sessions = SessionManager(temp_db)
    project = sample_project["id"]
    owner = sessions.register(
        external_id="owner",
        source="codex",
        project_id=project,
        machine_id=require_machine_id(),
    )
    caller = sessions.register(
        external_id="restricted-coordinator",
        source="codex",
        project_id=project,
        machine_id=require_machine_id(),
        parent_session_id=owner.id if restriction == "child" else None,
    )
    definitions = AgentDefinitionManager(temp_db)
    row = definitions.create(
        "dispatcher",
        {
            "name": "dispatcher",
            "prompts": {"agent": "Coordinate assigned work."},
            "workflows": {"rule_selectors": {"include": []}},
            "capabilities": ["coordinate_tasks", "manage_task_claims"],
        },
        project_id=project,
    )
    if restriction == "disabled":
        definitions.update(row.id, enabled=False)
    SessionVariableManager(temp_db).set_variable(caller.id, "_agent_type", "dispatcher")
    if restriction == "foreign_machine":
        caller = replace(caller, machine_id="another-machine")
    tasks = LocalTaskManager(temp_db)
    assert is_operator_coordinator(tasks, caller) is False
