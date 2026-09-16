"""Grok MCP tool output normalization contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.adapters.grok import GrokAdapter

pytestmark = pytest.mark.unit

_TOOL_USE_ID = "call-close-task-1"
_FULL_OUTPUT = json.dumps({"success": True, "result": {"closed": True}}, indent=2)


def _post_tool_output(okay_output: str) -> object:
    event = GrokAdapter().translate_to_hook_event(
        {
            "hook_type": "post_tool_use",
            "input_data": {
                "hookEventName": "post_tool_use",
                "sessionId": "grok-session",
                "toolName": "gobby__call_tool",
                "toolUseId": _TOOL_USE_ID,
                "toolInput": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#1"},
                },
                "toolResult": {
                    "type": "MCP",
                    "tool_name": "call_tool",
                    "server_name": "gobby",
                    "output": {"OkayOutput": okay_output},
                },
                "toolResultTruncated": False,
            },
        }
    )
    return event.data["tool_output"]


_SAVED_TAIL = (
    "saved to the file above; use `run_terminal_command` to query it (e.g. `jq` or `python3`).]"
)
_LONG_LINE_TAIL = (
    "with a very long line, so grep/read_file are ineffective on it — use "
    "`run_terminal_command` to query the saved file (e.g. `jq` or `python3`).]"
)


def _truncated(spill_path: Path, tail: str = _SAVED_TAIL) -> str:
    # Grok keeps a prefix of a large MCP result and appends one of these notices.
    return (
        _FULL_OUTPUT[:24]
        + "\n\n[MCP output truncated: showing first 0.0 KB of 0.1 KB. Full output written to: "
        + f"{spill_path}. The full output is valid JSON {tail}"
    )


@pytest.mark.parametrize(
    "tail",
    [_SAVED_TAIL, _LONG_LINE_TAIL],
    ids=["saved-to-file-notice", "very-long-line-notice"],
)
def test_truncated_mcp_output_reads_the_spilled_full_result(tmp_path: Path, tail: str) -> None:
    spill = tmp_path / "session" / "mcp" / f"{_TOOL_USE_ID}.json"
    spill.parent.mkdir(parents=True)
    spill.write_text(_FULL_OUTPUT, encoding="utf-8")

    assert _post_tool_output(_truncated(spill, tail)) == {
        "success": True,
        "result": {"closed": True},
    }


@pytest.mark.parametrize(
    "relative_spill",
    ["session/mcp/call-other.json", f"session/{_TOOL_USE_ID}.json"],
    ids=["another-tool-call", "outside-mcp-directory"],
)
def test_spill_file_not_bound_to_this_call_is_ignored(
    tmp_path: Path,
    relative_spill: str,
) -> None:
    spill = tmp_path / relative_spill
    spill.parent.mkdir(parents=True)
    spill.write_text(_FULL_OUTPUT, encoding="utf-8")
    truncated = _truncated(spill)

    assert _post_tool_output(truncated) == truncated


def test_missing_spill_file_keeps_the_truncated_text(tmp_path: Path) -> None:
    truncated = _truncated(tmp_path / "session" / "mcp" / f"{_TOOL_USE_ID}.json")

    assert _post_tool_output(truncated) == truncated
