"""AGY stream-json normalizer tests."""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.acp_stream import StreamEvent
from gobby.adapters.agy_contract import AGY_TOOL_MAP

pytestmark = pytest.mark.unit

SAMPLES = Path("tests/fixtures/provider_contracts/agy/stream-json-samples.jsonl")
CONV = "conv-agy-stream"
_AGY_STREAM_MOD = "gobby.servers.websocket.chat.backends.agy_stream"
_BOOKKEEPING_STEP_TYPES = (
    "user_input",
    "checkpoint",
    "system_message",
    "error_message",
    "unknown",
)


def _agy_stream() -> Any:
    spec = importlib.util.find_spec(_AGY_STREAM_MOD)
    assert spec is not None
    return importlib.import_module(_AGY_STREAM_MOD)


def _parse(record: dict[str, Any] | str | bytes) -> list[StreamEvent]:
    parse = getattr(_agy_stream(), "parse_agy_stream_line", None)
    assert callable(parse)
    if isinstance(record, (bytes, str)):
        return list(parse(record))
    return list(parse(json.dumps(record)))


def _init(**fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "cwd": "/workspace",
        "tools": ["run_command", "write_to_file", "call_mcp_tool"],
        "permission_mode": "always-proceed",
    }
    body.update(fields)
    return {"event": "init", "conversation_id": CONV, "init": body}


def _step(**fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "conversation_id": CONV,
        "step_index": 0,
        "state": "DONE",
    }
    body.update(fields)
    return {"event": "step_update", "step_update": body}


def _result(**fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "conversation_id": CONV,
        "status": "SUCCESS",
        "response": "done",
        "num_turns": 1,
        "duration_seconds": 1.5,
    }
    body.update(fields)
    return {"event": "result", "result": body}


def _usage() -> dict[str, int]:
    return {
        "input_tokens": 10,
        "output_tokens": 4,
        "thinking_tokens": 2,
        "cache_read_tokens": 7,
        "total_tokens": 16,
    }


def _live_payloads() -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for raw in SAMPLES.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        wrapper = json.loads(raw)
        payload = wrapper.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("event"), str):
            payloads.append(payload)
    return payloads


async def _collect_turn(lines: list[str | bytes]) -> list[StreamEvent]:
    iterate = getattr(_agy_stream(), "iter_agy_turn", None)
    assert callable(iterate)

    async def _gen() -> AsyncIterator[str | bytes]:
        for line in lines:
            yield line

    return [event async for event in iterate(_gen())]


def test_agy_stream_module_exists() -> None:
    assert importlib.util.find_spec(_AGY_STREAM_MOD) is not None


def test_live_init_reads_nested_body_not_flat_layout() -> None:
    payload = _live_payloads()[1]
    events = _parse(payload)
    assert len(events) == 1
    assert events[0].event_type == "init"
    assert events[0].data["cwd"] == payload["init"]["cwd"]
    assert events[0].data["model"] == payload["init"]["model"]
    assert events[0].data["permission_mode"] == payload["init"]["permission_mode"]
    assert "tools" in events[0].data


def test_flat_layout_init_does_not_map_sibling_fields() -> None:
    nested = _parse(_init(cwd="/nested-cwd"))
    flat = _parse(
        {
            "event": "init",
            "conversation_id": CONV,
            "cwd": "/flat-cwd",
            "tools": ["run_command"],
            "permission_mode": "always-proceed",
        }
    )
    assert len(nested) == 1
    assert nested[0].event_type == "init"
    assert nested[0].data["cwd"] == "/nested-cwd"
    assert flat == []


def test_text_delta_emits_content_and_result_does_not_repeat_it() -> None:
    events = _parse(
        _step(
            step_index=4,
            state="ACTIVE",
            step_type="agent_response",
            text_delta="Hello café",
        )
    ) + _parse(_result(response="Hello café", usage=_usage()))
    texts = [
        event.data.get("content")
        for event in events
        if event.event_type == "content_delta" and event.data.get("kind") == "text"
    ]
    assert texts == ["Hello café"]
    assert events[-1].event_type == "result"
    assert events[-1].data.get("content") is None


def test_tool_active_done_error_lifecycle() -> None:
    active = _parse(
        _step(
            step_index=3,
            state="ACTIVE",
            step_type="tool",
            tool_name="run_command",
            tool_info={"name": "run_command", "parameters": {"CommandLine": "ls -la"}},
        )
    )
    done = _parse(
        _step(
            step_index=3,
            state="DONE",
            step_type="tool",
            tool_name="run_command",
            tool_info={
                "name": "run_command",
                "parameters": {"CommandLine": "ls -la"},
                "output": "total 8",
            },
        )
    )
    error = _parse(
        _step(
            step_index=5,
            state="ERROR",
            step_type="tool",
            tool_name="list_dir",
            tool_info={
                "name": "list_dir",
                "parameters": {"DirectoryPath": "/tmp"},
                "error": {"type": "TOOL_ERROR", "message": "permission denied"},
            },
        )
    )
    assert len(active) == 1
    assert active[0].data["kind"] == "tool_use"
    assert active[0].data["tool_name"] == "Bash"
    assert active[0].data["call_id"] == f"{CONV}:3"
    assert active[0].data["tool_input"] == {"CommandLine": "ls -la"}
    assert done[0].data["kind"] == "tool_result"
    assert done[0].data["success"] is True
    assert done[0].data["result"] == "total 8"
    assert done[0].data["call_id"] == f"{CONV}:3"
    assert error[0].data["kind"] == "tool_result"
    assert error[0].data["success"] is False
    assert error[0].data["error"] == "permission denied"
    assert error[0].data["call_id"] == f"{CONV}:5"


@pytest.mark.parametrize("step_type", _BOOKKEEPING_STEP_TYPES)
def test_bookkeeping_step_types_emit_nothing(step_type: str) -> None:
    assert _parse(_step(step_type=step_type, step_index=1)) == []


def test_unrecognized_step_type_is_not_malformed() -> None:
    assert _parse(_step(step_type="future_kind", step_index=9)) == []


def test_malformed_and_unknown_records_do_not_terminate() -> None:
    lines = [
        "{not-json",
        json.dumps(["array"]),
        json.dumps({"event": "command_result", "command_result": {"name": "models"}}),
        json.dumps(_step(step_type="checkpoint", step_index=1)),
        json.dumps(_result(usage=_usage())),
    ]
    events: list[StreamEvent] = []
    for line in lines:
        events.extend(_parse(line))
    assert [event.event_type for event in events] == ["result"]


def test_checkpoint_is_not_a_compaction_event() -> None:
    events = _parse(_step(step_type="checkpoint", step_index=1))
    source = inspect.getsource(_agy_stream())
    assert events == []
    assert "compaction" not in source.lower()
    assert "PRE_COMPACT" not in source


def test_shared_stream_vocabulary_is_unchanged() -> None:
    doc = StreamEvent.__doc__ or ""
    assert "init" in doc
    assert "content_delta" in doc
    assert "result" in doc
    assert "error" in doc
    assert "compaction" not in doc.lower()


def test_agy_tool_name_adapter_maps_table_and_mcp_form() -> None:
    adapter = getattr(_agy_stream(), "agy_tool_name_adapter", None)
    assert callable(adapter)
    assert adapter("run_command") == "Bash"
    for raw_name, canonical in AGY_TOOL_MAP.items():
        if raw_name == "call_mcp_tool":
            continue
        assert adapter(raw_name) == canonical
    assert adapter("call_mcp_tool") == "mcp__gobby__call_tool"
    assert (
        adapter(
            "call_mcp_tool",
            {"ServerName": "gobby", "ToolName": "list_mcp_servers"},
        )
        == "mcp__gobby__list_mcp_servers"
    )
    assert (
        adapter(
            "call_mcp_tool",
            {"ServerName": "gobby", "ToolName": "call_tool"},
        )
        == "mcp__gobby__call_tool"
    )


def test_mcp_stream_tool_uses_server_and_tool_identity() -> None:
    events = _parse(
        _step(
            step_index=7,
            state="ACTIVE",
            step_type="tool",
            tool_name="call_mcp_tool",
            tool_info={
                "name": "call_mcp_tool",
                "parameters": {
                    "Arguments": {},
                    "ServerName": "gobby",
                    "ToolName": "list_tools",
                },
            },
        )
    )
    assert events[0].data["tool_name"] == "mcp__gobby__list_tools"


def test_result_usage_is_verbatim_including_cache_read_tokens() -> None:
    usage = _usage()
    events = _parse(_result(usage=usage, response="echoed"))
    assert events[0].event_type == "result"
    assert events[0].data["usage"] == usage
    assert events[0].data["usage"]["cache_read_tokens"] == 7
    assert "cache_read_input_tokens" not in events[0].data
    assert events[0].data.get("input_tokens") is None


@pytest.mark.asyncio
async def test_iter_agy_turn_stops_at_result_and_skips_repeated_init() -> None:
    events = await _collect_turn(
        [
            json.dumps(_init(model="gpt-oss-120b-medium")),
            json.dumps(_init(model="should-skip")),
            json.dumps(_step(step_type="user_input", step_index=0)),
            json.dumps(
                _step(
                    step_index=2,
                    state="ACTIVE",
                    step_type="agent_response",
                    text_delta="turn-one",
                )
            ),
            json.dumps(_result(response="turn-one", status="ERROR", usage=_usage())),
            json.dumps(
                _step(
                    step_index=3,
                    state="ACTIVE",
                    step_type="agent_response",
                    text_delta="should-not-bleed",
                )
            ),
        ]
    )
    assert events[0].event_type == "init"
    assert events[0].data["model"] == "gpt-oss-120b-medium"
    assert sum(1 for event in events if event.event_type == "init") == 1
    assert events[-1].event_type == "result"
    assert events[-1].data["status"] == "ERROR"
    texts = [event.data.get("content") for event in events if event.event_type == "content_delta"]
    assert texts == ["turn-one"]


@pytest.mark.asyncio
async def test_two_persistent_turns_have_no_bleed() -> None:
    iterate = getattr(_agy_stream(), "iter_agy_turn", None)
    assert callable(iterate)
    lines = [
        json.dumps(_init(model="gpt-oss-120b-medium")),
        json.dumps(_step(step_type="checkpoint", step_index=1)),
        json.dumps(
            _step(
                step_index=2,
                state="ACTIVE",
                step_type="agent_response",
                text_delta="first",
            )
        ),
        json.dumps(_result(response="first", usage=_usage())),
        json.dumps(_step(step_type="user_input", step_index=0)),
        json.dumps(
            _step(
                step_index=2,
                state="ACTIVE",
                step_type="agent_response",
                text_delta="second",
            )
        ),
        json.dumps(_result(response="second", num_turns=2, usage=_usage())),
    ]

    async def _gen() -> AsyncIterator[str]:
        for line in lines:
            yield line

    stream = _gen()
    turn1 = [event async for event in iterate(stream)]
    turn2 = [event async for event in iterate(stream)]
    assert turn1[0].event_type == "init"
    assert [event.event_type for event in turn1].count("result") == 1
    assert all(event.event_type != "init" for event in turn2)
    assert [event.data.get("content") for event in turn1 if event.data.get("kind") == "text"] == [
        "first"
    ]
    assert [event.data.get("content") for event in turn2 if event.data.get("kind") == "text"] == [
        "second"
    ]
    assert turn2[-1].data["num_turns"] == 2


@pytest.mark.asyncio
async def test_eof_before_result_yields_one_eof_error() -> None:
    events = await _collect_turn(
        [
            json.dumps(_init()),
            json.dumps(
                _step(
                    step_index=2,
                    state="ACTIVE",
                    step_type="agent_response",
                    text_delta="partial",
                )
            ),
        ]
    )
    errors = [event for event in events if event.event_type == "error"]
    assert len(errors) == 1
    assert errors[0].data["code"] == "eof"


def test_non_ascii_text_delta_round_trips_byte_exact() -> None:
    text = "café 日本語 🎉"
    record = _step(
        step_index=4,
        state="ACTIVE",
        step_type="agent_response",
        text_delta=text,
    )
    payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
    events = _parse(payload)
    assert events[0].data["content"] == text
    source = inspect.getsource(_agy_stream())
    assert "errors=" not in source
    assert "replace" not in source
    assert "\\ufffd" not in source
    assert "\ufffd" not in source
