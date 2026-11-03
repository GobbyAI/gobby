"""A terminal native wrapper failure releases only its uniquely correlated inner call."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.codex_impl.native_exec_failures import failed_native_outer_calls
from gobby.config.app import DaemonConfig
from gobby.hooks.checkout_reads import delivery_scope, node_origin
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_tool_bindings import TaskToolBindings, pending_fencing_calls

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
        ("computed_simple_cell", False),
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
    elif case == "computed_simple_cell":
        call = call.replace('cmd:"pwd"', "cmd:globalThis.command")
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
    assert pending_fencing_calls(variables.get_variables(canonical_task_session.id), second.id)
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
    bindings.reconcile_native_failures(next_call)
    if drop:
        state = variables.get_variables(canonical_task_session.id)
        assert pending_fencing_calls(state, second.id) == []
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
            with monkeypatch.context() as unavailable:
                unavailable.setattr(
                    TaskToolBindings,
                    "reconcile_native_failures",
                    Mock(side_effect=RuntimeError("synthetic reconciliation outage")),
                )
                manager._handle_after_daemon_ready(late)
                dispatch.assert_not_called()
        finally:
            manager.shutdown()
        bindings.complete(late)
        assert bindings.is_abandoned(late)
    else:
        state = variables.get_variables(canonical_task_session.id)
        assert pending_fencing_calls(state, second.id)


def test_successful_native_post_does_not_read_rollout(
    temp_db: HubDatabase,
    canonical_task_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = TaskToolBindings(SessionVariableManager(temp_db), canonical_task_session.id)
    before = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=canonical_task_session.external_id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        request_id="inner-normal",
        data={"tool_name": "Bash", "transcript_path": str(tmp_path / "rollout.jsonl")},
        metadata={
            "_native_hook_type": "PreToolUse",
            "_native_shell_outer_call_id": "outer-normal",
        },
    )
    bindings.start(before)
    post = replace(
        before, event_type=HookEventType.AFTER_TOOL, metadata={"_native_hook_type": "PostToolUse"}
    )
    read = Mock(side_effect=AssertionError("A successful post must not scan its own rollout"))
    monkeypatch.setattr(Path, "open", read)
    bindings.reconcile_native_failures(post)
    read.assert_not_called()
    bindings.complete(post)
    assert not bindings.is_abandoned(post)


def test_nested_rollout_is_not_abandonment_evidence(tmp_path: Path) -> None:
    transcript = tmp_path / "nested.jsonl"
    transcript.write_text('{"nested":' + "[" * 200_000 + "0" + "]" * 200_000 + "}\n")
    assert failed_native_outer_calls(str(transcript), {"outer-synthetic"}) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pre_explicit", "pre_inferred", "post_reconcile"])
async def test_node_native_hooks_never_open_hub_transcripts(
    temp_db: HubDatabase,
    canonical_task_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    bindings = TaskToolBindings(SessionVariableManager(temp_db), canonical_task_session.id)
    bindings.start(
        HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=canonical_task_session.external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            request_id="pending-inner",
            data={"tool_name": "Bash"},
            metadata={"_native_shell_outer_call_id": "pending-outer"},
        )
    )
    monkeypatch.setattr(
        "gobby.hooks.checkout_reads.get_machine_id",
        lambda: "00000000-0000-0000-0000-000000000111",
    )
    remote = "00000000-0000-0000-0000-000000000222"
    raw: dict[str, Any] = {
        "session_id": canonical_task_session.external_id,
        "machine_id": remote,
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_use_id": "unrelated-inner",
        "tool_input": {"command": "pwd"},
        "transcript_path": str(tmp_path / "node-supplied-transcript"),
    }
    if case == "pre_explicit":
        raw["tool_input"]["workdir"] = str(tmp_path)
    read = Mock(side_effect=AssertionError("The hub must never open a node transcript"))
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "open", read)
        with delivery_scope(remote, asyncio.get_running_loop(), str(tmp_path)):
            assert node_origin()
            event = CodexHooksAdapter().translate_to_hook_event(
                {
                    "hook_type": "PostToolUse" if case == "post_reconcile" else "PreToolUse",
                    "input_data": raw,
                }
            )
            assert event is not None
            bindings.reconcile_native_failures(event)
    read.assert_not_called()
