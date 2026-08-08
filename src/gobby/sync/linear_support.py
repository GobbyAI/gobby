"""Shared support for Linear sync orchestration."""

from __future__ import annotations

import logging
import re
from typing import Any, cast

import httpx

from gobby.integrations.mcp_result import MCPToolResultError, parse_mcp_tool_result
from gobby.tasks.state_semantics import current_stage_state, is_task_closed

logger = logging.getLogger("gobby.sync.linear")

_LINEAR_GOBBY_REF_TITLE_RE = re.compile(r"^#(?P<seq>\d+)\s*:\s*(?P<title>.+)$")
_LINEAR_FETCH_FAILURE_SUMMARY_INTERVAL = 10


class _RepeatedFetchFailureLimiter:
    """Suppress repeated identical fetch failures while preserving recovery visibility."""

    def __init__(self, *, summary_interval: int) -> None:
        self._summary_interval = summary_interval
        self._category: str | None = None
        self._message: str | None = None
        self._suppressed_count = 0

    def reset(self) -> None:
        self._category = None
        self._message = None
        self._suppressed_count = 0

    def log_failure(self, log: logging.Logger, error: BaseException) -> None:
        self._log_repeated(
            log,
            error,
            category="failure",
            first_level=logging.ERROR,
            first_message="Failed to fetch Linear issues",
        )

    def log_deferred(self, log: logging.Logger, error: BaseException) -> None:
        self._log_repeated(
            log,
            error,
            category="deferred",
            first_level=logging.WARNING,
            first_message="Deferred Linear issue fetch",
        )

    def _log_repeated(
        self,
        log: logging.Logger,
        error: BaseException,
        *,
        category: str,
        first_level: int,
        first_message: str,
    ) -> None:
        message = str(error)
        if message != self._message or category != self._category:
            self._log_changed_failure(log)
            self._category = category
            self._message = message
            self._suppressed_count = 0
            log.log(first_level, "%s: %s", first_message, message)
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


def is_transient_linear_fetch_error(error: BaseException) -> bool:
    """Return True when a Linear issue-list fetch should be retried later."""
    if isinstance(error, httpx.HTTPStatusError):
        return _is_retryable_http_status(error.response.status_code)
    if isinstance(error, (httpx.TimeoutException, httpx.TransportError)):
        return True
    cause = error.__cause__
    if cause is not None and cause is not error:
        return is_transient_linear_fetch_error(cause)
    return False


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


class LinearSyncError(Exception):
    """Base exception for Linear sync errors."""


def _parse_linear_mcp_result(result: Any) -> Any:
    try:
        return parse_mcp_tool_result(result)
    except MCPToolResultError as exc:
        raise LinearSyncError(f"Linear MCP tool failed: {exc.detail}") from exc


def _extract_records(result: Any, key: str = "issues") -> list[dict[str, Any]]:
    result = _parse_linear_mcp_result(result)

    if isinstance(result, list):
        if not all(isinstance(item, dict) for item in result):
            raise LinearSyncError(f"Invalid Linear MCP response: {key} contains non-object items")
        return cast(list[dict[str, Any]], result)
    if not isinstance(result, dict):
        raise LinearSyncError(
            f"Invalid Linear MCP response for {key}: expected object, got {type(result).__name__}"
        )

    explicit_collection_key = False
    if key in result:
        value = result[key]
        explicit_collection_key = True
    elif "nodes" in result:
        value = result["nodes"]
        explicit_collection_key = True
    elif "items" in result:
        value = result["items"]
        explicit_collection_key = True
    else:
        value = None
    if isinstance(value, dict):
        return _extract_records(value, key)
    if isinstance(value, list):
        if not all(isinstance(item, dict) for item in value):
            raise LinearSyncError(f"Invalid Linear MCP response: {key} contains non-object items")
        return cast(list[dict[str, Any]], value)
    if value is None:
        if explicit_collection_key:
            raise LinearSyncError(f"Invalid Linear MCP response: {key} is null")
        for nested in result.values():
            if isinstance(nested, dict) and any(
                nested_key in nested for nested_key in (key, "nodes", "items")
            ):
                return _extract_records(nested, key)
        if "id" in result:
            raise LinearSyncError(
                f"Invalid Linear MCP response for {key}: expected collection wrapper, "
                "got record object"
            )
        return []
    raise LinearSyncError(
        f"Invalid Linear MCP response for {key}: expected list, got {type(value).__name__}"
    )


def _extract_record(result: Any, key: str) -> dict[str, Any]:
    result = _parse_linear_mcp_result(result)

    if isinstance(result, dict):
        value = result.get(key)
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        if value is not None:
            raise LinearSyncError(
                f"Invalid Linear MCP response for {key}: expected object, "
                f"got {type(value).__name__}"
            )
        return cast(dict[str, Any], result)
    raise LinearSyncError(
        f"Invalid Linear MCP response for {key}: expected object, got {type(result).__name__}"
    )


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
    title = re.sub(rf"^\s*{re.escape(ref)}\s*:\s*", "", title).strip()
    return f"{ref}: {title}"


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
    }
    return state_map.get(gobby_state, "Todo")


def project_gobby_state_for_linear(task: Any) -> str:
    if is_task_closed(task):
        return "closed"
    return current_stage_state(task) or "ready"


def map_linear_state_to_gobby(linear_state: str) -> str:
    state_map = {
        "Todo": "ready",
        "In Progress": "in_progress",
        "Done": "closed",
        "Canceled": "escalated",
        "In Review": "needs_review",
        "Backlog": "ready",
        "Triage": "ready",
    }
    return state_map.get(linear_state, "ready")
