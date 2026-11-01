"""Claim coordination is declared authority, independent of team membership."""

from pathlib import Path
from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import _claim_release, create_task_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_sessions import AgentSessions
from tests.fixtures.isolated_checkout import insert_isolated_machine

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("operation", ["release_task_claim", "hand_off_task_claim"])
async def test_dirty_claim_refusal_preserves_owner_and_attribution_atomically(
    temp_db: HubDatabase, sample_git_project: dict[str, Any], operation: str
) -> None:
    sessions = AgentSessions(temp_db, sample_git_project["id"])
    coordinator = sessions.add("coordinator", capabilities=("manage_task_claims",))
    owner = sessions.add("worker")
    recipient = sessions.add("successor")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_git_project["id"], "Keep dirty work with its owner")
    manager.claim_task(task.id, owner)
    repo = Path(sample_git_project["repo_path"])
    (repo / "owned.py").write_text("value = 1\n")
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        owner,
        {
            "task_edited_files": {task.id: ["owned.py"]},
            "task_edited_file_checkouts": {task.id: {str(repo): ["owned.py"]}},
            "session_dirty_files": ["owned.py"],
            "session_dirty_file_checkouts": {str(repo): ["owned.py"]},
        },
    )
    before = variables.get_variables(owner)
    arguments = {
        "task_id": task.id,
        "holder_session_ref": owner,
        "reason": "Attempt explicit ownership coordination.",
    }
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = recipient
    with session_context_for_test(coordinator):
        result = await create_task_registry(manager).call(operation, arguments)
    assert result["error_code"] == "task_has_uncommitted_changes", result
    assert manager.get_task(task.id).claimed_by_session_id == owner
    assert variables.get_variables(owner) == before
    assert (
        temp_db.fetchall(
            "SELECT id FROM task_lifecycle_events WHERE task_id = %s "
            "AND to_state = 'claim:handed_off'",
            (task.id,),
        )
        == []
    )


async def test_claim_handoff_needs_no_team_binding(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = AgentSessions(temp_db, sample_project["id"])
    coordinator = sessions.add("custom-coordinator", capabilities=("manage_task_claims",))
    holder = sessions.add("custom-worker")
    recipient = sessions.add("custom-successor")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Transfer ownership", validation_criteria="Claim moves atomically."
    )
    manager.claim_task(task.id, holder)
    registry = create_task_registry(manager)

    with session_context_for_test(coordinator):
        result = await registry.call(
            "hand_off_task_claim",
            {
                "task_id": task.id,
                "holder_session_ref": holder,
                "recipient_session_ref": recipient,
                "reason": "Continue the same task in a different seat.",
            },
        )

    assert result.get("success") is True, result
    assert manager.get_task(task.id).claimed_by_session_id == recipient


@pytest.mark.parametrize("operation", ["release_task_claim", "hand_off_task_claim"])
async def test_claim_management_respects_the_declared_task_scope(
    temp_db: HubDatabase, sample_project: dict[str, Any], operation: str
) -> None:
    sessions = AgentSessions(temp_db, sample_project["id"])
    coordinator = sessions.add(
        "scoped-coordinator",
        capabilities=("manage_task_claims",),
        definition_fields={"task_claim_scope": {"categories": ["docs"]}},
    )
    holder = sessions.add("worker")
    recipient = sessions.add("successor")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Code task", category="code", validation_criteria="Preserve claim."
    )
    manager.claim_task(task.id, holder)
    arguments = {"task_id": task.id, "holder_session_ref": holder, "reason": "Scope check."}
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = recipient

    with session_context_for_test(coordinator):
        result = await create_task_registry(manager).call(operation, arguments)

    assert "error" in result
    assert manager.get_task(task.id).claimed_by_session_id == holder


@pytest.mark.parametrize("operation", ["release_task_claim", "hand_off_task_claim"])
async def test_claim_scope_is_rechecked_after_ownership_changes(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    sessions = AgentSessions(temp_db, sample_project["id"])
    coordinator = sessions.add(
        "own-claim-coordinator",
        capabilities=("manage_task_claims",),
        definition_fields={"task_claim_scope": {"own_claim_only": True}},
    )
    holder = sessions.add("next-owner")
    recipient = sessions.add("subsequent-owner")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Changing ownership",
        validation_criteria="Do not release a foreign claim.",
    )
    manager.claim_task(task.id, coordinator)
    original = _claim_release._may_manage_claim
    changed = False

    def change_owner_after_admission(ctx: RegistryContext, caller: Session, task_id: str) -> bool:
        nonlocal changed
        allowed = original(ctx, caller, task_id)
        if not changed:
            changed = True
            manager.claim_task(task_id, holder, force=True)
        return allowed

    monkeypatch.setattr(_claim_release, "_may_manage_claim", change_owner_after_admission)
    arguments = {"task_id": task.id, "holder_session_ref": holder, "reason": "Race check."}
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = recipient
    with session_context_for_test(coordinator):
        result = await create_task_registry(manager).call(operation, arguments)

    assert changed
    assert "error" in result
    assert manager.get_task(task.id).claimed_by_session_id == holder


@pytest.mark.parametrize("operation", ["release_task_claim", "hand_off_task_claim"])
async def test_remote_cohort_cannot_use_local_dirty_file_checks(
    temp_db: HubDatabase, sample_project: dict[str, Any], operation: str
) -> None:
    sessions = AgentSessions(temp_db, sample_project["id"])
    coordinator = sessions.add("remote-coordinator", capabilities=("manage_task_claims",))
    holder = sessions.add("remote-worker")
    recipient = sessions.add("remote-successor")
    machine = insert_isolated_machine(temp_db)
    for session_id in (coordinator, holder, recipient):
        temp_db.execute("UPDATE sessions SET machine_id = %s WHERE id = %s", (machine, session_id))
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"], "Remote work", validation_criteria="Preserve the remote claim."
    )
    manager.claim_task(task.id, holder)
    arguments = {"task_id": task.id, "holder_session_ref": holder, "reason": "Remote check."}
    if operation == "hand_off_task_claim":
        arguments["recipient_session_ref"] = recipient

    with session_context_for_test(coordinator):
        result = await create_task_registry(manager).call(operation, arguments)

    assert "error" in result
    assert manager.get_task(task.id).claimed_by_session_id == holder
