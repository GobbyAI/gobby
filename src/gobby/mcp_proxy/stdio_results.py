"""Shared result helpers for the stdio MCP proxy."""

from typing import Any

REMOVED_WORKFLOW_WAIT_TOOL = "wait_for_completion"
DAEMON_HEALTH_ATTEMPTS = 30
DAEMON_HEALTH_CHECK_TIMEOUT_SECONDS = 2.0
DAEMON_HEALTH_RETRY_DELAY_SECONDS = 1.0
DAEMON_PROXY_PREFLIGHT_TIMEOUT_SECONDS = 2.0
DAEMON_PROXY_PREFLIGHT_CACHE_SECONDS = 5.0


def _strip_none(obj: Any) -> Any:
    """Recursively strip None values from dicts.

    Prevents ``null`` fields in JSON payloads sent over MCP, which break
    strict Jinja prompt templates (e.g. Nemotron Super in LMStudio).
    The MCP SDK's ``exclude_none`` only covers Pydantic model fields —
    raw dicts like ``inputSchema`` pass through unchanged.
    """
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(item) for item in obj]
    return obj


def _daemon_unavailable_result(port: int, detail: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            f"Gobby daemon HTTP control plane is unavailable at localhost:{port}: {detail}. "
            "Check `gobby status` or restart with `gobby restart --verbose`."
        ),
        "error_code": "DAEMON_UNAVAILABLE",
    }


def _request_timeout_result(path: str, timeout: float) -> dict[str, Any]:
    return {
        "success": False,
        "error": f"Gobby daemon request timed out after {timeout:g}s while calling {path}.",
        "error_code": "REQUEST_TIMEOUT",
    }


def _removed_wait_for_completion_result() -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            "gobby-workflows.wait_for_completion was removed. Start the agent or pipeline, "
            "persist its run_id or execution_id, then resume from the daemon's durable "
            "completion notification and inspect get_task, get_agent_result, or "
            "get_pipeline_status."
        ),
        "error_code": "TOOL_REMOVED",
        "server_name": "gobby-workflows",
        "tool_name": REMOVED_WORKFLOW_WAIT_TOOL,
    }
