"""A terminal native wrapper failure releases only its uniquely correlated inner call."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.config.app import DaemonConfig
from gobby.hooks.events import HookEventType
from gobby.hooks.hook_manager import HookManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_tool_bindings import TaskToolBindings, cleanup_task_tool_bindings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "case,drop",
    [
        ("startup_error", True),
        ("printed_failure", False),
        ("yielded", False),
        ("two_commands", False),
        ("two_bindings", False),
        ("wrong_command", False),
        ("wrong_directory", False),
        ("partial", False),
        ("malformed", False),
        ("process_receipt", False),
        ("printed_receipt_then_error", False),
        ("throw_after_unprinted_result", False),
        ("non_startup_failure", False),
    ],
)
def test_native_terminal_failure_releases_only_proven_call(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    canonical_task_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    drop: bool,
) -> None:
    tasks = LocalTaskManager(temp_db)
    first = tasks.create_task(sample_project["id"], "First synthetic task")
    second = tasks.create_task(sample_project["id"], "Second synthetic task")
    tasks.claim_task_for_agent(first.id, canonical_task_session.id)
    variables = SessionVariableManager(temp_db)
    bindings = TaskToolBindings(variables, canonical_task_session.id)
    command = "pwd"
    call = f'text(await tools.exec_command({{cmd:"pwd",workdir:{json.dumps(str(tmp_path))}}}));'
    if case == "two_commands":
        call += call
    elif case == "throw_after_unprinted_result":
        call = call.replace("text(await ", "const result=await ").replace("));", ");")
        call += 'throw new Error("synthetic failure");'
    transcript = tmp_path / "rollout.jsonl"
    records: list[dict[str, Any]] = [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "outer-1",
                "input": call,
            },
        }
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records))
    adapter = CodexHooksAdapter()
    raw: dict[str, Any] = {
        "session_id": canonical_task_session.external_id,
        "machine_id": canonical_task_session.machine_id,
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_use_id": "inner-1",
        "tool_input": {"command": "whoami" if case == "wrong_command" else command},
        "transcript_path": str(transcript),
    }
    if case == "wrong_directory":
        raw["tool_input"]["workdir"] = str(tmp_path / "different")
    before = adapter.translate_to_hook_event({"hook_type": "PreToolUse", "input_data": raw})
    assert before is not None
    bindings.start(before)
    if case == "two_bindings":
        other = adapter.translate_to_hook_event(
            {"hook_type": "PreToolUse", "input_data": {**raw, "tool_use_id": "inner-2"}}
        )
        assert other is not None
        bindings.start(other)
    with pytest.raises(ValueError, match="in flight|pending|running|bound"):
        bindings.assert_can_select(second.id)
    variables.merge_variables(
        canonical_task_session.id,
        {
            "claimed_shell_snapshots": {"inner-1": {"root": str(tmp_path)}},
            "worktree_shell_calls": {"inner-1": {"root": str(tmp_path)}},
        },
    )
    failure = "Script failed\nWall time 0.1 seconds\nOutput:\nScript error:\nexec_command failed: CreateProcess"
    if case == "non_startup_failure":
        failure = failure.replace(
            "exec_command failed: CreateProcess", "TypeError: synthetic failure"
        )
    output = (
        "Script completed\nWall time 0.1 seconds\nOutput:\n" + failure
        if case == "printed_failure"
        else failure
    )
    if case in {"yielded", "process_receipt"}:
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "outer-1",
                    "output": "Script running with cell ID cell-1\nWall time 1 seconds\nOutput:\n"
                    if case == "yielded"
                    else '{"session_id":123}',
                },
            }
        )
    records.append(
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "outer-1",
                "output": [
                    {"type": "input_text", "text": output.split("Script error:\n", 1)[0]},
                    {
                        "type": "input_text",
                        "text": "Script error:\n" + output.split("Script error:\n", 1)[1],
                    },
                ],
            },
        }
    )
    if case == "printed_receipt_then_error":
        records[-1]["payload"]["output"].insert(
            1, {"type": "input_text", "text": '{"session_id":123}'}
        )
    content = "".join(json.dumps(row) + "\n" for row in records)
    if case == "partial":
        content = content.rstrip("\n")
    elif case == "malformed":
        content += "{broken}\n"
    transcript.write_text(content)
    next_call = adapter.translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                **raw,
                "tool_name": "mcp__example__read",
                "tool_use_id": "next-call",
                "tool_input": {},
            },
        }
    )
    assert next_call is not None
    assert next_call.event_type == HookEventType.BEFORE_TOOL
    cleanup_task_tool_bindings(next_call, variables, canonical_task_session.id, {})
    if drop:
        bindings.assert_can_select(second.id)
        state = variables.get_variables(canonical_task_session.id)
        assert "inner-1" not in (state.get("claimed_shell_snapshots") or {})
        assert "inner-1" not in (state.get("worktree_shell_calls") or {})
        assert not state.get("task_edited_files")
        assert not state.get("session_dirty_files")
        bindings.clear_pending("prompt")
        late = replace(before, event_type=HookEventType.AFTER_TOOL)
        assert bindings.is_abandoned(late)
        late.metadata["_native_hook_type"] = "PostToolUse"
        late.metadata["_platform_session_id"] = canonical_task_session.id
        late.project_id = sample_project["id"]
        monkeypatch.setattr(HookManager, "_start_health_check_monitoring", lambda self: None)
        monkeypatch.setattr(HookManager, "_reregister_active_sessions", lambda self: None)
        manager = HookManager(
            database=temp_db, session_manager=SessionManager(temp_db), config=DaemonConfig()
        )
        dispatch = Mock(side_effect=AssertionError("An abandoned post must never dispatch"))
        monkeypatch.setattr(manager, "_get_event_handler", dispatch)
        try:
            manager._handle_after_daemon_ready(late)
            dispatch.assert_not_called()
        finally:
            manager.shutdown()
        bindings.complete(late)
        assert bindings.is_abandoned(late)
    else:
        with pytest.raises(ValueError, match="in flight|pending|running|bound"):
            bindings.assert_can_select(second.id)
