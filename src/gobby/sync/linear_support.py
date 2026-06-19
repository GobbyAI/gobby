"""Shared support for Linear sync orchestration."""

from __future__ import annotations

import logging
import re
from typing import Any, cast

from gobby.tasks.state_semantics import current_stage_state, is_task_closed, is_task_escalated

logger = logging.getLogger("gobby.sync.linear")

_LINEAR_GOBBY_REF_TITLE_RE = re.compile(r"^#(?P<seq>\d+):\s*(?P<title>.+)$")
_LINEAR_FETCH_FAILURE_SUMMARY_INTERVAL = 10


class _RepeatedFetchFailureLimiter:
    """Suppress repeated identical fetch failures while preserving recovery visibility."""

    def __init__(self, *, summary_interval: int) -> None:
        self._summary_interval = summary_interval
        self._message: str | None = None
        self._suppressed_count = 0

    def reset(self) -> None:
        self._message = None
        self._suppressed_count = 0

    def log_failure(self, log: logging.Logger, error: BaseException) -> None:
        message = str(error)
        if message != self._message:
            self._log_changed_failure(log)
            self._message = message
            self._suppressed_count = 0
            log.error("Failed to fetch Linear issues: %s", message)
            return

        self._suppressed_count += 1
        if self._suppressed_count % self._summary_interval == 0:
            log.info(
                "Still failing to fetch Linear issues after %d suppressed repeat(s): %s",
                self._suppressed_count,
                message,
            )
            return

        log.debug(
            "Suppressing repeated Linear issue fetch failure #%d: %s",
            self._suppressed_count,
            message,
        )

    def log_success(self, log: logging.Logger) -> None:
        if self._message is None:
            return
        if self._suppressed_count:
            log.info(
                "Linear issue fetch recovered after %d suppressed repeat(s); last error: %s",
                self._suppressed_count,
                self._message,
            )
        else:
            log.info("Linear issue fetch recovered after previous failure: %s", self._message)
        self.reset()

    def _log_changed_failure(self, log: logging.Logger) -> None:
        if self._message is None or not self._suppressed_count:
            return
        log.info(
            "Linear issue fetch failure changed after %d suppressed repeat(s); previous error: %s",
            self._suppressed_count,
            self._message,
        )


_linear_fetch_failure_limiter = _RepeatedFetchFailureLimiter(
    summary_interval=_LINEAR_FETCH_FAILURE_SUMMARY_INTERVAL
)


class LinearSyncError(Exception):
    """Base exception for Linear sync errors."""


class LinearRateLimitError(LinearSyncError):
    """Raised when Linear API rate limit is exceeded."""

    def __init__(self, message: str, reset_at: int | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class LinearNotFoundError(LinearSyncError):
    """Raised when a Linear resource is not found."""

    def __init__(
        self,
        message: str,
        resource: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.resource = resource
        self.resource_id = resource_id


def _extract_records(result: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []

    value = result.get(key) or result.get("nodes") or result.get("items")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _extract_record(result: Any, key: str) -> dict[str, Any]:
    if isinstance(result, dict):
        value = result.get(key)
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        return cast(dict[str, Any], result)
    return {}


def _gobby_seq_from_linear_title(title: str) -> int | None:
    match = _LINEAR_GOBBY_REF_TITLE_RE.match(title)
    if not match:
        return None
    return int(match.group("seq"))


def _local_title_from_linear(title: str) -> str:
    match = _LINEAR_GOBBY_REF_TITLE_RE.match(title)
    if not match:
        return title
    return match.group("title")


def task_ref(task: Any) -> str:
    seq_num = getattr(task, "seq_num", None)
    return f"#{seq_num}" if seq_num else str(getattr(task, "id", ""))[:8]


def linear_issue_title(task: Any) -> str:
    ref = task_ref(task)
    title = str(getattr(task, "title", "") or "")
    return title if title.startswith(ref) else f"{ref}: {title}"


def decorate_issue_result(
    result: dict[str, Any],
    task: Any,
    *,
    team_id: str,
    project_id: str,
    project_name: str | None = None,
) -> dict[str, Any]:
    decorated = dict(result)
    decorated["gobby_ref"] = task_ref(task)
    decorated["gobby_task_id"] = task.id
    decorated["linear_team_id"] = team_id
    decorated["linear_project_id"] = project_id
    if project_name:
        decorated["linear_project_name"] = project_name
    identifier = decorated.get("identifier")
    if isinstance(identifier, str):
        decorated["linear_identifier"] = identifier
    issue_id = decorated.get("id")
    if isinstance(issue_id, str):
        decorated["linear_issue_id"] = issue_id
    return decorated


def map_gobby_state_to_linear(gobby_state: str) -> str:
    state_map = {
        "ready": "Todo",
        "in_progress": "In Progress",
        "needs_review": "In Review",
        "review_approved": "Done",
        "closed": "Done",
        "escalated": "Canceled",
    }
    return state_map.get(gobby_state, "Todo")


def project_gobby_state_for_linear(task: Any) -> str:
    if is_task_closed(task):
        return "closed"
    if is_task_escalated(task):
        return "escalated"
    return current_stage_state(task) or "ready"


def map_linear_state_to_gobby(linear_state: str) -> str:
    state_map = {
        "Todo": "ready",
        "In Progress": "in_progress",
        "Done": "closed",
        "Canceled": "closed",
        "In Review": "in_progress",
        "Backlog": "ready",
        "Triage": "ready",
    }
    return state_map.get(linear_state, "ready")
