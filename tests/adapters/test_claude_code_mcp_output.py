"""Claude Code oversized MCP result recovery contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

pytestmark = pytest.mark.unit

_SESSION_ID = "08e60e7e-1f89-4e30-b37e-db276d81a6b6"
_CLOSE_ARGUMENTS = {"task_id": "#1"}
_FULL_RESULT = {"success": True, "closed": True, "task_id": "#1"}
_BOUND_RESULT = f"project/{_SESSION_ID}/tool-results/mcp-gobby-call_tool-1789424128611.txt"


def _post_tool_output(tool_response: str) -> object:
    event = ClaudeCodeAdapter().translate_to_hook_event(
        {
            "hook_type": "post-tool-use",
            "input_data": {
                "session_id": _SESSION_ID,
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": _CLOSE_ARGUMENTS,
                },
                "tool_response": tool_response,
            },
        }
    )
    return event.data["tool_output"]


def _pointer(saved_path: Path) -> str:
    # Claude Code's replacement for an MCP result over its token limit.
    return (
        "Error: result (70,506 characters) exceeds maximum allowed tokens. "
        f"Output has been saved to {saved_path}.\n"
        "Format: JSON with schema: {success: boolean, closed: boolean, task_id: string}"
    )


def _saved(tmp_path: Path, relative: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_FULL_RESULT), encoding="utf-8")
    return path


def test_oversized_mcp_result_reads_the_saved_full_result(tmp_path: Path) -> None:
    assert _post_tool_output(_pointer(_saved(tmp_path, _BOUND_RESULT))) == _FULL_RESULT


@pytest.mark.parametrize(
    "relative",
    [
        "project/another-session/tool-results/mcp-gobby-call_tool-1789424128611.txt",
        f"project/{_SESSION_ID}/tool-results/toolu_01-bash-output.txt",
        f"project/{_SESSION_ID}/mcp-gobby-call_tool-1789424128611.txt",
    ],
    ids=["another-session", "non-mcp-result", "outside-tool-results"],
)
def test_saved_result_not_bound_to_this_session_is_ignored(
    tmp_path: Path,
    relative: str,
) -> None:
    pointer = _pointer(_saved(tmp_path, relative))

    assert _post_tool_output(pointer) == pointer


def test_missing_saved_result_keeps_the_pointer(tmp_path: Path) -> None:
    pointer = _pointer(tmp_path / _BOUND_RESULT)

    assert _post_tool_output(pointer) == pointer


def test_recovered_close_task_result_satisfies_close_hook_condition(tmp_path: Path) -> None:
    # Synthetic copy of a seat's close_task success condition, for a recovered result.
    when = (
        "tool_output.get('closed') is True "
        "or (tool_output.get('result') or {}).get('closed') is True"
    )
    evaluator = SafeExpressionEvaluator(
        {
            "vars": {"assigned_task_id": "#1"},
            "tool_input": _CLOSE_ARGUMENTS,
            "tool_output": _post_tool_output(_pointer(_saved(tmp_path, _BOUND_RESULT))),
        },
        {},
    )

    assert evaluator.evaluate(when) is True
