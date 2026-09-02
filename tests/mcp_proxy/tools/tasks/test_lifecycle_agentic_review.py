"""Detached task-close review gate routing tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import gobby.mcp_proxy.tools.tasks._lifecycle_review_gate as review_gate
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_review_gate import (
    SubmittedCloseReview,
    evaluate_close_criteria,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import ValidationResult
from gobby.storage.tasks import Task
from gobby.tasks.validation import PreparedCloseReview, TaskValidator

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_ordinary_review_detaches_without_awaiting_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_release = asyncio.Event()

    async def wait_for_provider(*_args: object, **_kwargs: object) -> ValidationResult:
        await provider_release.wait()
        return ValidationResult(can_close=True)

    inline_review = AsyncMock(side_effect=wait_for_provider)
    monkeypatch.setattr(
        review_gate,
        "evaluate_criteria_review",
        inline_review,
        raising=False,
    )

    result = await asyncio.wait_for(_evaluate(), timeout=0.05)

    assert result.error_type == "agentic_review_required"
    assert result.extra["review_fingerprint"] == "close"
    assert result.extra["deterministic_evidence_fingerprint"] == "evidence"
    inline_review.assert_not_awaited()
    assert "spawn_request" not in result.extra
    assert "review_run_id" not in result.extra


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["valid", "invalid"])
async def test_matching_submitted_verdict_uses_shared_accounting(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    accounted = ValidationResult(can_close=status == "valid")
    account = MagicMock(return_value=accounted)
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={
                "status": status,
                "criteria": [{"index": 1, "satisfied": status == "valid", "gap": None}],
                "feedback": status,
            },
            review_fingerprint="close",
            evidence_fingerprint="evidence",
        )
    )

    assert result is accounted
    account.assert_called_once()
    assert account.call_args.kwargs["verdict"].status == status
    assert result.extra["deterministic_evidence_fingerprint"] == "evidence"


@pytest.mark.asyncio
async def test_stale_submitted_fingerprint_skips_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = MagicMock()
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={"status": "valid", "criteria": [], "feedback": "ok"},
            review_fingerprint="stale",
            evidence_fingerprint="evidence",
        )
    )

    assert result.error_type == "agentic_review_stale"
    account.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_submitted_verdict_can_be_corrected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = MagicMock()
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        submitted=SubmittedCloseReview(
            verdict={"status": "unknown", "criteria": [], "feedback": "invalid status"},
            review_fingerprint="close",
            evidence_fingerprint="evidence",
        )
    )

    assert result.error_type == "agentic_review_malformed"
    account.assert_not_called()


@pytest.mark.asyncio
async def test_operational_criteria_block_before_inline_or_submitted_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = MagicMock(return_value=_prepared())
    account = MagicMock()
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)

    result = await _evaluate(
        criteria="Install the release, restart the daemon, and run a smoke check.",
        changes_summary="Implementation and tests are complete.",
        submitted=SubmittedCloseReview(
            verdict={"status": "valid", "criteria": [], "feedback": "ok"},
            review_fingerprint="close",
            evidence_fingerprint="evidence",
        ),
        prepare=prepare,
    )

    assert result.error_type == "operational_evidence_missing"
    assert result.extra["missing_operational_actions"] == ["install", "restart", "smoke"]
    prepare.assert_not_called()
    account.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes_summary", "checklist_facts"),
    [
        ("Release installed; daemon restart completed; smoke check passed.", {}),
        (
            "Implementation complete.",
            {
                "transcript_operational_actions": [
                    "install:release",
                    "restart:daemon",
                    "smoke",
                ]
            },
        ),
    ],
)
async def test_operational_criteria_reach_review_with_completion_evidence(
    changes_summary: str,
    checklist_facts: dict[str, object],
) -> None:
    prepare = MagicMock(return_value=_prepared())

    result = await _evaluate(
        criteria="Install the release, restart the daemon, and run a smoke check.",
        changes_summary=changes_summary,
        checklist_facts=checklist_facts,
        prepare=prepare,
    )

    assert result.error_type == "agentic_review_required"
    prepare.assert_called_once()


@pytest.mark.asyncio
async def test_no_work_disposition_skips_operational_evidence_gate() -> None:
    prepare = MagicMock(return_value=_prepared())

    result = await _evaluate(
        criteria="Deploy the service and run a smoke check.",
        changes_summary="Superseded by the replacement task.",
        reason="obsolete",
        prepare=prepare,
    )

    assert result.error_type == "agentic_review_required"
    prepare.assert_called_once()


def _prepared() -> PreparedCloseReview:
    return PreparedCloseReview(
        prompt="prompt",
        criteria=("Criterion.",),
        prompt_chars=1_024,
        prompt_limit=256_000,
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        manifest_count=1,
        excerpt_chars=10,
    )


async def _evaluate(
    *,
    submitted: SubmittedCloseReview | None = None,
    criteria: str = "Criterion.",
    changes_summary: str = "summary",
    checklist_facts: dict[str, object] | None = None,
    reason: str = "completed",
    prepare: MagicMock | None = None,
) -> ValidationResult:
    task = Task(
        id="task",
        project_id="project",
        title="Task",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        updated_at=datetime(2026, 8, 21, tzinfo=UTC),
        validation_criteria=criteria,
    )
    prepare_review = prepare or MagicMock(return_value=_prepared())
    validator = cast(
        TaskValidator,
        SimpleNamespace(prepare_task_review=prepare_review),
    )
    ctx = cast(RegistryContext, SimpleNamespace())
    return await evaluate_close_criteria(
        task=task,
        task_validator=validator,
        ctx=ctx,
        resolved_id=task.id,
        changes_summary=changes_summary,
        diff_text="diff",
        checklist_facts=checklist_facts or {},
        validation_config=None,
        reason=reason,
        description="",
        test_bodies="tests",
        submitted_review=submitted,
    )
