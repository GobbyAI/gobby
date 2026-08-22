from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.plans.review_evidence import register_review_evidence_tools
from gobby.plans.review_evidence_models import ReviewEvidenceError

pytestmark = pytest.mark.unit


def _registry(service: MagicMock) -> InternalToolRegistry:
    registry = InternalToolRegistry(name="test-review-evidence")
    with patch(
        "gobby.mcp_proxy.tools.plans.review_evidence.PlanReviewEvidenceService",
        return_value=service,
    ):
        register_review_evidence_tools(
            registry,
            MagicMock(),
            resolve_project_id=lambda _project: "project-1",
        )
    return registry


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ReviewEvidenceError(
                "invalid_evidence_row",
                "stored plan snapshot is not bytes",
            ),
            {
                "ok": False,
                "error": "invalid_evidence_row",
                "message": "stored plan snapshot is not bytes",
                "retryable": False,
            },
        ),
        (
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            {
                "ok": False,
                "error": "get_plan_review_snapshot_failed",
                "message": "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
            },
        ),
    ],
)
def test_snapshot_expected_failures_return_structured_error(
    error: Exception,
    expected: dict[str, object],
) -> None:
    service = MagicMock()
    service.snapshot_payload.side_effect = error

    tool = _registry(service).get_tool("get_plan_review_snapshot")
    assert tool is not None
    result = tool(evidence_id="evidence-1")

    assert result == expected


@pytest.mark.asyncio
async def test_bound_round_prepare_returns_structured_error() -> None:
    service = MagicMock()
    error = ReviewEvidenceError(
        "review_round_bound",
        "plan review evidence evidence-1 is already bound to agent run run-1",
        retryable=True,
        details={"evidence_id": "evidence-1", "run_id": "run-1"},
    )
    service.prepare_plan_review_round.side_effect = error
    registry = _registry(service)

    tool = registry.get_tool("prepare_plan_review_round")
    assert tool is not None
    result = await tool(plan_path=".gobby/plans/example.md", round_number=1)

    assert result == error.to_dict()
    metadata = registry.get_tool_metadata("bind_evidence_run")
    assert metadata is not None
    assert "replaying the same run_id is idempotent" in metadata.description


@pytest.mark.parametrize(
    ("tool_name", "service_method", "arguments", "fallback"),
    [
        (
            "bind_evidence_run",
            "bind_evidence_run",
            {"evidence_id": "evidence-1", "run_id": "run-1"},
            "bind_evidence_run_failed",
        ),
        (
            "expire_plan_review_evidence",
            "expire_plan_review_evidence",
            {"evidence_id": "evidence-1"},
            "expire_plan_review_evidence_failed",
        ),
        (
            "verify_plan_unchanged",
            "verify_plan_unchanged",
            {"evidence_id": "evidence-1", "plan_path": "/tmp/plan.md"},
            "verify_plan_unchanged_failed",
        ),
        (
            "finalize_plan_review_evidence",
            "finalize_plan_review_evidence",
            {"evidence_id": "evidence-1", "round_result": {}},
            "finalize_plan_review_evidence_failed",
        ),
        (
            "apply_plan_review_repairs",
            "apply_plan_review_repairs",
            {"evidence_id": "evidence-1", "accepted_finding_ids": ["F1"]},
            "apply_plan_review_repairs_failed",
        ),
    ],
)
@pytest.mark.parametrize(
    "error",
    [OSError("filesystem unavailable"), psycopg.OperationalError("database unavailable")],
)
def test_review_evidence_write_boundaries_return_structured_expected_errors(
    tool_name: str,
    service_method: str,
    arguments: dict[str, Any],
    fallback: str,
    error: Exception,
) -> None:
    service = MagicMock()
    getattr(service, service_method).side_effect = error

    tool = _registry(service).get_tool(tool_name)
    assert tool is not None
    result = tool(**arguments)

    assert result == {"ok": False, "error": fallback, "message": str(error)}


def test_apply_plan_review_repairs_error_envelope() -> None:
    service = MagicMock()
    service.apply_plan_review_repairs.side_effect = ReviewEvidenceError(
        "evidence_not_finalized",
        "finalize the rejection checkpoint before applying repairs",
    )

    tool = _registry(service).get_tool("apply_plan_review_repairs")
    assert tool is not None
    result = tool(evidence_id="evidence-1", accepted_finding_ids=["F1"])

    assert result == {
        "ok": False,
        "error": "evidence_not_finalized",
        "message": "finalize the rejection checkpoint before applying repairs",
        "retryable": False,
    }
    service.apply_plan_review_repairs.assert_called_once_with("evidence-1", ["F1"])
