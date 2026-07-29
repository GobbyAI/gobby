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
            OSError("snapshot unavailable"),
            {
                "ok": False,
                "error": "get_plan_review_snapshot_failed",
                "message": "snapshot unavailable",
            },
        ),
        (
            ValueError("invalid page"),
            {
                "ok": False,
                "error": "get_plan_review_snapshot_failed",
                "message": "invalid page",
            },
        ),
    ],
)
def test_snapshot_expected_failures_return_structured_error(
    error: Exception,
    expected: dict[str, object],
) -> None:
    service = MagicMock()
    service.snapshot_page.side_effect = error

    tool = _registry(service).get_tool("get_plan_review_snapshot")
    assert tool is not None
    result = tool(evidence_id="evidence-1")

    assert result == expected


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
