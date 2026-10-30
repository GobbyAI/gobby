"""Installed permissions, rather than definition names, authorize coordinators."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from gobby.agents.team_communications import recipient_policy
from gobby.mcp_proxy.tools.tasks._authorization import require_claim_authority
from gobby.sessions.clear_continuation import stage_clear_attempt, take_clear_handoff_marker
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.lifecycle_access import is_operator_coordinator
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_resolver import session_has_capability_or_operator
from gobby.workflows.condition_helpers_sessions import network_override_allowed
from gobby.workflows.engine.idle_declaration import SET_VARIABLE_TOOL, native_variable_write_error
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
        agent_depth=1 if restriction == "child" else 0,
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


@pytest.mark.parametrize("role", ["operator", "coordinator"])
def test_root_authority_survives_clear_lineage(
    temp_db: HubDatabase, sample_project: dict[str, Any], role: str
) -> None:
    sessions = SessionManager(temp_db)
    project = sample_project["id"]
    predecessor, successor, inbox = (
        sessions.register(
            external_id=name,
            source="codex",
            project_id=project,
            machine_id=require_machine_id(),
        )
        for name in ("predecessor", "successor", "inbox")
    )
    definitions = AgentDefinitionManager(temp_db)
    definitions.create(
        "dispatcher",
        {
            "name": "dispatcher",
            "prompts": {"agent": "Coordinate assigned work."},
            "workflows": {"rule_selectors": {"include": []}},
            "capabilities": [
                "coordinate_tasks",
                "close_without_work",
                "override_spawn_network",
                "send_reports",
                "write_session_variables",
            ],
        },
        project_id=project,
    )
    definitions.create(
        "inbox",
        {
            "name": "inbox",
            "prompts": {"agent": "Receive authorized reports."},
            "workflows": {"rule_selectors": {"include": []}},
            "message_sender_capabilities": ["send_reports"],
        },
        project_id=project,
    )
    variables = SessionVariableManager(temp_db)
    variables.set_variable(inbox.id, "_agent_type", "inbox")
    if role == "coordinator":
        variables.set_variable(successor.id, "_agent_type", "dispatcher")
    attempt = uuid4().hex
    stage_clear_attempt(
        temp_db,
        predecessor.id,
        attempt_id=attempt,
        handoff=build_handoff_payload(current_state="Continue.", next_steps=["Continue."]),
        terminal_context=None,
        chat_context=None,
    )
    assert take_clear_handoff_marker(
        temp_db, predecessor.id, attempt_id=attempt, successor_id=successor.id
    )
    caller = sessions.get(successor.id)
    assert caller is not None and caller.parent_session_id == predecessor.id

    assert session_has_capability_or_operator(temp_db, caller.id, "close_without_work")
    assert network_override_allowed(sessions, caller.id)
    assert (
        native_variable_write_error(
            SET_VARIABLE_TOOL,
            {"name": "sample", "value": True},
            caller.id,
            variables.get_variables(caller.id),
            sessions,
        )
        is None
    )
    assert recipient_policy(temp_db, caller.id, [inbox.id]).withheld == frozenset()
    assert is_operator_coordinator(LocalTaskManager(temp_db), caller) is (role == "coordinator")
