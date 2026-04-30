"""Tests for MCP proxy result handling helpers."""

from gobby.mcp_proxy.services.result_handling import build_synthetic_tool_output


def test_build_synthetic_tool_output_promotes_error_code() -> None:
    result = {
        "success": False,
        "status": "error",
        "error": "Cannot claim task #1: task is closed",
        "error_code": "TASK_CLOSED",
    }

    wrapped = build_synthetic_tool_output(result)

    assert wrapped["success"] is False
    assert wrapped["status"] == "error"
    assert wrapped["error"] == result["error"]
    assert wrapped["error_code"] == "TASK_CLOSED"
    assert wrapped["result"] == result
