"""Focused tests for stdio proxy error envelopes."""

import pytest

from gobby.mcp_proxy.stdio_results import _request_timeout_result

pytestmark = pytest.mark.unit


def test_spawn_timeout_explains_task_id_recovery() -> None:
    result = _request_timeout_result("/mcp/tools/spawn_agent", 30)

    assert result["error_code"] == "REQUEST_TIMEOUT"
    assert "Retry spawn_agent with the same task_id" in result["error"]
    assert "list_running_agents with that task_id" in result["error"]


def test_other_timeout_does_not_include_spawn_recovery() -> None:
    result = _request_timeout_result("/mcp/tools/get_task", 30)

    assert result == {
        "success": False,
        "error": "Gobby daemon request timed out after 30s while calling /mcp/tools/get_task.",
        "error_code": "REQUEST_TIMEOUT",
    }
