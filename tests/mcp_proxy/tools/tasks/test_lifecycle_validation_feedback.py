"""Close-review verdict accounting and severity-threshold contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import account_criteria_verdict
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_close_reviews import TaskCloseReview
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.agentic_close_review import build_terminal_review_payload
from gobby.tasks.close_verdict import (
    FINDING_SEVERITY_ORDER,
    CloseCriterionVerdict,
    CloseFinding,
    CloseVerdict,
    FindingSeverity,
)

pytestmark = pytest.mark.integration


def _task(manager: LocalTaskManager, project_id: str) -> Task:
    return manager.create_task(
        project_id=project_id,
        title="Account close-review verdict",
        category="code",
        validation_criteria="The implementation is correct.",
    )


def _ctx(manager: LocalTaskManager) -> RegistryContext:
    return cast(RegistryContext, SimpleNamespace(task_manager=manager))


def _criterion(*, gap: bool = False) -> CloseCriterionVerdict:
    return CloseCriterionVerdict(
        index=1,
        criterion="The implementation is correct.",
        satisfied=not gap,
        gap="Criterion evidence is incomplete." if gap else None,
    )


def _finding(severity: FindingSeverity, finding_id: str = "finding-1") -> CloseFinding:
    return CloseFinding(
        id=finding_id,
        path="src/review.py",
        start_line=10,
        end_line=12,
        severity=severity,
        category="bug",
        description=f"{severity} code finding",
    )


def test_criterion_gap_blocks_at_the_highest_severity_threshold(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, str(sample_project["id"]))
    verdict = CloseVerdict(status="invalid", criteria=(_criterion(gap=True),), feedback="gap")

    result = account_criteria_verdict(
        task=task,
        verdict=verdict,
        ctx=_ctx(manager),
        resolved_id=task.id,
        validation_config=TaskValidationConfig(close_review_min_severity="critical"),
        reset_reason="close_review_valid",
    )

    assert result.can_close is False
    assert result.error_type == "validation_failed"
    assert result.extra["blocking_reasons"] == ["Criterion evidence is incomplete."]


@pytest.mark.parametrize("minimum", ["critical", "high", "medium", "low"])
@pytest.mark.parametrize("finding_severity", ["critical", "high", "medium", "low"])
def test_code_findings_respect_configured_severity_and_remain_visible(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    minimum: FindingSeverity,
    finding_severity: FindingSeverity,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, str(sample_project["id"]))
    expected_blocking = FINDING_SEVERITY_ORDER[finding_severity] >= FINDING_SEVERITY_ORDER[minimum]
    verdict = CloseVerdict(
        status="invalid" if expected_blocking else "valid",
        criteria=(_criterion(),),
        feedback="reviewed",
        findings=(_finding(finding_severity),),
    )

    result = account_criteria_verdict(
        task=task,
        verdict=verdict,
        ctx=_ctx(manager),
        resolved_id=task.id,
        validation_config=TaskValidationConfig(close_review_min_severity=minimum),
        reset_reason="close_review_valid",
    )

    assert result.can_close is (not expected_blocking)
    assert result.extra["verdict"]["findings"][0]["severity"] == finding_severity
    if expected_blocking:
        assert result.error_type == "validation_failed"
        assert any(
            f"{finding_severity} bug finding" in reason
            for reason in result.extra["blocking_reasons"]
        )
    else:
        assert result.error_type is None
        assert "blocking_reasons" not in result.extra


def test_pending_external_stays_pending_without_gaps_or_blocking_findings(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, str(sample_project["id"]))
    criterion = CloseCriterionVerdict(
        index=1,
        criterion="Live: coordinator verifies the service",
        satisfied=False,
        gap=None,
        state="pending_external",
    )
    verdict = CloseVerdict(status="valid", criteria=(criterion,), feedback="pending")

    result = account_criteria_verdict(
        task=task,
        verdict=verdict,
        ctx=_ctx(manager),
        resolved_id=task.id,
        validation_config=TaskValidationConfig(),
        reset_reason="close_review_valid",
    )

    assert result.can_close is False
    assert result.error_type == "external_pending"
    assert result.extra["pending_external_criteria"] == [criterion.criterion]


def _owner_payload(extra: dict[str, Any], message: str | None) -> dict[str, Any]:
    """Render the verdict's close extra as the owner-visible terminal wake payload."""
    now = datetime(2026, 10, 15, tzinfo=UTC)
    review = TaskCloseReview(
        id="review",
        task_id="task",
        task_ref="#42",
        caller_session_id="owner",
        agent_run_id="run",
        close_arguments={},
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        status="running",
        result_payload=None,
        error=None,
        launched_at=now,
        completed_at=None,
        delivered_at=None,
        created_at=now,
        updated_at=now,
    )
    return build_terminal_review_payload(
        review, status="invalid", close_result=extra, message=message
    )


def test_single_finding_without_gaps_counts_one_and_carries_id(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, str(sample_project["id"]))
    verdict = CloseVerdict(
        status="invalid",
        criteria=(_criterion(),),
        feedback="One bug blocks the close.",
        findings=(_finding("high", "finding-7"),),
    )

    result = account_criteria_verdict(
        task=task,
        verdict=verdict,
        ctx=_ctx(manager),
        resolved_id=task.id,
        validation_config=TaskValidationConfig(close_review_min_severity="low"),
        reset_reason="close_review_valid",
    )
    payload = _owner_payload(result.extra, result.message)

    assert payload["outstanding_finding_count"] == 1
    assert [
        (finding["id"], finding["severity"], finding["description"])
        for finding in payload["outstanding_findings"]
    ] == [("finding-7", "high", "high code finding")]
    assert payload["blocking_reasons"] == [
        "high bug finding finding-7 at src/review.py:10-12: high code finding"
    ]
    assert payload["criterion_gaps"] == []


def test_feedback_summary_is_not_a_finding(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, str(sample_project["id"]))
    feedback = "Criterion 1 lacks evidence and one finding blocks."
    verdict = CloseVerdict(
        status="invalid",
        criteria=(_criterion(gap=True),),
        feedback=feedback,
        findings=(_finding("medium", "finding-2"),),
    )

    result = account_criteria_verdict(
        task=task,
        verdict=verdict,
        ctx=_ctx(manager),
        resolved_id=task.id,
        validation_config=TaskValidationConfig(close_review_min_severity="low"),
        reset_reason="close_review_valid",
    )
    payload = _owner_payload(result.extra, result.message)

    assert payload["review_feedback"] == feedback
    assert feedback not in payload["blocking_reasons"]
    assert payload["criterion_gaps"] == ["Criterion evidence is incomplete."]
    assert [finding["id"] for finding in payload["outstanding_findings"]] == ["finding-2"]
    assert payload["outstanding_finding_count"] == 1
    assert payload["blocking_reasons"] == [
        "Criterion evidence is incomplete.",
        "medium bug finding finding-2 at src/review.py:10-12: medium code finding",
    ]
