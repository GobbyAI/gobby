"""Deterministic failure categories shared by validation and build tooling."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class FailureCategory(StrEnum):
    """Closed taxonomy for failures that affect task and build progress."""

    ENVIRONMENT = "environment"
    DEPENDENCY = "dependency"
    CODE = "code"
    TEST = "test"
    PROVIDER = "provider"
    TIMEOUT = "timeout"


INFRASTRUCTURE_FAILURE_CATEGORIES = frozenset(
    {
        FailureCategory.ENVIRONMENT,
        FailureCategory.DEPENDENCY,
        FailureCategory.PROVIDER,
        FailureCategory.TIMEOUT,
    }
)

type ValidationStatus = Literal["valid", "invalid", "pending", "error"]


def persisted_validation_status(
    status: ValidationStatus,
    failure_category: FailureCategory | None,
) -> ValidationStatus:
    """Map a validator result to the status stored on the task."""
    if status == "invalid" and failure_category in INFRASTRUCTURE_FAILURE_CATEGORIES:
        return "error"
    return status


_TIMEOUT_MARKERS = (
    "maximum number of turns",
    "max_turns",
    "timed out",
    "timeout",
    "deadline exceeded",
)
_PROVIDER_MARKERS = (
    "http 429",
    "status 429",
    "429 too many requests",
    "rate limit",
    "rate_limit",
    "provider startup failed",
    "provider_startup_failed",
    "overloaded_error",
)
_DEPENDENCY_MARKERS = (
    "uv sync",
    "pip install",
    "dependency resolution",
    "failed to resolve dependencies",
    "no matching distribution found",
    "failed to build wheel",
    "package not found",
)
_ENVIRONMENT_MARKERS = (
    "connection refused",
    "could not connect",
    "command not found",
    "no such file or directory",
    "not a git repository",
    "git checkout",
    "git clone",
    "git fetch",
    "git worktree",
    "git status failed",
    "worktree setup",
    "worktree not found",
    "bootstrap_accounting_stall",
)
_TEST_COMMAND_MARKERS = ("pytest", "unittest", "npm test", "pnpm test", "vitest")
_CODE_COMMAND_MARKERS = ("ruff", "mypy", "pyright", "tsc", "eslint")


def classify_failure(
    text: str | None = None,
    *,
    command: str | None = None,
    timed_out: bool = False,
    default: FailureCategory = FailureCategory.CODE,
) -> FailureCategory:
    """Classify a failure using stable local markers and explicit timeout state."""
    if timed_out:
        return FailureCategory.TIMEOUT

    combined = "\n".join(part for part in (command, text) if part).lower()
    if any(marker in combined for marker in _TIMEOUT_MARKERS):
        return FailureCategory.TIMEOUT
    if any(marker in combined for marker in _PROVIDER_MARKERS):
        return FailureCategory.PROVIDER
    if any(marker in combined for marker in _DEPENDENCY_MARKERS):
        return FailureCategory.DEPENDENCY
    if any(marker in combined for marker in _ENVIRONMENT_MARKERS):
        return FailureCategory.ENVIRONMENT
    if command and any(marker in command.lower() for marker in _TEST_COMMAND_MARKERS):
        return FailureCategory.TEST
    if command and any(marker in command.lower() for marker in _CODE_COMMAND_MARKERS):
        return FailureCategory.CODE
    if "pytest" in combined or "test failed" in combined or "tests failed" in combined:
        return FailureCategory.TEST
    return default


def classify_exception(
    error: BaseException,
    *,
    default: FailureCategory = FailureCategory.PROVIDER,
) -> FailureCategory:
    """Classify an exception, including its cause/context chain."""
    messages: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current))
        current = current.__cause__ or current.__context__
    return classify_failure("\n".join(messages), default=default)
