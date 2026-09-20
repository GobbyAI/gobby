"""Close-review verdict accounting and severity-threshold contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import account_criteria_verdict
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
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


def _finding(severity: FindingSeverity) -> CloseFinding:
    return CloseFinding(
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
