"""Progress reports label actual participants without inferring a coding phase."""

from datetime import UTC, datetime, timedelta

import pytest

from gobby.agents.team_progress import _task_progress
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import AttestationSubmission, record_attestation
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


def test_claim_span_uses_declared_participant_identity(
    temp_db: HubDatabase, sample_project: dict[str, str]
) -> None:
    AgentDefinitionManager(temp_db).create(
        name="cake-baker",
        definition_json={
            "name": "cake-baker",
            "prompts": {"agent": "Bake the cake and record its evidence."},
            "task_attestation_writes": ["baked", "tasted"],
            "workflows": {"rule_selectors": {"include": []}},
        },
        enabled=True,
    )
    session = SessionManager(temp_db).register(
        external_id="cake-progress",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    SessionVariableManager(temp_db).set_variable(session.id, "_agent_type", "cake-baker")
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(sample_project["id"], "Bake a cake")
    claimed = tasks.claim_task(task.id, session.id)
    progress = _task_progress(temp_db, claimed, datetime.now(UTC))
    assert progress is not None
    assert [span.stage for span in progress.stages] == ["cake-baker"]
    assert progress.flags == frozenset()
    assert progress.claim_to_close_seconds is None


@pytest.mark.parametrize("state", ["awaiting", "acknowledged", "expired", "closed"])
async def test_explicit_claim_transfer_reports_identity_activity_and_stalls(
    temp_db: HubDatabase, sample_project: dict[str, str], state: str
) -> None:
    sessions = AgentSessions(temp_db, sample_project["id"])
    owner = sessions.add("baker", capabilities=("manage_task_claims",))
    recipient = sessions.add("taster", ["taste-check"], results=["balanced"])
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Bake and taste a cake")
    manager.claim_task(task.id, owner)
    with session_context_for_test(owner):
        handed = await create_task_registry(manager).call(
            "hand_off_task_claim",
            {
                "task_id": task.id,
                "holder_session_ref": owner,
                "recipient_session_ref": recipient,
                "reason": "Taste the completed cake.",
            },
        )
    assert handed.get("success") is True, handed
    start = datetime.now(UTC) - timedelta(minutes=20)
    temp_db.execute(
        "UPDATE task_lifecycle_events SET created_at = %s "
        "WHERE task_id = %s AND to_state = 'claim:started'",
        (start, task.id),
    )
    temp_db.execute(
        "UPDATE task_lifecycle_events SET created_at = %s "
        "WHERE task_id = %s AND to_state = 'claim:handed_off'",
        (start + timedelta(minutes=1), task.id),
    )
    if state == "acknowledged":
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=recipient,
            submission=AttestationSubmission(
                kind="taste-check", result="balanced", summary="The cake has balanced sweetness."
            ),
        )
    elif state == "expired":
        temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (recipient,))
    elif state == "closed":
        manager.close_task(task.id, reason="done")
    progress = _task_progress(temp_db, manager.get_task(task.id), datetime.now(UTC))
    assert progress is not None
    assert [span.stage for span in progress.stages] == ["baker", "taster"]
    assert progress.stages[0].seconds == 60
    if state in {"acknowledged", "closed"}:
        assert progress.flags == frozenset()
    else:
        assert "handoff_unacknowledged" in progress.flags
        assert ("holder_not_live" in progress.flags) is (state == "expired")
    assert (progress.claim_to_close_seconds is not None) is (state == "closed")
