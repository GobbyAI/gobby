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
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import SubmittedCloseReview
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_close_reviews import (
    TaskCloseReview,
    TaskCloseReviewStatus,
    TaskCloseReviewStore,
)
from gobby.storage.tasks import Task
from gobby.tasks import agentic_close_review as agentic_close_review_module

pytestmark = pytest.mark.unit

_PERSISTED_TASK_ID = "00000000-0000-4000-8000-000000002608"
_PERSISTED_SESSION_ID = "00000000-0000-4000-8000-000000002609"
_FIRST_REVIEW_RUN_ID = "00000000-0000-4000-8000-000000002610"
_SECOND_REVIEW_RUN_ID = "00000000-0000-4000-8000-000000002611"
_REQUIRED_EVIDENCE = "Run the real close adapter and capture its MCP response receipt."


@pytest.mark.asyncio
async def test_close_persists_and_launches_one_taskless_validator(
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
    # An unpinned candidate inherits the profile default, which is always `auto`.
    assert launch_args["reasoning_effort"] == "auto"
    assert result["success"] is True
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["error"] == "agentic_review_required"
    assert result["review_status"] == "running"
    assert "run_id" not in result
    assert "spawn_request" not in result
    assert "review_run_id" not in result
    assert "Do not poll agent runs or re-call close_task." in result["message"]
    assert "Oversized" not in result["message"]
    assert result["criteria_review_duration_ms"] == 4.25


@pytest.mark.asyncio
async def test_launch_prompt_carries_gate10_validation_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The evaluation's gate-10 record reaches the validator through its launch prompt."""
    store = _Store(_review(status="launching", run_id=None))
    registry = SimpleNamespace(call=AsyncMock(return_value={"success": True, "run_id": "run"}))
    ctx = _ctx(registry=registry)
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)
    evaluation = _evaluation()
    evaluation.extra["validation_commands"] = {
        "latest_outcomes": {"test": "success"},
        "latest_runs": [
            {
                "category": "test",
                "command": "uv run pytest tests/tasks/ -q",
                "completed_at": "2026-09-03T05:10:00+00:00",
                "outcome": "success",
                "exit_code": 0,
            }
        ],
    }

    await launch_close_review(ctx, evaluation=evaluation, close_arguments=_arguments())

    launch_prompt = registry.call.await_args.args[1]["prompt"]
    assert "validation_commands=" in launch_prompt
    assert "uv run pytest tests/tasks/ -q" in launch_prompt
    assert "gate 10's authoritative transcript record" in launch_prompt


@pytest.mark.asyncio
@pytest.mark.integration
async def test_launch_after_rejected_verdict_carries_required_evidence(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TaskCloseReviewStore(temp_db)
    task_manager = SimpleNamespace(db=temp_db)
    task = Task(
        id=_PERSISTED_TASK_ID,
        project_id="00000000-0000-4000-8000-000000002612",
        title="Carry prior close evidence",
        priority=1,
        task_type="task",
        validation_criteria="Focused tests pass.",
        created_at=datetime(2026, 8, 22, tzinfo=UTC),
        updated_at=datetime(2026, 8, 22, tzinfo=UTC),
        seq_num=42,
    )
    evaluation = CloseEvaluation("#42")
    evaluation.task = task
    evaluation.task_id = task.id
    evaluation.resolved_session_id = _PERSISTED_SESSION_ID
    evaluation.repo_path = "/repo"
    evaluation.commit_shas = ["def"]
    evaluation.error = "agentic_review_required"
    evaluation.extra.update(
        {
            "review_fingerprint": "first-review",
            "deterministic_evidence_fingerprint": "first-evidence",
            "diff_sha": "a" * 64,
            "test_bodies_sha": "b" * 64,
            "stable_facts": {"commit_shas": ["def"]},
        }
    )
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _FIRST_REVIEW_RUN_ID})
    )
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=task_manager,
            agent_registry=registry,
            validation_config=TaskValidationConfig(),
        ),
    )

    await launch_close_review(ctx, evaluation=evaluation, close_arguments=_arguments())

    first_launch_prompt = registry.call.await_args.args[1]["prompt"]
    assert "prior_requirements=" not in first_launch_prompt
    first_review = store.get_active_for_task(_PERSISTED_TASK_ID)
    assert first_review is not None
    assert first_review.agent_run_id == _FIRST_REVIEW_RUN_ID

    monkeypatch.setattr(orchestration, "_authenticate_submission", lambda _ctx, _review: None)
    submitted_verdict: dict[str, object] = {
        "status": "invalid",
        "criteria": [
            {
                "index": 1,
                "satisfied": False,
                "gap": "The real close path was not exercised.",
                "required_evidence": _REQUIRED_EVIDENCE,
            }
        ],
        "feedback": "The close evidence is incomplete.",
    }
    submit_ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=task_manager,
            agent_registry=None,
            validation_config=TaskValidationConfig(),
        ),
    )

    async def evaluate_close(
        _ctx: RegistryContext,
        **kwargs: Any,
    ) -> CloseEvaluation:
        submitted = kwargs["submitted_review"]
        assert isinstance(submitted, SubmittedCloseReview)
        evaluation = CloseEvaluation("#42")
        evaluation.task_id = _PERSISTED_TASK_ID
        evaluation.error = "validation_failed"
        evaluation.message = "The close evidence is incomplete."
        evaluation.validation_status = "invalid"
        evaluation.verdict = dict(submitted.verdict)
        return evaluation

    submit_result = await submit_close_review(
        submit_ctx,
        review_id=first_review.id,
        verdict=submitted_verdict,
        evaluate_close=evaluate_close,
        commit_close=AsyncMock(),
    )

    assert submit_result["review_status"] == "invalid"
    terminal = store.get(first_review.id)
    assert terminal is not None
    assert terminal.result_payload is not None
    assert terminal.result_payload["verdict"] == submitted_verdict

    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": True, "run_id": _SECOND_REVIEW_RUN_ID})
    )
    evaluation.extra.update(
        {
            "review_fingerprint": "second-review",
            "deterministic_evidence_fingerprint": "second-evidence",
            "diff_sha": "c" * 64,
            "test_bodies_sha": "d" * 64,
            "stable_facts": {"commit_shas": ["def"]},
        }
    )
    ctx = cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=task_manager,
            agent_registry=registry,
            validation_config=TaskValidationConfig(),
        ),
    )

    await launch_close_review(ctx, evaluation=evaluation, close_arguments=_arguments())

    launch_prompt = registry.call.await_args.args[1]["prompt"]
    assert f"Required evidence: {_REQUIRED_EVIDENCE}" in launch_prompt
    assert "prior_requirements=" in launch_prompt
    assert 'changes_summary="Implemented."' in launch_prompt


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


@pytest.mark.asyncio
async def test_failed_validator_launch_remains_unsuccessful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    registry = SimpleNamespace(
        call=AsyncMock(return_value={"success": False, "error": "validator unavailable"})
    )
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    result = await launch_close_review(
        _ctx(registry=registry),
        evaluation=_evaluation(),
        close_arguments=_arguments(),
    )

    assert result["success"] is False
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["error"] == "agentic_review_launch_failed"
    assert result["review_status"] == "error"
    assert store.finished_status == "error"


@pytest.mark.asyncio
async def test_missing_agent_registry_finishes_launch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    result = await launch_close_review(
        _ctx(registry=None),
        evaluation=_evaluation(),
        close_arguments=_arguments(),
    )

    assert result["error"] == "agentic_review_launch_failed"
    assert result["closed"] is False
    assert store.finished_status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "launch",
    [
        pytest.param(RuntimeError("spawn failed"), id="spawn-exception"),
        pytest.param({"success": True}, id="missing-run-id"),
    ],
)
async def test_incomplete_spawn_finishes_launch_error(
    monkeypatch: pytest.MonkeyPatch,
    launch: Exception | dict[str, object],
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    call = (
        AsyncMock(side_effect=launch)
        if isinstance(launch, Exception)
        else AsyncMock(return_value=launch)
    )
    monkeypatch.setattr(orchestration, "TaskCloseReviewStore", lambda _db: store)

    result = await launch_close_review(
        _ctx(registry=SimpleNamespace(call=call)),
        evaluation=_evaluation(),
        close_arguments=_arguments(),
    )

    assert result["error"] == "agentic_review_launch_failed"
    assert result["closed"] is False
    assert store.finished_status == "error"


@pytest.mark.parametrize(
    ("candidates", "profile", "expected"),
    [
        (
            ["codex/gpt-5.6-terra"],
            "feature_mid",
            {"provider": "codex", "model": "gpt-5.6-terra", "reasoning_effort": "auto"},
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
async def test_concurrent_close_reuses_active_review(
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

    assert result["success"] is True
    assert result["closed"] is False
    assert result["can_close"] is False
    assert result["error"] == "agentic_review_pending"
    assert "run_id" not in result
    assert "Do not poll agent runs or re-call close_task." in result["message"]
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
        if expected == "invalid":
            assert result["terminal_payload"]["blocking_reasons"] == ["gap"]


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
            "diff_sha": "diff",
            "test_bodies_sha": "tests",
            "stable_facts": {},
            "criteria_review_duration_ms": 4.25,
        }
    )
    if ready:
        evaluation.pass_gate(13, "criteria_review", "valid")
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
