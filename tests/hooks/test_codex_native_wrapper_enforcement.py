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
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
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


@pytest.mark.parametrize("shape", ["single", "batch", "mixed", "computed"])
def test_inner_hook_enforces_wrapper_provenance(
    event_handlers: EventHandlers, tmp_path: Path, shape: str
) -> None:
    workdir = tmp_path / "task"
    source = _source(workdir)
    if shape == "batch":
        source *= 3
    elif shape == "mixed":
        call = source.removeprefix("text(await ").removesuffix(");")
        source = f"await Promise.allSettled([tools.write_stdin({{session_id:123}}), {call}]);"
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
