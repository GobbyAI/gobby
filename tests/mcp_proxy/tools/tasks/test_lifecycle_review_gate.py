"""Focused tests for background close-review staleness."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

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

_LAUNCH_FACTS: dict[str, object] = {
    "commit_count": 1,
    "commit_shas": ["abc1234"],
    "had_attributed_edits": True,
    "attributed_paths": ["src/gobby/tasks/validation.py"],
    "claim_started_at": "2026-09-04T10:00:00+00:00",
}


def _prepared(stable_facts: dict[str, object]) -> PreparedCloseReview:
    return PreparedCloseReview(
        prompt="prompt",
        criteria=("Criterion.",),
        prompt_chars=6,
        prompt_limit=100,
        review_fingerprint="review",
        evidence_fingerprint="evidence",
        diff_sha="diff",
        test_bodies_sha="tests",
        stable_facts=stable_facts,
        manifest_count=1,
        excerpt_chars=4,
    )


async def _evaluate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    submitted_facts: dict[str, object],
    current_facts: dict[str, object],
) -> tuple[ValidationResult, MagicMock, ValidationResult]:
    accounted = ValidationResult(can_close=True)
    account = MagicMock(return_value=accounted)
    monkeypatch.setattr(review_gate, "account_criteria_verdict", account)
    task = Task(
        id="task",
        project_id="project",
        title="Task",
        priority=2,
        task_type="task",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
        updated_at=datetime(2026, 9, 4, tzinfo=UTC),
        validation_criteria="Criterion.",
    )
    validator = cast(
        TaskValidator,
        SimpleNamespace(prepare_task_review=MagicMock(return_value=_prepared(current_facts))),
    )
    result = await evaluate_close_criteria(
        task=task,
        task_validator=validator,
        ctx=cast(RegistryContext, SimpleNamespace()),
        resolved_id=task.id,
        changes_summary="summary",
        diff_text="diff",
        checklist_facts={},
        validation_config=None,
        reason="completed",
        description="",
        test_bodies="tests",
        submitted_review=SubmittedCloseReview(
            verdict={
                "status": "valid",
                "criteria": [{"index": 1, "satisfied": True, "gap": None}],
                "feedback": "ok",
            },
            review_fingerprint="review",
            evidence_fingerprint="evidence",
            diff_sha="diff",
            test_bodies_sha="tests",
            stable_facts=submitted_facts,
        ),
    )
    return result, account, accounted


async def test_claim_release_with_unchanged_commit_set_does_not_stale_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    after_release = {
        **_LAUNCH_FACTS,
        "claim_started_at": "2026-09-01T09:00:00+00:00",
    }

    result, account, accounted = await _evaluate(
        monkeypatch,
        submitted_facts=dict(_LAUNCH_FACTS),
        current_facts=after_release,
    )

    assert result is accounted
    account.assert_called_once()


async def test_force_claim_with_unchanged_commit_set_does_not_stale_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    after_force_claim = {
        **_LAUNCH_FACTS,
        "claim_started_at": "2026-09-04T11:00:00+00:00",
    }

    result, account, accounted = await _evaluate(
        monkeypatch,
        submitted_facts=dict(_LAUNCH_FACTS),
        current_facts=after_force_claim,
    )

    assert result is accounted
    account.assert_called_once()


async def test_changed_commit_set_stales_review_with_named_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_new_commit = {
        **_LAUNCH_FACTS,
        "commit_count": 2,
        "commit_shas": ["abc1234", "def5678"],
    }

    result, account, _accounted = await _evaluate(
        monkeypatch,
        submitted_facts=dict(_LAUNCH_FACTS),
        current_facts=with_new_commit,
    )

    assert result.error_type == "agentic_review_stale"
    assert "commit set" in result.extra["invalidating_delta"]
    account.assert_not_called()
