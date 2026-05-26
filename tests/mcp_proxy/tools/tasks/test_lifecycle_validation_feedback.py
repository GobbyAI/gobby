"""Regression tests for lifecycle validation feedback guards."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    feedback_admits_required_validation_failure,
    matched_required_validation_failure_pattern,
    validate_leaf_task_with_llm,
)
from gobby.tasks.validation import ValidationResult as TaskValidationResult

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "feedback",
    [
        "Required verification gate failed: Cargo test did not pass.",
        "CI check did not pass because the Go integration tests are still failing.",
        "Build errors remain in the TypeScript package.",
        "Compiler errors remain in the Rust crate.",
        "Static analysis check failed for the Java service.",
        "The acceptance criteria are not met because the UI workflow is incomplete.",
    ],
)
def test_feedback_admits_required_validation_failure_across_languages(feedback: str) -> None:
    """Required validation failures are detected without Python-specific tool names."""
    assert feedback_admits_required_validation_failure(feedback) is True


@pytest.mark.parametrize(
    ("feedback", "expected"),
    [
        ("Required validation\n\ncheck did not\npass after retry.", True),
        ("The criteria not satisfied by the delivered implementation.", True),
        ("Validation errors remain unresolved in the frontend package.", True),
        ("Mypy found incomplete type hints in the service boundary.", True),
        ("The implementation mentions criteria and satisfied users.", False),
        ("Errors were documented and resolved before closure.", False),
        ("Mypy hints were improved and all work is complete.", False),
    ],
)
def test_multiline_and_specific_pattern_variants(feedback: str, expected: bool) -> None:
    """Failure feedback detection handles multiline positives without near-miss matches."""
    assert feedback_admits_required_validation_failure(feedback) is expected


def test_matched_pattern_helper_returns_the_triggering_pattern() -> None:
    """The override path can log the concrete pattern that matched."""
    feedback = "Required verification gate failed: cargo test failed."

    pattern = matched_required_validation_failure_pattern(feedback)

    assert pattern is not None
    assert pattern.search("verification gate failed") is not None
    assert feedback_admits_required_validation_failure(feedback) is True


@pytest.mark.parametrize("feedback", [None, "", "   ", "All checks pass."])
def test_matched_pattern_helper_returns_none_for_non_failures(feedback: str | None) -> None:
    """Empty or successful feedback does not report a matched pattern."""
    assert matched_required_validation_failure_pattern(feedback) is None


@pytest.mark.parametrize(
    "feedback",
    [
        "Tests were added for the new behavior.",
        "The build configuration was updated and validation can run locally.",
        "Static analysis coverage was expanded.",
    ],
)
def test_feedback_without_failure_admission_is_allowed(feedback: str) -> None:
    """Mentioning validation concepts alone is not treated as an admitted failure."""
    assert feedback_admits_required_validation_failure(feedback) is False


@pytest.mark.parametrize(
    "feedback",
    [
        "tests: 10 passed, 0 failed",
        "Validation summary: zero failures and all checks passed.",
        "pytest report: failed=0, passed=18",
    ],
)
def test_zero_failure_summaries_do_not_admit_failure(feedback: str) -> None:
    """Benign zero-count failure tokens are ignored before failure-pattern matching."""
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


@pytest.mark.parametrize(
    "feedback",
    [
        "tests: 9 passed, 1 failed",
        "Validation summary: 2 failures remain.",
        "Tests are failing in the required check.",
        "The validation gate did not pass.",
    ],
)
def test_nonzero_and_explicit_failure_summaries_still_admit_failure(feedback: str) -> None:
    """Nonzero and explicit failure summaries still block lifecycle closure."""
    assert feedback_admits_required_validation_failure(feedback) is True


@pytest.mark.asyncio
async def test_valid_llm_result_with_failure_feedback_is_overridden_to_invalid() -> None:
    """A valid status cannot close when feedback admits a required validation failure."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="valid",
                feedback="Required validation gate did not pass.",
            )
        )
    )
    ctx = SimpleNamespace(task_manager=SimpleNamespace(update_task=update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is False
    assert result.extra == {"validation_status": "invalid"}
    update_task.assert_called_once_with(
        "task-1",
        validation_status="invalid",
        validation_feedback="Required validation gate did not pass.",
    )


@pytest.mark.asyncio
async def test_valid_llm_result_with_zero_failure_feedback_remains_valid() -> None:
    """A valid status stays valid when feedback only reports zero failures."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="valid",
                feedback="tests: 10 passed, 0 failed",
            )
        )
    )
    ctx = SimpleNamespace(task_manager=SimpleNamespace(update_task=update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is True
    update_task.assert_called_once_with(
        "task-1",
        validation_status="valid",
        validation_feedback="tests: 10 passed, 0 failed",
    )
