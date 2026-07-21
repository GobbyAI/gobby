from __future__ import annotations

import pytest

from gobby.failure_categories import FailureCategory, classify_exception, classify_failure


@pytest.mark.parametrize(
    ("text", "command", "timed_out", "expected"),
    [
        ("connection refused by PostgreSQL", None, False, FailureCategory.ENVIRONMENT),
        ("uv sync failed to resolve dependencies", None, False, FailureCategory.DEPENDENCY),
        ("3 tests failed", "uv run pytest tests/tasks", False, FailureCategory.TEST),
        ("Found 2 errors", "uv run mypy src/gobby", False, FailureCategory.CODE),
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
