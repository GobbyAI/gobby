"""Closed-candidate correction preserves the original reviewed closure."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry
from gobby.storage.hub.protocol import Cursor, HubDatabase, Transaction
from gobby.storage.sessions import SessionManager
from gobby.storage.task_close_reviews import (
    QueuedAgentRunSpec,
    TaskCloseReview,
    TaskCloseReviewStore,
)
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.agentic_close_review import build_terminal_review_payload
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from tests.mcp_proxy.tools.tasks.test_close_task_flow import _ctx

pytestmark = pytest.mark.unit
_CANDIDATE = "b8fa20f6a339132f9791a23c907e2a82dbeb25ed"
_OLD = "0ebabda962"
_REASON = "Correct the insertion-order marker to the originally reviewed candidate."


@pytest.mark.asyncio
async def test_public_repair_tool_derives_the_candidate_from_original_review(
    reviewed_task: tuple[LocalTaskManager, TaskCloseReview],
) -> None:
    manager, review = reviewed_task
    task = manager.get_task(review.task_id)
    assert task is not None
    ctx = _ctx(task)
    ctx.task_manager = manager
    ctx.session_manager = SessionManager(manager.db)
    registry = create_lifecycle_registry(ctx)
    schema = registry.get_schema("repair_closed_candidate")
    assert schema is not None
    assert "candidate_commit_sha" not in schema["inputSchema"]["properties"]
    with (
        session_context_for_test(review.caller_session_id),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_candidate_repair.resolve_task_repo_path",
            return_value="/repo",
        ),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_candidate_repair._canonical_commit_sha",
            return_value=_CANDIDATE,
        ),
    ):
        result = await registry.call(
            "repair_closed_candidate",
            {
                "task_id": review.task_id,
                "review_id": review.id,
                "expected_closed_commit_sha": _OLD,
                "reason": _REASON,
                "project_path": "/repo",
            },
        )
    assert result["success"] is True
    assert result["candidate_commit_sha"] == _CANDIDATE
    assert result["applied"] is False
    assert manager.get_task(review.task_id) == task


@pytest.fixture
def reviewed_task(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> tuple[LocalTaskManager, TaskCloseReview]:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Historical closed candidate",
        validation_criteria="Focused tests pass.",
    )
    actor = SessionManager(temp_db).register(
        external_id=str(uuid4()),
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    manager.link_commit(task.id, _CANDIDATE)
    manager.link_commit(task.id, _OLD)
    linked = manager.get_task(task.id)
    assert linked is not None
    store = TaskCloseReviewStore(temp_db)
    review, created = store.create_or_get_active(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        caller_session_id=actor.id,
        commit_shas=linked.commits or [],
        close_arguments={"commit_sha": _CANDIDATE, "reason": "completed"},
        expected_task_updated_at=linked.updated_at,
        review_fingerprint="a" * 64,
        evidence_fingerprint="e" * 64,
        diff_sha="d" * 64,
        test_bodies_sha="b" * 64,
        stable_facts={"commit_shas": linked.commits or []},
        review_id=str(uuid4()),
        run=QueuedAgentRunSpec(
            id=str(uuid4()),
            machine_id=actor.machine_id or "",
            provider="codex",
            model=None,
            agent_name="task-close-reviewer",
            prompt="Isolated historical review fixture",
            timeout_seconds=30,
        ),
    )
    assert created is True
    manager.close_task(
        task.id,
        reason="completed",
        closed_in_session_id=actor.id,
        closed_commit_sha=_OLD,
        validation_status="valid",
        validation_feedback="Original independent VALID review.",
    )
    finished = store.finish(
        review.id,
        status="closed",
        result_payload=build_terminal_review_payload(
            review,
            status="closed",
            close_result={"closed": True, "validation_status": "valid"},
        ),
    )
    assert finished is not None
    return manager, finished


def test_repair_preview_is_read_only_and_apply_is_audited(
    reviewed_task: tuple[LocalTaskManager, TaskCloseReview],
) -> None:
    manager, review = reviewed_task
    before = manager.get_task(review.task_id)
    assert before is not None
    arguments = {
        "review_id": review.id,
        "candidate_commit_sha": _CANDIDATE,
        "expected_closed_commit_sha": _OLD,
        "by_session_id": review.caller_session_id,
        "reason": _REASON,
    }
    preview = manager.repair_closed_candidate(review.task_id, **arguments, preview=True)
    assert preview["candidate_commit_sha"] == _CANDIDATE
    assert preview["applied"] is False
    assert manager.get_task(review.task_id) == before
    applied = manager.repair_closed_candidate(review.task_id, **arguments, preview=False)
    after = manager.get_task(review.task_id)
    assert after is not None
    assert applied["applied"] is True
    assert after.closed_commit_sha == _CANDIDATE
    assert after.closed_at == before.closed_at
    assert after.closed_reason == before.closed_reason
    assert after.closed_in_session_id == before.closed_in_session_id
    assert after.validation_status == "valid"
    assert after.validation_feedback == before.validation_feedback
    assert after.commits == before.commits
    assert after.claimed_by_session_id is None
    assert TaskCloseReviewStore(manager.db).get(review.id) == review
    events = manager.db.fetchall(
        "SELECT * FROM task_lifecycle_events WHERE task_id = %s AND reason LIKE %s",
        (review.task_id, "repair_closed_candidate:%"),
    )
    assert len(events) == 1
    assert events[0]["from_state"] == events[0]["to_state"] == "closed"
    assert events[0]["by_actor"] == review.caller_session_id
    assert review.id in events[0]["reason"]
    assert _OLD in events[0]["reason"] and _CANDIDATE in events[0]["reason"]


def test_audit_failure_rolls_back_the_marker_correction(
    reviewed_task: tuple[LocalTaskManager, TaskCloseReview],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, review = reviewed_task
    before = manager.get_task(review.task_id)
    transaction = manager.db.transaction

    @contextmanager
    def failing_audit_transaction() -> Iterator[Transaction]:
        with transaction() as conn:

            def execute(sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
                if "INSERT INTO task_lifecycle_events" in sql:
                    raise RuntimeError("Simulated unavailable lifecycle audit")
                return conn.execute(sql, params)

            proxy = MagicMock(wraps=conn)
            proxy.execute.side_effect = execute
            yield cast(Transaction, proxy)

    monkeypatch.setattr(manager.db, "transaction", failing_audit_transaction)
    with pytest.raises(RuntimeError, match="unavailable lifecycle audit"):
        manager.repair_closed_candidate(
            review.task_id,
            review_id=review.id,
            candidate_commit_sha=_CANDIDATE,
            expected_closed_commit_sha=_OLD,
            by_session_id=review.caller_session_id,
            reason=_REASON,
            preview=False,
        )
    assert manager.get_task(review.task_id) == before
    assert TaskCloseReviewStore(manager.db).get(review.id) == review
    assert (
        manager.db.fetchall(
            "SELECT id FROM task_lifecycle_events WHERE task_id = %s AND reason LIKE %s",
            (review.task_id, "repair_closed_candidate:%"),
        )
        == []
    )


@pytest.mark.parametrize(
    "drift",
    [
        "open",
        "invalid",
        "claimed",
        "merging",
        "links",
        "later_closure",
        "invalid_review",
        "different_candidate",
        "terminal_payload",
        "marker",
    ],
)
def test_repair_rejects_unreviewed_or_changed_state_without_writes(
    reviewed_task: tuple[LocalTaskManager, TaskCloseReview], drift: str
) -> None:
    manager, review = reviewed_task
    if drift == "open":
        manager.db.execute("UPDATE tasks SET closed_at = NULL WHERE id = %s", (review.task_id,))
    elif drift == "invalid":
        manager.db.execute(
            "UPDATE tasks SET validation_status = 'invalid' WHERE id = %s", (review.task_id,)
        )
    elif drift == "claimed":
        manager.db.execute(
            "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s",
            (review.caller_session_id, review.task_id),
        )
    elif drift == "merging":
        manager.db.execute(
            "UPDATE tasks SET merge_in_progress = TRUE WHERE id = %s", (review.task_id,)
        )
    elif drift == "links":
        manager.link_commit(review.task_id, "f" * 40)
    elif drift == "later_closure":
        assert review.completed_at is not None
        manager.db.execute(
            "UPDATE tasks SET closed_at = %s WHERE id = %s",
            (review.completed_at + timedelta(seconds=1), review.task_id),
        )
    elif drift == "invalid_review":
        manager.db.execute(
            "UPDATE task_close_reviews SET status = 'invalid' WHERE id = %s", (review.id,)
        )
    elif drift == "terminal_payload":
        manager.db.execute(
            """UPDATE task_close_reviews SET result_payload =
               '{"closed": true, "validation_status": "valid", "status": "invalid"}'::jsonb
               WHERE id = %s""",
            (review.id,),
        )
    elif drift == "marker":
        manager.db.execute(
            "UPDATE tasks SET closed_commit_sha = %s WHERE id = %s",
            ("c" * 40, review.task_id),
        )
    before = manager.get_task(review.task_id)
    with pytest.raises(ValueError):
        manager.repair_closed_candidate(
            review.task_id,
            review_id=review.id,
            candidate_commit_sha="f" * 40 if drift == "different_candidate" else _CANDIDATE,
            expected_closed_commit_sha=_OLD,
            by_session_id=review.caller_session_id,
            reason=_REASON,
            preview=False,
        )
    assert manager.get_task(review.task_id) == before
    assert (
        manager.db.fetchall(
            "SELECT id FROM task_lifecycle_events WHERE task_id = %s AND reason LIKE %s",
            (review.task_id, "repair_closed_candidate:%"),
        )
        == []
    )
