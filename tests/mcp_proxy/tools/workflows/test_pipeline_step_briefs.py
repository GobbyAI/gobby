"""Pipeline list briefs must not prefix-slice step errors."""

from __future__ import annotations

from gobby.mcp_proxy.tools.workflows._pipeline_query import _step_summary
from gobby.workflows.pipeline_state import StepExecution, StepStatus


def test_step_summary_omits_error_body() -> None:
    error = "boom " * 80
    summary = _step_summary(
        StepExecution(
            id=1,
            execution_id="exec-1",
            step_id="build",
            status=StepStatus.FAILED,
            error=error,
        )
    )

    assert summary == {
        "step_id": "build",
        "status": StepStatus.FAILED.value,
        "error_present": True,
        "error_chars": len(error),
    }
    assert error not in str(summary)
    assert error[:200] not in str(summary)
