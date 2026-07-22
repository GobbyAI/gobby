"""Tests for canonical structured tool outcomes."""

from __future__ import annotations

import pytest

from gobby.hooks.tool_outcomes import ToolOutcomeStatus, normalize_tool_outcome

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


def test_explicit_provider_contract_is_unknown_when_structured_signal_conflicts() -> None:
    data = {"tool_name": "Bash", "tool_output": {"exitCode": 9}}

    outcome = normalize_tool_outcome(
        data,
        explicit_success=True,
        provenance="claude.hook:PostToolUse",
    )

    assert outcome.status is ToolOutcomeStatus.UNKNOWN
    assert outcome.exit_code is None
    assert outcome.provenance == (
        "conflicting_outcomes:claude.hook:PostToolUse|tool_output.exitCode"
    )


def test_structured_signal_conflict_is_unknown() -> None:
    data = {
        "tool_name": "Bash",
        "status": "failed",
        "tool_output": {"success": True, "exitCode": 7},
    }

    outcome = normalize_tool_outcome(data)

    assert outcome.status is ToolOutcomeStatus.UNKNOWN
    assert outcome.exit_code is None
