"""Automated task-close review orchestration tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration as orchestration
from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_close_orchestration import (
    launch_close_review,
    submit_close_review,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_close_preview import CloseEvaluation
from gobby.storage.task_close_reviews import TaskCloseReview, TaskCloseReviewStatus
from gobby.storage.tasks import Task
from gobby.tasks import agentic_close_review as agentic_close_review_module

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_oversized_close_persists_and_launches_one_taskless_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    registry = SimpleNamespace(call=AsyncMock(return_value={"success": True, "run_id": "run"}))
    ctx = _ctx(registry=registry)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    evaluation = _evaluation()
    arguments = _arguments()

    result = await launch_close_review(ctx, evaluation=evaluation, close_arguments=arguments)

    assert store.created_arguments == arguments
    registry.call.assert_awaited_once()
    launch_args = registry.call.call_args.args[1]
    assert launch_args["agent"] == "task-close-validator"
    assert launch_args["task_id"] is None
    assert launch_args["isolation"] == "none"
    assert launch_args["provider"] == "codex"
    assert launch_args["model"] == "gpt-5.6-terra"
    assert launch_args["reasoning_effort"] is None
    assert result["error"] == "agentic_review_required"
    assert result["review_status"] == "running"
    assert result["run_id"] == "run"
    assert "spawn_request" not in result
    assert "review_run_id" not in result


@pytest.mark.asyncio
async def test_launch_omits_model_overrides_without_validation_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    registry = SimpleNamespace(call=AsyncMock(return_value={"success": True, "run_id": "run"}))
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=SimpleNamespace(db=object()),
            agent_registry=registry,
            validation_config=None,
        ),
    )
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    await launch_close_review(ctx, evaluation=_evaluation(), close_arguments=_arguments())

    launch_args = registry.call.call_args.args[1]
    assert "provider" not in launch_args
    assert "model" not in launch_args
    assert "reasoning_effort" not in launch_args


@pytest.mark.parametrize(
    ("candidates", "profile", "expected"),
    [
        (
            ["codex/gpt-5.6-terra"],
            "feature_mid",
            {"provider": "codex", "model": "gpt-5.6-terra", "reasoning_effort": None},
        ),
        (
            [{"candidate": "codex/gpt-5.6-sol", "reasoning_effort": "xhigh"}, "claude/opus"],
            "feature_high",
            {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
        ),
        (
            ["claude/sonnet"],
            "feature_low",
            {"provider": "claude", "model": "sonnet", "reasoning_effort": "auto"},
        ),
    ],
)
def test_validator_spawn_overrides_follow_first_validation_candidate(
    candidates: list[object],
    profile: str,
    expected: dict[str, str | None],
) -> None:
    config = TaskValidationConfig(candidates=candidates, profile=profile)

    overrides = agentic_close_review_module.validator_spawn_overrides(config)

    assert overrides == expected


def test_validator_spawn_overrides_are_empty_without_config() -> None:
    assert agentic_close_review_module.validator_spawn_overrides(None) == {}


@pytest.mark.asyncio
async def test_concurrent_oversized_close_reuses_active_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _review(status="running", run_id="same-run")
    store = _Store(existing, created=False)
    registry = SimpleNamespace(call=AsyncMock())
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    result = await launch_close_review(
        _ctx(registry=registry),
        evaluation=_evaluation(),
        close_arguments=_arguments(),
    )

    assert result["error"] == "agentic_review_pending"
    assert result["run_id"] == "same-run"
    registry.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_authenticated_valid_submission_closes_and_persists_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="running", run_id="run"))
    _authenticate(monkeypatch, store.review)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    evaluation = _evaluation(ready=True)
    evaluate = AsyncMock(return_value=evaluation)
    commit = AsyncMock(
        return_value={
            "success": True,
            "closed": True,
            "task_id": "task",
            "commit_shas": ["abc"],
        }
    )

    result = await submit_close_review(
        _ctx(),
        review_id="review",
        verdict=_verdict("valid"),
        evaluate_close=evaluate,
        commit_close=commit,
    )

    assert result["success"] is True
    assert result["review_status"] == "closed"
    assert result["terminal_payload"]["event"] == "task_close_review_completed"
    assert store.finished_status == "closed"
    assert evaluate.call_args.kwargs["closing_session_id"] == "parent"
    assert evaluate.call_args.kwargs["submitted_review"].review_fingerprint == "close"
    commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_and_stale_submissions_clear_active_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for validation_status, error, expected in [
        ("invalid", "validation_failed", "invalid"),
        (None, "agentic_review_stale", "stale"),
    ]:
        store = _Store(_review(status="running", run_id="run"))
        _authenticate(monkeypatch, store.review)
        monkeypatch.setattr(
            orchestration,
            "TaskCloseReviewStore",
            lambda _db, current=store: current,
        )
        evaluation = _evaluation()
        evaluation.error = error
        evaluation.message = "feedback"
        evaluation.validation_status = validation_status
        evaluation.extra["blocking_reasons"] = ["gap"]

        result = await submit_close_review(
            _ctx(),
            review_id="review",
            verdict=_verdict("invalid"),
            evaluate_close=AsyncMock(return_value=evaluation),
            commit_close=AsyncMock(),
        )

        assert result["review_status"] == expected
        assert store.finished_status == expected
        assert result["closed"] is False


@pytest.mark.asyncio
async def test_malformed_submission_returns_review_to_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="running", run_id="run"))
    _authenticate(monkeypatch, store.review)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    evaluation = _evaluation()
    evaluation.error = "agentic_review_malformed"
    evaluation.message = "Fix criterion indexes."

    result = await submit_close_review(
        _ctx(),
        review_id="review",
        verdict={},
        evaluate_close=AsyncMock(return_value=evaluation),
        commit_close=AsyncMock(),
    )

    assert result["error"] == "agentic_review_malformed"
    assert result["review_status"] == "running"
    assert store.restored is True
    assert store.finished_status is None


@pytest.mark.asyncio
async def test_wrong_validator_run_is_rejected_without_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="running", run_id="run"))
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    monkeypatch.setattr(orchestration, "get_current_agent_run_id", lambda: "other-run")
    monkeypatch.setattr(orchestration, "get_current_session_id", lambda: "child")

    result = await submit_close_review(
        _ctx(),
        review_id="review",
        verdict=_verdict("valid"),
        evaluate_close=AsyncMock(),
        commit_close=AsyncMock(),
    )

    assert result["error"] == "agentic_review_unauthorized"
    assert store.claimed is False


class _Store:
    def __init__(self, review: TaskCloseReview, *, created: bool = True) -> None:
        self.review = review
        self.created = created
        self.created_arguments: dict[str, Any] | None = None
        self.finished_status: str | None = None
        self.claimed = False
        self.restored = False

    def create_or_get_active(self, **kwargs: Any) -> tuple[TaskCloseReview, bool]:
        self.created_arguments = dict(kwargs["close_arguments"])
        return self.review, self.created

    def bind_run(self, _review_id: str, run_id: str) -> TaskCloseReview:
        self.review = replace(self.review, status="running", agent_run_id=run_id)
        return self.review

    def get(self, _review_id: str) -> TaskCloseReview:
        return self.review

    def claim_finalizing(self, _review_id: str, _run_id: str) -> TaskCloseReview:
        self.claimed = True
        self.review = replace(self.review, status="finalizing")
        return self.review

    def restore_running(self, _review_id: str, _run_id: str, *, error: str) -> bool:
        del error
        self.restored = True
        self.review = replace(self.review, status="running")
        return True

    def finish(self, _review_id: str, *, status: str, **kwargs: Any) -> TaskCloseReview:
        self.finished_status = status
        self.review = replace(
            self.review,
            status=cast(TaskCloseReviewStatus, status),
            result_payload=dict(kwargs["result_payload"]),
        )
        return self.review


def _ctx(
    *,
    registry: object | None = None,
    validation_config: object | None = None,
) -> RegistryContext:
    if validation_config is None:
        validation_config = TaskValidationConfig(candidates=["codex/gpt-5.6-terra"])
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=SimpleNamespace(db=object()),
            agent_registry=registry,
            validation_config=validation_config,
        ),
    )


def _review(*, status: str, run_id: str | None) -> TaskCloseReview:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    return TaskCloseReview(
        id="review",
        task_id="task",
        task_ref="#42",
        caller_session_id="parent",
        agent_run_id=run_id,
        close_arguments=_arguments(),
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


def _evaluation(*, ready: bool = False) -> CloseEvaluation:
    task = Task(
        id="task",
        project_id="project",
        title="Task",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 8, 22, tzinfo=UTC),
        updated_at=datetime(2026, 8, 22, tzinfo=UTC),
        seq_num=42,
    )
    evaluation = CloseEvaluation("#42")
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.resolved_session_id = "parent"
    evaluation.repo_path = "/repo"
    evaluation.commit_shas = ["abc"]
    evaluation.extra.update(
        {
            "review_fingerprint": "close",
            "deterministic_evidence_fingerprint": "evidence",
        }
    )
    if ready:
        evaluation.pass_gate(14, "criteria_review", "valid")
    else:
        evaluation.error = "agentic_review_required"
    return evaluation


def _arguments() -> dict[str, Any]:
    return {
        "task_id": "#42",
        "reason": "completed",
        "changes_summary": "Implemented.",
        "skip_validation": False,
        "override_justification": None,
        "scope_justification": None,
        "commit_sha": "abc",
        "project_path": "/repo",
        "preview": True,
        "response_detail": "concise",
    }


def _authenticate(monkeypatch: pytest.MonkeyPatch, review: TaskCloseReview) -> None:
    run = SimpleNamespace(
        agent_name="task-close-validator",
        task_id=None,
        parent_session_id=review.caller_session_id,
        child_session_id="child",
    )
    monkeypatch.setattr(orchestration, "get_current_agent_run_id", lambda: review.agent_run_id)
    monkeypatch.setattr(orchestration, "get_current_session_id", lambda: "child")
    monkeypatch.setattr(
        orchestration,
        "LocalAgentRunManager",
        lambda _db: SimpleNamespace(get=MagicMock(return_value=run)),
    )


def _verdict(status: str) -> dict[str, object]:
    return {
        "status": status,
        "criteria": [{"index": 1, "satisfied": status == "valid", "gap": None}],
        "feedback": status,
    }
