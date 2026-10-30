"""Native Codex enforces shell provenance at the inner hook it actually delivers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.codex_impl.item_normalization import build_pre_tool_lifecycle_payload
from gobby.hooks._path_scope import current_tool_cwd
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields

pytestmark = pytest.mark.unit

COMMAND = "uv run --no-sync ruff format file.py"


def _call(call_id: str, source: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": call_id,
            "input": source,
        },
    }


def _source(workdir: Path) -> str:
    return (
        f"text(await tools.exec_command({{cmd:{json.dumps(COMMAND)},"
        f"workdir:{json.dumps(str(workdir))},yield_time_ms:1000,max_output_tokens:1800}}));"
    )


def test_outer_wrapper_does_not_claim_native_enforcement(event_handlers: EventHandlers) -> None:
    source = _source(Path("/task")) * 3
    adapter = CodexHooksAdapter()
    event = adapter.translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "tool_name": "functions.exec",
                "tool_input": {"arguments": source},
                "cwd": "/launcher",
            },
        }
    )
    assert event is not None
    response = event_handlers.handle_before_tool(event)
    assert response.decision == "allow", response.reason
    assert event.data["tool_name"] == "functions.exec"


def test_managed_wrapper_keeps_its_delivered_before_hook_guard(
    event_handlers: EventHandlers,
) -> None:
    payload = build_pre_tool_lifecycle_payload(
        {
            "item": {
                "id": "managed-outer",
                "type": "dynamicToolCall",
                "namespace": "functions",
                "tool": "exec",
                "arguments": _source(Path("/task")) * 3,
            }
        }
    )
    assert payload is not None
    name, tool_input = payload
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="managed",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        cwd="/launcher",
        data=normalize_tool_fields({"tool_name": name, "tool_input": tool_input}),
    )
    response = event_handlers.handle_before_tool(event)
    assert response.decision == "block"
    assert "exactly one static exec_command" in (response.reason or "")


@pytest.mark.parametrize("shape", ["single", "batch", "mixed", "race", "any", "computed"])
def test_inner_hook_enforces_wrapper_provenance(
    event_handlers: EventHandlers, tmp_path: Path, shape: str
) -> None:
    workdir = tmp_path / "task"
    source = _source(workdir)
    if shape == "batch":
        source *= 3
    elif shape in {"mixed", "race", "any"}:
        combinator = "allSettled" if shape == "mixed" else shape
        call = source.removeprefix("text(await ").removesuffix(");")
        source = (
            f"text(await Promise.{combinator}([tools.write_stdin({{session_id:123}}), {call}]));"
        )
    elif shape == "computed":
        source = source.replace(json.dumps(str(workdir)), 'load("worktree")')
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps(_call("active", source)) + "\n")
    adapter = CodexHooksAdapter()
    event = adapter.translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "tool_name": "Bash",
                "tool_use_id": "exec-inner",
                "tool_input": {"command": COMMAND},
                "cwd": str(tmp_path / "launcher"),
                "transcript_path": str(transcript),
            },
        }
    )
    assert event is not None
    response = event_handlers.handle_before_tool(event)
    assert response.decision == ("allow" if shape == "single" else "block"), response.reason
    if shape == "single":
        assert current_tool_cwd(event.data) == workdir
    else:
        assert "cannot prove its working directory" in (response.reason or "")
        native = adapter.translate_from_hook_response(response, "PreToolUse")
        assert native["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("history", ["processes", "calls", "cells"])
def test_literal_call_ignores_only_started_process_history_overflow(
    event_handlers: EventHandlers, tmp_path: Path, history: str
) -> None:
    workdir = tmp_path / "task"
    records: list[dict[str, Any]] = []
    for index in range(65):
        call_id = f"old-{index}"
        records.append(_call(call_id, _source(tmp_path / f"old-{index}")))
        if history != "calls":
            output = (
                json.dumps({"session_id": index + 1})
                if history == "processes"
                else f"Script running with cell ID cell-{index}"
            )
            records.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                }
            )
    records.append(_call("active", _source(workdir)))
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    event = CodexHooksAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "tool_name": "Bash",
                "tool_use_id": "exec-inner",
                "tool_input": {"command": COMMAND},
                "cwd": str(tmp_path / "launcher"),
                "transcript_path": str(transcript),
            },
        }
    )
    assert event is not None
    response = event_handlers.handle_before_tool(event)
    assert response.decision == ("allow" if history == "processes" else "block"), response.reason
    if history == "processes":
        assert current_tool_cwd(event.data) == workdir
    else:
        assert "cannot prove its working directory" in (response.reason or "")


def _output(call_id: str, *texts: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": text} for text in texts],
        },
    }


def _yielded_process(call_id: str, session_id: int) -> dict[str, Any]:
    """Codex's completed-cell envelope for an exec_command whose process is still running."""
    result = {"chunk_id": "e6285b", "wall_time_seconds": 1.0, "session_id": session_id}
    return _output(
        call_id,
        "Script completed\nWall time 2.4 seconds\nOutput:\n",
        json.dumps({**result, "original_token_count": 0, "output": ""}),
    )


def _inner_hook(
    event_handlers: EventHandlers,
    tmp_path: Path,
    records: list[dict[str, Any]],
    command: str = COMMAND,
) -> tuple[HookEvent, HookResponse]:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    event = CodexHooksAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "tool_name": "Bash",
                "tool_use_id": "exec-inner",
                "tool_input": {"command": command},
                "cwd": str(tmp_path / "launcher"),
                "transcript_path": str(transcript),
            },
        }
    )
    assert event is not None
    return event, event_handlers.handle_before_tool(event)


@pytest.mark.parametrize("known_session", [False, True])
@pytest.mark.parametrize("shape", ["all", "allSettled", "sequential"])
def test_opaque_poll_wrapper_blocks_pending_sibling_cwd(
    event_handlers: EventHandlers, tmp_path: Path, shape: str, known_session: bool
) -> None:
    records: list[dict[str, Any]] = []
    if known_session:
        records += [_call("started", _source(tmp_path / "old")), _yielded_process("started", 123)]
    records += [
        _call("sibling", _source(tmp_path / "a")),
        _output("sibling", "Script running with cell ID cell-1"),
    ]
    call = _source(tmp_path / "b").removeprefix("text(await ").removesuffix(");")
    poll = "tools.write_stdin({session_id:123})"
    if shape == "sequential":
        source = f"await {poll}; text(await {call}); text(await {call});"
    else:
        source = f"await Promise.{shape}([{poll}, {call}]);"
    records.append(_call("opaque", source))
    event, response = _inner_hook(event_handlers, tmp_path, records)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")


def test_literal_call_after_yielded_validation_processes_keeps_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Replay of a live refusal: earlier validations left their processes running."""
    workdir = tmp_path / "task-worktree"
    env = (
        f"UV_PROJECT_ENVIRONMENT={tmp_path / 'checkout' / '.venv'} "
        f"PYTHONPATH={workdir / 'src'} PYTHONDONTWRITEBYTECODE=1"
    )
    command = (
        f"{env} uv run --no-sync gobby test-quality audit "
        "tests/workflows/test_agent_workspace_scope.py "
        "--baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low"
    )

    def source(cmd: str) -> str:
        return (
            f"text(await tools.exec_command({{cmd:{json.dumps(cmd)},"
            f"workdir:{json.dumps(str(workdir))},max_output_tokens:4000,yield_time_ms:1000}}));\n"
        )

    records: list[dict[str, Any]] = []
    for index in range(63):
        records += [
            _call(f"earlier-{index}", source(f"uv run --no-sync pytest tests/t{index}.py")),
            _yielded_process(f"earlier-{index}", 1000 + index),
        ]
    records += [
        _call("pytest", source(f"{env} uv run --no-sync pytest tests/workflows -q")),
        _yielded_process("pytest", 7172),
        _call("mypy", source(f"{env} uv run --no-sync mypy src/")),
        _yielded_process("mypy", 50054),
        _call("audit", source(command)),
    ]
    event, response = _inner_hook(event_handlers, tmp_path, records, command)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir
