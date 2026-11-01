"""Authenticated evidence writer boundaries through the public task registry."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import list_attestations
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.agent_sessions import AgentSessions
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("refusal", ["no_grant", "disabled", "different_project"])
async def test_unauthorized_evidence_cannot_mutate_history(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path, refusal: str
) -> None:
    project_id = sample_project["id"]
    author = AgentSessions(temp_db, project_id).add(
        "arbitrary-writer", [] if refusal == "no_grant" else ["inspect"]
    )
    if refusal == "disabled":
        definitions = AgentDefinitionManager(temp_db)
        definition = definitions.get_by_name("arbitrary-writer", project_id)
        assert definition is not None
        definitions.update(definition.id, enabled=False)
    elif refusal == "different_project":
        project_id = install_isolated_checkout_project(
            temp_db, tmp_path / "foreign", name="foreign", machine_id=require_machine_id()
        ).project.id
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id, "Inspect the deliverable")
    listener = MagicMock()
    manager.add_change_listener(listener)
    with session_context_for_test(author):
        result = await create_task_registry(manager).call(
            "record_attestation",
            {"task_id": task.id, "kind": "inspect", "result": "approved", "summary": "Checked."},
        )
    assert "error" in result, result
    assert list_attestations(temp_db, task.id) == []
    listener.assert_not_called()


async def test_exact_keyed_replay_notifies_only_on_creation(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    author = AgentSessions(temp_db, sample_project["id"]).add("inspector", ["inspect"])
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Inspect the deliverable")
    listener = MagicMock()
    manager.add_change_listener(listener)
    registry = create_task_registry(manager)
    arguments = {
        "task_id": task.id,
        "kind": "inspect",
        "result": "approved",
        "summary": "Checked.",
        "idempotency_key": "inspection-1",
    }
    with session_context_for_test(author):
        first = await registry.call("record_attestation", arguments)
        replay = await registry.call("record_attestation", arguments)
    assert first["created"] is True
    assert replay["created"] is False
    assert replay["record"] == first["record"]
    listener.assert_called_once_with()
