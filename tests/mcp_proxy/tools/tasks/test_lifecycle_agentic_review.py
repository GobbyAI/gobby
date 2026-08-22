"""Agentic task-close review routing tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks import _lifecycle_review_gate as review_gate
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import evaluate_close_criteria
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.storage.tasks import Task
from gobby.tasks.agentic_close_review import AgenticReviewCheck
from gobby.tasks.validation import TaskValidator


@pytest.mark.asyncio
async def test_oversized_review_returns_fingerprinted_launch_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        review_gate,
        "evaluate_criteria_review",
        AsyncMock(return_value=_oversized()),
    )

    result = await _evaluate(review_run_id=None)

    assert result.error_type == "agentic_review_required"
    assert result.extra["review_fingerprint"] == "close"
    spawn = cast(dict[str, object], result.extra["spawn_request"])
    assert spawn["agent"] == "task-close-validator"
    assert spawn["task_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["valid", "invalid"])
async def test_matching_agent_verdict_uses_shared_accounting(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    monkeypatch.setattr(
        review_gate,
        "evaluate_criteria_review",
        AsyncMock(return_value=_oversized()),
    )
    monkeypatch.setattr(
        review_gate,
        "validate_agentic_review_run",
        MagicMock(
            return_value=AgenticReviewCheck(
                state="ready",
                error_type=None,
                message="ready",
                verdict={
                    "status": status,
                    "criteria": [
                        {
                            "index": 1,
                            "satisfied": status == "valid",
                            "gap": None if status == "valid" else "missing",
                        }
                    ],
                    "feedback": status,
                },
            )
        ),
    )
    accounted = ValidationResult(can_close=status == "valid")
    account = MagicMock(return_value=accounted)
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(review_run_id="run")

    assert result is accounted
    account.assert_called_once()
    assert account.call_args.kwargs["verdict"].status == status
    assert result.extra["review_run_id"] == "run"


def _oversized() -> ValidationResult:
    return ValidationResult(
        can_close=False,
        error_type="validation_prompt_too_large",
        message="large",
        extra={
            "review_fingerprint": "close",
            "evidence_fingerprint": "evidence",
            "prompt_chars": 256_001,
            "prompt_limit": 256_000,
        },
    )


async def _evaluate(*, review_run_id: str | None) -> ValidationResult:
    task = Task(
        id="task",
        project_id="project",
        title="Task",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        updated_at=datetime(2026, 8, 21, tzinfo=UTC),
        validation_criteria="Criterion.",
    )
    ctx = cast(
        RegistryContext,
        SimpleNamespace(task_manager=SimpleNamespace(db=object())),
    )
    return await evaluate_close_criteria(
        task=task,
        task_validator=cast(TaskValidator, object()),
        ctx=ctx,
        resolved_id=task.id,
        parent_session_id="parent",
        changes_summary="summary",
        commit_shas=["abc"],
        diff_text="diff",
        checklist_facts={},
        validation_config=None,
        reason="completed",
        description="description",
        test_bodies="tests",
        review_run_id=review_run_id,
    )
