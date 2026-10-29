"""Native inner shell hooks borrow cwd only from proven pending provider calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks._path_scope import current_tool_cwd
from gobby.hooks.events import tool_execution_id

pytestmark = pytest.mark.unit


def _call(call_id: str, command: str, workdir: str, *, direct: bool = False) -> dict[str, Any]:
    arguments = {"cmd": command, "workdir": workdir}
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call" if direct else "custom_tool_call",
            "name": "exec_command" if direct else "exec",
            "call_id": call_id,
            "arguments" if direct else "input": json.dumps(arguments)
            if direct
            else f"text(await tools.exec_command({json.dumps(arguments)}));",
        },
    }


def _output(call_id: str, output: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


@pytest.mark.parametrize(
    "case",
    [
        "literal",
        "direct",
        "yielded",
        "completed",
        "wrong_command",
        "ambiguous",
        "malformed",
        "partial",
        "computed",
        "overflow",
        "running_process",
        "relative_with_base",
        "relative_without_base",
    ],
)
def test_native_before_workdir_requires_unique_complete_evidence(tmp_path: Path, case: str) -> None:
    root = tmp_path / "session"
    target = tmp_path / "task"
    command = "uv run ruff format file.py"
    records = [_call("outer", command, str(target), direct=case == "direct")]
    if case.startswith("relative_"):
        root = tmp_path / "launcher" / "session"
        records = [_call("outer", command, "../task")]
        if case == "relative_with_base":
            records.insert(
                0, {"type": "turn_context", "payload": {"cwd": str(tmp_path / "provider")}}
            )
            records.append({"type": "turn_context", "payload": {"cwd": str(root)}})
    elif case == "completed":
        records.append(_output("outer", '{"exit_code":0}'))
    elif case == "yielded":
        records.append(_output("outer", "Script running with cell ID cell-1"))
    elif case == "running_process":
        records.append(_output("outer", '{"session_id":123}'))
    elif case == "wrong_command":
        records = [_call("outer", "uv run ruff check file.py", str(target))]
    elif case == "ambiguous":
        records.append(_call("other", command, str(tmp_path / "other")))
    elif case == "computed":
        records[0]["payload"]["input"] = (
            f"text(await tools.exec_command({{cmd:{json.dumps(command)},workdir:target}}));"
        )
    elif case == "overflow":
        records = [_call(f"outer-{i}", command, str(target)) for i in range(65)]
        records.extend(_output(f"outer-{i}", '{"exit_code":0}') for i in range(1, 64))
    text = "".join(json.dumps(record) + "\n" for record in records)
    if case == "malformed":
        text = "{malformed}\n" + text
    elif case == "partial":
        text = text.rstrip("\n")
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(text)
    event = CodexHooksAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "session_id": "native-session",
                "cwd": str(root),
                "tool_name": "Bash",
                "tool_use_id": "exec-inner",
                "tool_input": {"command": command},
                "transcript_path": str(transcript),
            },
        }
    )
    assert event is not None
    assert tool_execution_id(event) == "exec-inner"
    expected = target if case in {"literal", "direct", "yielded", "relative_with_base"} else root
    assert current_tool_cwd(event.data) == expected


@pytest.mark.parametrize("field", ["workdir", "cwd"])
def test_relative_tool_directory_without_event_base_stays_unresolved(field: str) -> None:
    assert current_tool_cwd({"tool_input": {field: "src"}}) is None
    assert current_tool_cwd({"cwd": "relative-session", "tool_input": {field: "src"}}) is None
