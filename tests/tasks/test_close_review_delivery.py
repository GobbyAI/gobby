"""Terminal delivery projection tests for task-close reviewer runs."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import gobby.tasks.close_review_delivery as delivery
from gobby.events.wake_notifications import persist_completion_notification
from gobby.storage.task_close_reviews import TaskCloseReview, TaskCloseReviewStatus

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("denials", "log_location", "expected_operations"),
    [
        ([], "retained", None),
        (
            [
                "codex(12) deny(1) system-info vfs.disk-space",
                "codex(12) deny(1) network-outbound",
                "deny network-outbound raw.githubusercontent.com:443 (host is not on the allow list)",
            ],
            "retained",
            {"network-outbound": 2, "system-info": 1},
        ),
        (
            [
                "codex(12) deny(1) system-info vfs.disk-space",
                "codex(12) deny(1) network-outbound",
                "deny network-outbound raw.githubusercontent.com:443 (host is not on the allow list)",
            ],
            "live",
            {"network-outbound": 2, "system-info": 1},
        ),
        (
            [
                "codex(12) deny(1) system-info vfs.disk-space",
                "codex(12) deny(1) network-outbound",
                "deny network-outbound raw.githubusercontent.com:443 (host is not on the allow list)",
            ],
            "managed",
            {"network-outbound": 2, "system-info": 1},
        ),
    ],
)
def test_completed_review_reports_sandbox_denials_without_changing_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    denials: list[str],
    log_location: str,
    expected_operations: dict[str, int] | None,
) -> None:
    home = tmp_path / "gobby-home"
    retained = home / "logs" / "sandbox-violations" / "run.jsonl"
    live = home / "run" / "sandbox" / "run" / "violations.jsonl"
    managed = home / "runtime" / "managed-executions" / "run" / "logs" / "violations.jsonl"
    log = {"live": live, "managed": managed, "retained": retained}[log_location]
    log.parent.mkdir(parents=True)
    log.write_text("".join(json.dumps({"line": line}) + "\n" for line in denials))
    monkeypatch.setenv("GOBBY_HOME", str(home))
    original_payload = {"review_id": "review", "status": "closed", "message": "Task closed."}
    store = _Store(replace(_review("closed"), result_payload=original_payload))
    run = SimpleNamespace(
        resume_metadata_json={
            "sandbox": {
                "backend": "srt",
                "enforced": True,
                "violation_path": str(log) if log_location != "retained" else None,
                "retained_violation_path": str(retained) if log_location == "retained" else None,
            }
        }
    )
    _install(monkeypatch, store=store, run=run, task=None)

    delivered = delivery.terminal_review_delivery(cast(Any, object()), "run")

    assert delivered is not None
    payload, message = delivered
    assert payload["status"] == "closed"
    assert original_payload == {
        "review_id": "review",
        "status": "closed",
        "message": "Task closed.",
    }
    if expected_operations is None:
        assert "sandbox_denials" not in payload
        assert message == "Task closed."
    else:
        assert payload["sandbox_denials"] == {
            "violation_count": len(denials),
            "operations": expected_operations,
            "retained_violation_path": str(retained) if log_location == "retained" else None,
            "retained_settings_path": None,
        }
        assert "SRT denied 3 operations" in message
        assert "network-outbound 2" in message
        assert "get_agent_result(run)" in message


async def test_managed_live_denials_reach_owner_completion_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "gobby-home"
    log = home / "runtime" / "managed-executions" / "run" / "logs" / "violations.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps({"line": "deny network-outbound raw.githubusercontent.com:443"}) + "\n"
    )
    monkeypatch.setenv("GOBBY_HOME", str(home))
    store = _Store(
        replace(
            _review("closed"),
            result_payload={
                "event": "task_close_review_completed",
                "review_id": "review",
                "run_id": "run",
                "task_id": "task",
                "status": "closed",
                "message": "Task closed after background validation.",
            },
        )
    )
    run = SimpleNamespace(
        resume_metadata_json={
            "sandbox": {"backend": "srt", "enforced": True, "violation_path": str(log)}
        }
    )
    _install(monkeypatch, store=store, run=run, task=None)
    projected = delivery.terminal_review_delivery(cast(Any, object()), "run")
    assert projected is not None
    payload, message = projected

    created: list[dict[str, Any]] = []
    manager = SimpleNamespace(
        db=SimpleNamespace(bounded_transaction=nullcontext),
        create_message=lambda **kwargs: created.append(kwargs),
    )

    async def run_db(func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)

    assert await persist_completion_notification(
        cast(Any, manager), run_db, "owner", message, payload
    )
    assert len(created) == 1
    notification = created[0]
    metadata = json.loads(notification["metadata_json"])
    assert metadata["sandbox_denials"]["violation_count"] == 1
    assert metadata["sandbox_denials"]["operations"] == {"network-outbound": 1}
    assert "SRT denied 1 operation" in notification["content"]
    assert "SRT denied 1 operation" in metadata["completion_message"]


@pytest.mark.parametrize(
    "run_status,terminal_reason,infrastructure,expected_class",
    [
        ("error", "provider_error", False, "action_required"),
        ("timeout", None, False, "retryable_infrastructure"),
        ("cancelled", "user_cancel", False, "action_required"),
        ("error", "provider_quota_exhausted", False, "retryable_infrastructure"),
        ("cancelled", "spawn_rollback", True, "retryable_infrastructure"),
        ("cancelled", "spawn_rollback", False, "action_required"),
        ("success", None, False, "action_required"),
    ],
)
def test_failed_reviewer_run_terminalizes_review_and_clears_lock(
    monkeypatch: pytest.MonkeyPatch,
    run_status: str,
    terminal_reason: str | None,
    infrastructure: bool,
    expected_class: str,
) -> None:
    store = _Store(_review("running"))
    run = SimpleNamespace(
        status=run_status,
        error="boom",
        terminal_reason=terminal_reason,
        resume_metadata_json={"spawn_retryable_infrastructure": infrastructure},
    )
    _install(monkeypatch, store=store, run=run, task=None)

    resolved = delivery.terminal_review_delivery(cast(Any, object()), "run")

    assert resolved is not None
    payload, message = resolved
    assert payload["status"] == "error"
    assert payload["closed"] is False
    assert run_status in message
    assert payload["error_class"] == expected_class
    assert (payload["retry_after"] is not None) == (expected_class == "retryable_infrastructure")
    assert store.finished_status == "error"


def test_interrupted_finalization_recovers_closed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review("finalizing"))
    run = SimpleNamespace(status="success", error=None, resume_metadata_json=None)
    task = SimpleNamespace(id="task", commits=["abc"], closed_at=datetime.now(UTC))
    _install(monkeypatch, store=store, run=run, task=task)

    resolved = delivery.terminal_review_delivery(cast(Any, object()), "run")

    assert resolved is not None
    payload, _message = resolved
    assert payload["status"] == "closed"
    assert payload["closed"] is True
    assert payload["commit_shas"] == ["abc"]
    assert store.finished_status == "closed"


def test_run_end_leaves_finalizing_review_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review("finalizing"))
    run = SimpleNamespace(status="success", error=None, resume_metadata_json=None)
    task = SimpleNamespace(id="task", commits=[], closed_at=None)
    _install(monkeypatch, store=store, run=run, task=task)

    resolved = delivery.terminal_review_delivery(cast(Any, object()), "run")

    assert resolved is None
    assert store.finished_status is None
    assert store.review.status == "finalizing"


class _Store:
    def __init__(self, review: TaskCloseReview) -> None:
        self.review = review
        self.finished_status: str | None = None

    def get_by_run(self, _run_id: str) -> TaskCloseReview:
        return self.review

    def finish(self, _review_id: str, *, status: str, **kwargs: Any) -> TaskCloseReview:
        self.finished_status = status
        self.review = replace(
            self.review,
            status=cast(TaskCloseReviewStatus, status),
            result_payload=dict(kwargs["result_payload"]),
        )
        return self.review

    def finish_run_ended(self, _review_id: str, **kwargs: Any) -> TaskCloseReview:
        return self.finish(_review_id, status="error", **kwargs)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: _Store,
    run: object,
    task: object | None,
) -> None:
    monkeypatch.setattr(delivery, "TaskCloseReviewStore", lambda _db: store)
    monkeypatch.setattr(
        delivery,
        "LocalAgentRunManager",
        lambda _db: SimpleNamespace(get=lambda _run_id: run),
    )
    monkeypatch.setattr(
        delivery,
        "LocalTaskManager",
        lambda _db: SimpleNamespace(get_task=lambda _task_id: task),
    )


def _review(status: str) -> TaskCloseReview:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    return TaskCloseReview(
        id="review",
        task_id="task",
        task_ref="#42",
        caller_session_id="parent",
        agent_run_id="run",
        close_arguments={},
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        status=cast(TaskCloseReviewStatus, status),
        result_payload=None,
        error=None,
        launched_at=now,
        completed_at=None,
        delivered_at=None,
        created_at=now,
        updated_at=now,
    )
