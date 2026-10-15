"""Tests for canonical structured tool outcomes."""

from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.hooks.tool_outcomes import (
    ToolOutcomeStatus,
    classify_raw_tool_result,
    normalize_tool_outcome,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("field", ["exitCode", "exit_code", "returncode"])
def test_structured_exit_code_aliases_are_normalized(field: str) -> None:
    data = {"tool_name": "Bash", "tool_output": {field: 0}}

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.SUCCEEDED
    assert outcome.exit_code == 0
    assert outcome.provenance == f"tool_output.{field}"


def test_json_content_block_exposes_nested_exit_code() -> None:
    data = {
        "tool_name": "functions.exec",
        "tool_output": {
            "status": "completed",
            "content": [
                {
                    "type": "inputText",
                    "text": '{"exit_code": 9, "output": "failed"}',
                }
            ],
        },
    }

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert outcome.exit_code == 9
    assert outcome.provenance == "tool_output.content[0].text.json.exit_code"


@pytest.mark.parametrize("status", ["complete", "completed", "inProgress", "running"])
def test_terminal_or_progress_status_without_result_is_unknown(status: str) -> None:
    data = {"tool_name": "Bash", "tool_output": {"status": status}}

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.UNKNOWN
    assert outcome.provenance == f"tool_output.status:{status.lower()}"


def test_human_readable_exit_code_summary_is_ignored() -> None:
    data = {"tool_name": "Bash", "tool_output": "Process failed with exit code 7"}

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.UNKNOWN
    assert outcome.exit_code is None


def test_functions_wrapper_ignores_outer_success_without_nested_result() -> None:
    data = {
        "_original_tool_name": "functions.exec",
        "tool_name": "Bash",
        "success": True,
        "status": "completed",
        "tool_output": "Script running with cell ID cell-7",
    }

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.UNKNOWN


def test_functions_wrapper_preserves_definitive_outer_failure() -> None:
    data = {
        "tool_name": "functions.exec",
        "success": False,
        "tool_output": "wrapper execution failed",
    }

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert outcome.provenance == "event.success"


def test_explicit_provider_contract_wins_over_structured_signal() -> None:
    data = {"tool_name": "Bash", "tool_output": {"exitCode": 9}}

    outcome = normalize_tool_outcome(
        data,
        explicit_success=True,
        provenance="claude.hook:PostToolUse",
    )

    assert outcome.status is ToolOutcomeStatus.SUCCEEDED
    assert outcome.exit_code is None
    assert outcome.provenance == "claude.hook:PostToolUse"


def test_structured_signal_conflict_is_unknown() -> None:
    data = {
        "tool_name": "Bash",
        "status": "failed",
        "tool_output": {"success": True, "exitCode": 7},
    }

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.UNKNOWN
    assert outcome.exit_code is None


def test_direct_result_wins_over_nested_result() -> None:
    data = {
        "tool_name": "Bash",
        "tool_output": {
            "exitCode": 0,
            "content": {"isError": True},
        },
    }

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.SUCCEEDED
    assert outcome.exit_code == 0
    assert outcome.provenance == "tool_output.exitCode"


def test_equal_trust_direct_result_conflict_is_unknown() -> None:
    data = {
        "tool_name": "Bash",
        "tool_output": {"exitCode": 0, "success": False},
    }

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.UNKNOWN
    assert outcome.exit_code is None
    assert outcome.provenance == ("conflicting_outcomes:tool_output.success|tool_output.exitCode")


def test_canonical_tool_output_precedes_response_envelope_status() -> None:
    data = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_output": {"success": False, "error": "Invalid arguments"},
        "tool_response": {
            "structuredContent": {"success": False, "error": "Invalid arguments"},
            "isError": False,
        },
    }

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert outcome.provenance == "tool_output.success"


def test_null_tool_output_does_not_mask_later_result_alias() -> None:
    outcome = normalize_tool_outcome(
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_output": None,
            "tool_result": {"success": True},
        }
    )

    assert outcome.status is ToolOutcomeStatus.SUCCEEDED
    assert outcome.provenance == "tool_result.success"


@pytest.mark.parametrize("error", ["Invalid or unresolved commit SHA", {"code": "E1"}])
def test_present_error_value_fails_a_raw_result(error: object) -> None:
    outcome = classify_raw_tool_result({"task_id": "t-1", "error": error})

    assert (outcome.status, outcome.provenance) == (ToolOutcomeStatus.FAILED, "raw_result.error")


@pytest.mark.parametrize("error", [None, "", {}])
def test_absent_error_value_keeps_a_raw_result_successful(error: object) -> None:
    outcome = classify_raw_tool_result({"task_id": "t-1", "error": error})

    assert outcome.status is ToolOutcomeStatus.SUCCEEDED


_WIRE_FAILURE = {"success": False, "error": "Invalid or unresolved commit SHA"}


@pytest.mark.parametrize(
    "output",
    [
        _WIRE_FAILURE,
        json.dumps(_WIRE_FAILURE),
        {"content": [{"type": "text", "text": json.dumps(_WIRE_FAILURE)}]},
        {"structuredContent": _WIRE_FAILURE},
        {"isError": False, "content": [{"type": "text", "text": json.dumps(_WIRE_FAILURE)}]},
        {"isError": False, "structuredContent": {"error": "Invalid or unresolved commit SHA"}},
        CallToolResult(
            content=[TextContent(type="text", text=json.dumps(_WIRE_FAILURE))], isError=True
        ),
    ],
)
def test_mcp_wrapped_wire_failure_is_failed(output: object) -> None:
    assert classify_raw_tool_result(output).status is ToolOutcomeStatus.FAILED


@pytest.mark.parametrize("body_key", ["content", "structuredContent"])
def test_mcp_success_wrapper_with_a_null_error_field_succeeds(body_key: str) -> None:
    body = {"task_id": "t-1", "commits": ["abc1234"], "error": None}
    wrapped = [{"type": "text", "text": json.dumps(body)}] if body_key == "content" else body

    outcome = classify_raw_tool_result({"isError": False, body_key: wrapped})

    assert outcome.status is ToolOutcomeStatus.SUCCEEDED


_FAILED_CHILD_RUN = {
    "success": True,
    "status": "error",
    "error": "provider exited 1",
    "result": {"summary": "handoff"},
    "run_id": "run-1",
    "dirty_paths": [],
}


@pytest.mark.parametrize(
    "output",
    [
        _FAILED_CHILD_RUN,
        {"success": True, "status": "failed", "execution_id": "pe-1"},
        {"isError": False, "structuredContent": _FAILED_CHILD_RUN},
    ],
)
def test_explicit_success_true_wins_a_raw_result(output: dict[str, object]) -> None:
    assert classify_raw_tool_result(output).status is ToolOutcomeStatus.SUCCEEDED


def test_conflicting_signals_without_a_success_key_fail_a_raw_result() -> None:
    outcome = classify_raw_tool_result({"exit_code": 1, "status": "ok"})

    assert (outcome.status, outcome.provenance) == (
        ToolOutcomeStatus.FAILED,
        "conflicting_outcomes:tool_output.exit_code|tool_output.status",
    )


_FINAL_VERIFY_DIRTY = {
    "success": False,
    "exit_code": 0,
    "stdout": "",
    "stderr": "",
    "error": "final verification failed: worktree is dirty",
    "dirty_files": [" M src/app.py"],
}


@pytest.mark.parametrize(
    "output",
    [
        _FINAL_VERIFY_DIRTY,
        {"success": False, "status": "ok"},
        {"success": False, "isError": False, "error": "boom"},
    ],
)
def test_top_level_success_false_fails_a_raw_result(output: dict[str, object]) -> None:
    outcome = classify_raw_tool_result(output)

    assert (outcome.status, outcome.provenance) == (
        ToolOutcomeStatus.FAILED,
        "raw_result.success",
    )
