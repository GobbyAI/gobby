from __future__ import annotations

import pytest

from gobby.failure_categories import FailureCategory, classify_exception, classify_failure

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "command", "timed_out", "expected"),
    [
        ("connection refused by PostgreSQL", None, False, FailureCategory.ENVIRONMENT),
        ("uv sync failed to resolve dependencies", None, False, FailureCategory.DEPENDENCY),
        ("3 tests failed", "uv run pytest tests/tasks", False, FailureCategory.TEST),
        ("Found 2 errors", "uv run mypy src/gobby", False, FailureCategory.CODE),
        ("whitespace errors", "git diff --check", False, FailureCategory.CODE),
        ("changes found", "git diff --exit-code", False, FailureCategory.CODE),
        ("patch does not apply", "git apply --check", False, FailureCategory.CODE),
        ("tests failed", "git diff --check", False, FailureCategory.TEST),
        ("HTTP 429 Too Many Requests", None, False, FailureCategory.PROVIDER),
        ("anything", None, True, FailureCategory.TIMEOUT),
    ],
)
def test_classify_failure_mapping(
    text: str,
    command: str | None,
    timed_out: bool,
    expected: FailureCategory,
) -> None:
    assert classify_failure(text, command=command, timed_out=timed_out) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Claude SDK hit maximum number of turns", FailureCategory.TIMEOUT),
        ("provider returned status 429", FailureCategory.PROVIDER),
    ],
)
def test_classify_exception_infrastructure(message: str, expected: FailureCategory) -> None:
    assert classify_exception(RuntimeError(message)) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [("PostgreSQL connection refused", FailureCategory.ENVIRONMENT)],
)
def test_classify_exception_uses_infrastructure_cause_chain(
    message: str,
    expected: FailureCategory,
) -> None:
    error = ValueError("validation wrapper failed")
    error.__cause__ = RuntimeError(message)

    assert classify_exception(error) is expected
