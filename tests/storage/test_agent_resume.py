from __future__ import annotations

from typing import Any

from gobby.storage.agent_resume import (
    finalize_daemon_resume,
    register_daemon_resume_waiter,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.storage.session_lifecycle import rebind_agent_run
from gobby.storage.sessions import SessionManager


def _seed_parked_run(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> tuple[SessionManager, LocalAgentRunManager, Any, Any, Any]:
    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="parent-resume",
        machine_id="machine-1",
        source="test",
        project_id=sample_project["id"],
    )
    child = sessions.register(
        external_id="child-resume",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        parent_session_id=parent.id,
    )
    runs = LocalAgentRunManager(temp_db)
    original = runs.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="work",
        resume_metadata_json={"provider_native_session_id": "native-1"},
    )
    temp_db.execute(
        "UPDATE sessions SET agent_run_id = %s WHERE id = %s",
        (original.id, child.id),
    )
    runs.start(original.id)
    parked = runs.cancel(original.id, terminal_reason="daemon_stop")
    assert parked is not None
    return sessions, runs, parent, child, parked


def _seed_successor(
    temp_db: Any,
    runs: LocalAgentRunManager,
    *,
    parent_id: str,
    child_id: str,
    original_id: str,
) -> Any:
    successor = runs.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="codex",
        prompt="continue",
        resume_metadata_json={
            "resumed_from_run_id": original_id,
            "daemon_stop_resume_phase": "runtime_persisted",
        },
    )
    assert rebind_agent_run(
        temp_db,
        session_id=child_id,
        expected_run_id=original_id,
        new_run_id=successor.id,
        workflow_name=None,
    )
    runs.start(successor.id)
    return successor


def test_finalize_daemon_resume_transfers_session_and_subscribers(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    sessions, runs, parent, child, original = _seed_parked_run(temp_db, sample_project)
    subscribers = CompletionSubscriberManager(temp_db)
    subscribers.add_completion_subscriber(original.id, parent.id)
    successor = _seed_successor(
        temp_db,
        runs,
        parent_id=parent.id,
        child_id=child.id,
        original_id=original.id,
    )

    result = finalize_daemon_resume(
        temp_db,
        original_run_id=original.id,
        successor_run_id=successor.id,
        child_session_id=child.id,
    )

    original_after = runs.get(original.id)
    successor_after = runs.get(successor.id)
    child_after = sessions.get(child.id)
    assert result.subscriber_session_ids == (parent.id,)
    assert original_after is not None
    assert original_after.resume_metadata_json is not None
    assert original_after.resume_metadata_json["daemon_stop_resume_consumed_by_run_id"] == (
        successor.id
    )
    assert successor_after is not None
    assert successor_after.resume_metadata_json is not None
    assert successor_after.resume_metadata_json["daemon_stop_resume_phase"] == "finalized"
    assert child_after is not None
    assert child_after.status == "active"
    assert child_after.agent_run_id == successor.id
    assert subscribers.get_completion_subscribers(original.id) == []
    assert subscribers.get_completion_subscribers(successor.id) == [parent.id]


def test_waiter_follows_successor_under_resume_fence(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    _sessions, runs, parent, child, original = _seed_parked_run(temp_db, sample_project)
    first = register_daemon_resume_waiter(
        temp_db,
        run_id=original.id,
        subscriber_session_id=parent.id,
    )
    assert first.run_id == original.id
    assert first.recovery_pending is True

    successor = _seed_successor(
        temp_db,
        runs,
        parent_id=parent.id,
        child_id=child.id,
        original_id=original.id,
    )
    finalize_daemon_resume(
        temp_db,
        original_run_id=original.id,
        successor_run_id=successor.id,
        child_session_id=child.id,
    )

    followed = register_daemon_resume_waiter(
        temp_db,
        run_id=original.id,
        subscriber_session_id=parent.id,
    )
    assert followed.run_id == successor.id
    assert followed.recovery_pending is False
