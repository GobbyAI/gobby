from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.storage.agent_resume import (
    claim_daemon_stop_orphan_reap,
    finalize_daemon_resume,
    increment_daemon_resume_failure_count,
    register_daemon_resume_waiter,
    rollback_prepared_daemon_resume,
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


def _seed_prepared_successor(
    temp_db: Any,
    runs: LocalAgentRunManager,
    *,
    parent_id: str,
    child_id: str,
    original_id: str,
    phase: str = "prepared",
) -> Any:
    """Create a CAS-rebound successor left pending in the given resume phase."""
    successor = runs.create(
        parent_session_id=parent_id,
        child_session_id=child_id,
        provider="codex",
        prompt="continue",
        resume_metadata_json={
            "resumed_from_run_id": original_id,
            "daemon_stop_resume_phase": phase,
        },
    )
    assert rebind_agent_run(
        temp_db,
        session_id=child_id,
        expected_run_id=original_id,
        new_run_id=successor.id,
        workflow_name=None,
    )
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


def test_finalize_daemon_resume_repeat_call_is_idempotent(
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

    first = finalize_daemon_resume(
        temp_db,
        original_run_id=original.id,
        successor_run_id=successor.id,
        child_session_id=child.id,
    )
    original_between = runs.get(original.id)
    assert original_between is not None
    assert original_between.resume_metadata_json is not None
    consumed_at = original_between.resume_metadata_json["daemon_stop_resume_consumed_at"]

    second = finalize_daemon_resume(
        temp_db,
        original_run_id=original.id,
        successor_run_id=successor.id,
        child_session_id=child.id,
    )

    assert first.already_finalized is False
    assert second.already_finalized is True
    assert second.subscriber_session_ids == (parent.id,)
    original_after = runs.get(original.id)
    assert original_after is not None
    assert original_after.resume_metadata_json is not None
    assert original_after.resume_metadata_json["daemon_stop_resume_consumed_at"] == consumed_at
    assert original_after.resume_metadata_json["daemon_stop_resume_consumed_by_run_id"] == (
        successor.id
    )
    child_after = sessions.get(child.id)
    assert child_after is not None
    assert child_after.status == "active"
    assert child_after.agent_run_id == successor.id
    assert subscribers.get_completion_subscribers(original.id) == []
    assert subscribers.get_completion_subscribers(successor.id) == [parent.id]


def test_rollback_prepared_daemon_resume_restores_binding_before_delete(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    sessions, runs, parent, child, original = _seed_parked_run(temp_db, sample_project)
    successor = _seed_prepared_successor(
        temp_db,
        runs,
        parent_id=parent.id,
        child_id=child.id,
        original_id=original.id,
    )

    assert rollback_prepared_daemon_resume(
        temp_db,
        original_run_id=original.id,
        successor_run_id=successor.id,
        child_session_id=child.id,
    )

    # sessions.agent_run_id carries ON DELETE SET NULL: the binding can only
    # read back as the original if the CAS restore ran before the delete.
    child_after = sessions.get(child.id)
    assert child_after is not None
    assert child_after.agent_run_id == original.id
    assert runs.get(successor.id) is None
    original_after = runs.get(original.id)
    assert original_after is not None
    metadata = original_after.resume_metadata_json or {}
    assert not metadata.get("daemon_stop_resume_consumed_at")
    parked = runs.list_parked_non_task_resume_candidates(max_age_hours=24)
    assert [run.id for run in parked] == [original.id]


def test_rollback_prepared_daemon_resume_refuses_launch_requested_successor(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    sessions, runs, parent, child, original = _seed_parked_run(temp_db, sample_project)
    successor = _seed_prepared_successor(
        temp_db,
        runs,
        parent_id=parent.id,
        child_id=child.id,
        original_id=original.id,
        phase="launch_requested",
    )

    assert not rollback_prepared_daemon_resume(
        temp_db,
        original_run_id=original.id,
        successor_run_id=successor.id,
        child_session_id=child.id,
    )

    child_after = sessions.get(child.id)
    assert child_after is not None
    assert child_after.agent_run_id == successor.id
    assert runs.get(successor.id) is not None


def test_waiter_registered_with_pending_successor_rides_finalization(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    sessions, runs, parent, child, original = _seed_parked_run(temp_db, sample_project)
    waiter = sessions.register(
        external_id="waiter-resume",
        machine_id="machine-1",
        source="test",
        project_id=sample_project["id"],
    )
    successor = _seed_successor(
        temp_db,
        runs,
        parent_id=parent.id,
        child_id=child.id,
        original_id=original.id,
    )

    pending = register_daemon_resume_waiter(
        temp_db,
        run_id=original.id,
        subscriber_session_id=waiter.id,
    )
    assert pending.run_id == original.id
    assert pending.recovery_pending is True
    assert pending.subscriber_inserted is True

    result = finalize_daemon_resume(
        temp_db,
        original_run_id=original.id,
        successor_run_id=successor.id,
        child_session_id=child.id,
    )

    subscribers = CompletionSubscriberManager(temp_db)
    assert waiter.id in result.subscriber_session_ids
    assert subscribers.get_completion_subscribers(original.id) == []
    assert waiter.id in subscribers.get_completion_subscribers(successor.id)

    followed = register_daemon_resume_waiter(
        temp_db,
        run_id=original.id,
        subscriber_session_id=waiter.id,
    )
    assert followed.run_id == successor.id
    assert followed.recovery_pending is False
    assert followed.subscriber_inserted is False


def test_orphan_reap_claim_refuses_consumed_original(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    _sessions, runs, _parent, child, original = _seed_parked_run(temp_db, sample_project)
    merged = runs.merge_resume_metadata(
        original.id,
        {
            "daemon_stop_resume_consumed_at": "2026-07-01T00:00:00+00:00",
            "daemon_stop_resume_consumed_by_run_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
        },
    )
    assert merged is not None

    assert not claim_daemon_stop_orphan_reap(
        temp_db,
        original_run_id=original.id,
        child_session_id=child.id,
    )

    original_after = runs.get(original.id)
    assert original_after is not None
    assert original_after.resume_metadata_json is not None
    assert "daemon_stop_orphan_reap_started_at" not in original_after.resume_metadata_json


def test_orphan_reap_claim_refuses_session_owned_by_successor(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    _sessions, runs, parent, child, original = _seed_parked_run(temp_db, sample_project)
    _seed_successor(
        temp_db,
        runs,
        parent_id=parent.id,
        child_id=child.id,
        original_id=original.id,
    )

    assert not claim_daemon_stop_orphan_reap(
        temp_db,
        original_run_id=original.id,
        child_session_id=child.id,
    )

    original_after = runs.get(original.id)
    assert original_after is not None
    metadata = original_after.resume_metadata_json or {}
    assert "daemon_stop_orphan_reap_started_at" not in metadata


def test_orphan_reap_claim_stamps_elapsed_unconsumed_orphan(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    _sessions, runs, _parent, child, original = _seed_parked_run(temp_db, sample_project)
    old_timestamp = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    temp_db.execute(
        "UPDATE agent_runs SET completed_at = %s, updated_at = %s WHERE id = %s",
        (old_timestamp, old_timestamp, original.id),
    )

    orphans = runs.list_daemon_stop_orphans(max_age_hours=24)
    assert [run.id for run in orphans] == [original.id]

    assert claim_daemon_stop_orphan_reap(
        temp_db,
        original_run_id=original.id,
        child_session_id=child.id,
    )

    claimed = runs.get(original.id)
    assert claimed is not None
    assert claimed.resume_metadata_json is not None
    started_at = claimed.resume_metadata_json["daemon_stop_orphan_reap_started_at"]
    assert started_at

    # A repeat claim keeps the first stamp: the JSONB write is guarded.
    assert claim_daemon_stop_orphan_reap(
        temp_db,
        original_run_id=original.id,
        child_session_id=child.id,
    )
    reclaimed = runs.get(original.id)
    assert reclaimed is not None
    assert reclaimed.resume_metadata_json is not None
    assert reclaimed.resume_metadata_json["daemon_stop_orphan_reap_started_at"] == started_at


def test_increment_failure_count_treats_non_numeric_as_zero(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    _sessions, runs, _parent, _child, original = _seed_parked_run(temp_db, sample_project)
    merged = runs.merge_resume_metadata(
        original.id,
        {"daemon_stop_resume_failure_count": "corrupt"},
    )
    assert merged is not None

    assert increment_daemon_resume_failure_count(temp_db, run_id=original.id) == 1
    assert increment_daemon_resume_failure_count(temp_db, run_id=original.id) == 2


def test_increment_failure_count_increments_numeric_value(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    _sessions, runs, _parent, _child, original = _seed_parked_run(temp_db, sample_project)
    merged = runs.merge_resume_metadata(
        original.id,
        {"daemon_stop_resume_failure_count": "2"},
    )
    assert merged is not None

    assert increment_daemon_resume_failure_count(temp_db, run_id=original.id) == 3
