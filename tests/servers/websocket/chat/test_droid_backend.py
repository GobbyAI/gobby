"""Tests for the Droid web-chat backend."""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.adapters.acp_client import ACP_STREAM_READER_LIMIT_BYTES, StreamEvent
from gobby.agents.sandbox import SandboxConfig
from gobby.llm.claude_models import (
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.websocket.chat import permissions
from gobby.servers.websocket.chat.backends.droid import (
    DroidManagedChatSession,
    DroidWebChatBackend,
    _extract_plan_from_tool_args,
    _is_plan_exit_tool,
    _redact_droid_stderr,
    droid_tool_name_adapter,
    parse_droid_stream_line,
)

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path("tests/fixtures/droid/stream_json")
DROID_COMMAND_OUTCOMES_FIXTURE = Path(
    "tests/fixtures/provider_contracts/droid/command-outcomes-0.174.0.json"
)


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [(line + "\n").encode("utf-8") for line in lines]

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, lines: list[str] | None = None, *, stdout: Any | None = None) -> None:
        self.stdin = _FakeStdin()
        self.stdout = stdout if stdout is not None else _FakeStdout(lines or [])
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.terminated = False
        self.killed = False

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _HangingStdout:
    """Yield prefix lines, then hang until the reader is cancelled."""

    def __init__(self, prefix: list[str]) -> None:
        self._prefix = list(prefix)

    async def readline(self) -> bytes:
        if self._prefix:
            return (self._prefix.pop(0) + "\n").encode("utf-8")
        await asyncio.get_running_loop().create_future()
        raise AssertionError("hanging stdout resumed")


class _TimedStdout:
    """Each step is (delay_seconds, line). ``line is None`` hangs after the delay."""

    def __init__(self, steps: list[tuple[float, str | None]]) -> None:
        self._steps = list(steps)

    async def readline(self) -> bytes:
        if not self._steps:
            return b""
        delay, line = self._steps.pop(0)
        if delay > 0:
            await asyncio.sleep(delay)
        if line is None:
            await asyncio.get_running_loop().create_future()
            raise AssertionError("timed stdout hang resumed")
        return (line + "\n").encode("utf-8")


class _PrefixThenTrickleStdout:
    """Emit prefix lines immediately, then a low-rate discarded stream."""

    def __init__(self, prefix: list[str], *, trickle: str, interval_s: float) -> None:
        self._prefix = list(prefix)
        self._trickle = trickle
        self._interval_s = interval_s

    async def readline(self) -> bytes:
        if self._prefix:
            return (self._prefix.pop(0) + "\n").encode("utf-8")
        await asyncio.sleep(self._interval_s)
        return (self._trickle + "\n").encode("utf-8")


def _droid_session(backend: DroidWebChatBackend) -> DroidManagedChatSession:
    """Session carrying the unsandboxed launch snapshot every backend launch requires."""
    return DroidManagedChatSession(
        conversation_id="conv-droid",
        _backend=backend,
        sandbox_config=SandboxConfig(enabled=False),
    )


def _attached_session(
    process: _FakeProcess,
    *,
    prompt_timeout: float | None = None,
) -> tuple[DroidWebChatBackend, DroidManagedChatSession]:
    backend = DroidWebChatBackend(prompt_timeout=prompt_timeout)
    session = _droid_session(backend)
    session.project_path = str(Path.cwd())
    return backend, session


def _fixture_lines(name: str) -> list[str]:
    return FIXTURE_DIR.joinpath(name).read_text(encoding="utf-8").splitlines()


def _session_init_line(model: str = "gpt-5.4") -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "type": "response",
            "factoryApiVersion": "1.0.0",
            "factoryProtocolVersion": "1.25.0",
            "id": "gobby-init-1",
            "result": {
                "sessionId": "droid-session-1",
                "session": {"messages": []},
                "settings": {"modelId": model, "reasoningEffort": "high"},
            },
        }
    )


def _turn_response_lines(text: str) -> list[str]:
    return [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "type": "response",
                "factoryApiVersion": "1.0.0",
                "factoryProtocolVersion": "1.25.0",
                "id": "gobby-message-2",
                "result": {},
            }
        ),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "type": "notification",
                "factoryApiVersion": "1.0.0",
                "factoryProtocolVersion": "1.25.0",
                "method": "droid.session_notification",
                "params": {
                    "notification": {
                        "type": "assistant_text_delta",
                        "messageId": "msg-1",
                        "blockIndex": 0,
                        "textDelta": text,
                    }
                },
            }
        ),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "type": "notification",
                "factoryApiVersion": "1.0.0",
                "factoryProtocolVersion": "1.25.0",
                "method": "droid.session_notification",
                "params": {
                    "notification": {
                        "type": "droid_working_state_changed",
                        "newState": "idle",
                    }
                },
            }
        ),
    ]


def _permission_request_line(
    *,
    request_id: str = "permission-1",
    tool_id: str = "tool-1",
    tool_name: str = "Read",
    tool_input: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "type": "request",
            "factoryApiVersion": "1.0.0",
            "factoryProtocolVersion": "1.25.0",
            "id": request_id,
            "method": "droid.request_permission",
            "params": {
                "toolUses": [
                    {
                        "toolUse": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_input
                            if tool_input is not None
                            else {"file_path": "README.md"},
                        },
                        "confirmationType": "read",
                        "details": {"type": "read", "filePath": "README.md"},
                    }
                ],
                "options": [
                    {"label": "Proceed once", "value": "proceed_once"},
                    {"label": "Cancel", "value": "cancel"},
                ],
            },
        }
    )


def test_droid_tool_name_adapter() -> None:
    assert droid_tool_name_adapter("gobby___list_mcp_servers") == ("mcp__gobby__list_mcp_servers")
    assert droid_tool_name_adapter("mcp__gobby__list") == "mcp__gobby__list"
    assert droid_tool_name_adapter("Execute") == "Bash"
    assert droid_tool_name_adapter("Read") == "Read"


def test_redact_droid_stderr_covers_bearer_and_named_credentials() -> None:
    text = (
        "Authorization: Bearer sk-secret\n"
        "DROID_AUTH_TOKEN=token-secret\n"
        "factory.api-key: key-secret"
    )

    redacted = _redact_droid_stderr(text)

    assert "sk-secret" not in redacted
    assert "token-secret" not in redacted
    assert "key-secret" not in redacted
    assert "Bearer <redacted>" in redacted
    assert "DROID_AUTH_TOKEN=<redacted>" in redacted
    assert "factory.api-key: <redacted>" in redacted


def test_is_plan_exit_tool_matches_regardless_of_separators_and_case() -> None:
    # Droid's spec-mode exit tool plus the cross-CLI plan-exit names, matched as
    # alphanumeric-only lowercase so separators/casing don't matter.
    assert _is_plan_exit_tool("ExitSpecMode")
    assert _is_plan_exit_tool("exit_spec_mode")
    assert _is_plan_exit_tool("ExitPlanMode")
    assert _is_plan_exit_tool("update_plan")
    # Ordinary tools are not plan-exit tools.
    assert not _is_plan_exit_tool("Bash")
    assert not _is_plan_exit_tool("Read")
    assert not _is_plan_exit_tool("Write")


def test_extract_plan_from_tool_args_prefers_known_keys_in_order() -> None:
    assert _extract_plan_from_tool_args({"plan": "the plan"}) == "the plan"
    assert _extract_plan_from_tool_args({"spec": "the spec"}) == "the spec"
    # `plan` outranks `spec` when both are present.
    assert _extract_plan_from_tool_args({"spec": "s", "plan": "p"}) == "p"
    # Blank / non-string / absent values fall through to None so the caller can
    # use the accumulated prose instead.
    assert _extract_plan_from_tool_args({"plan": "   "}) is None
    assert _extract_plan_from_tool_args({"plan": 123}) is None
    assert _extract_plan_from_tool_args({}) is None


def test_parse_stream_json_normalizes_content_blocks() -> None:
    events = []
    for line in _fixture_lines("tool_call.jsonl"):
        events.extend(parse_droid_stream_line(line))

    assert [event.event_type for event in events] == ["content_delta", "content_delta", "result"]
    assert events[0].data["kind"] == "tool_use"
    assert events[0].data["tool_name"] == "gobby___list_mcp_servers"
    assert events[1].data["kind"] == "tool_result"


@pytest.mark.parametrize("is_error", [None, "false", 0])
def test_parse_stream_json_leaves_unproven_tool_result_outcome_unknown(
    is_error: object,
) -> None:
    record: dict[str, Any] = {
        "type": "tool_result",
        "id": "tool-ambiguous",
        "value": "ambiguous output",
    }
    if is_error is not None:
        record["isError"] = is_error

    events = parse_droid_stream_line(json.dumps(record))

    assert len(events) == 1
    assert events[0].data["kind"] == "tool_result"
    assert "success" not in events[0].data


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "permission_request", "kind": "untrusted", "id": "perm-1"},
        {
            "type": "message",
            "role": "assistant",
            "message": {
                "content": [{"type": "permission_request", "kind": "untrusted", "id": "perm-1"}]
            },
        },
    ],
)
def test_parse_stream_json_strips_permission_request_kind(payload: dict[str, Any]) -> None:
    events = parse_droid_stream_line(json.dumps(payload))

    assert len(events) == 1
    assert events[0].event_type == "content_delta"
    assert events[0].data == {"kind": "permission_request", "id": "perm-1"}


@pytest.mark.parametrize("exception_type", [TypeError, KeyError, AttributeError, ValueError])
def test_parse_stream_json_skips_record_conversion_errors(
    exception_type: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    target = "gobby.servers.websocket.chat.backends.droid_stream._stream_events_from_droid_record"
    with patch(target, side_effect=exception_type("bad record")):
        events = parse_droid_stream_line('{"type": "message"}')

    assert events == []
    assert "Skipping malformed Droid stream-json record" in caplog.text


@pytest.mark.asyncio
async def test_managed_session_translates_text_thinking_and_done() -> None:
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session._connected = True
    session._model = "gpt-5.4"
    session._context_window_overrides = {"gpt-5.4": 200_000}

    async def fake_send_message(_session: Any, _prompt: str) -> AsyncIterator[StreamEvent]:
        for line in _fixture_lines("thinking.jsonl"):
            for event in parse_droid_stream_line(line):
                yield event

    backend.send_message = fake_send_message

    events = [event async for event in session.send_message("hello")]

    assert any(isinstance(event, ThinkingEvent) for event in events)
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Done"]
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].context_window == 200_000


@pytest.mark.parametrize(
    "error_type",
    [asyncio.CancelledError, RuntimeError],
    ids=["cancelled", "programming-error"],
)
async def test_managed_session_propagates_stream_errors(
    error_type: type[BaseException],
) -> None:
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session._connected = True

    async def failing_send_message(_session: Any, _prompt: str) -> AsyncIterator[StreamEvent]:
        raise error_type("boom")
        yield

    backend.send_message = failing_send_message

    with pytest.raises(error_type):
        _ = [event async for event in session.send_message("hello")]


@pytest.mark.asyncio
async def test_managed_session_translates_structured_tool_events() -> None:
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session._connected = True

    async def fake_send_message(_session: Any, _prompt: str) -> AsyncIterator[StreamEvent]:
        for line in _fixture_lines("tool_call.jsonl"):
            for event in parse_droid_stream_line(line):
                yield event

    backend.send_message = fake_send_message

    events = [event async for event in session.send_message("list servers")]

    assert isinstance(events[0], ToolCallEvent)
    assert events[0].tool_call_id == "tool-1"
    assert events[0].tool_name == "mcp__gobby__list_mcp_servers"
    assert events[0].server_name == "gobby"
    assert isinstance(events[1], ToolResultEvent)
    assert events[1].tool_call_id == "tool-1"
    assert events[1].success is True
    assert events[1].result == {"success": True}


@pytest.mark.asyncio
async def test_managed_session_leaves_ambiguous_tool_outcome_unknown() -> None:
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session._connected = True
    session._on_post_tool = AsyncMock(return_value=None)

    records = [
        {
            "type": "tool_call",
            "id": "tool-ambiguous",
            "toolName": "Execute",
            "parameters": {"command": "sh -c 'exit 7'"},
        },
        {
            "type": "tool_result",
            "id": "tool-ambiguous",
            "value": "ambiguous output",
        },
    ]

    async def fake_send_message(_session: Any, _prompt: str) -> AsyncIterator[StreamEvent]:
        for record in records:
            for event in parse_droid_stream_line(json.dumps(record)):
                yield event

    backend.send_message = fake_send_message

    events = [event async for event in session.send_message("run ambiguous command")]

    assert any(isinstance(event, ToolResultEvent) for event in events)
    post_tool_payload = session._on_post_tool.await_args.args[0]
    assert "is_error" not in post_tool_payload


@pytest.mark.asyncio
async def test_managed_session_preserves_live_droid_command_outcomes() -> None:
    payload = json.loads(DROID_COMMAND_OUTCOMES_FIXTURE.read_text())
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session._connected = True
    session._on_post_tool = AsyncMock(return_value=None)

    async def fake_send_message(_session: Any, _prompt: str) -> AsyncIterator[StreamEvent]:
        for record in payload["events"]:
            for event in parse_droid_stream_line(json.dumps(record)):
                yield event

    backend.send_message = fake_send_message

    events = [event async for event in session.send_message("run validation")]
    assert any(isinstance(event, ToolResultEvent) and event.success for event in events)
    assert any(isinstance(event, ToolResultEvent) and not event.success for event in events)

    post_tool_payloads = [call.args[0] for call in session._on_post_tool.await_args_list]
    by_error = {
        post_tool_payload["is_error"]: post_tool_payload for post_tool_payload in post_tool_payloads
    }
    assert by_error[False]["tool_name"] == "Bash"
    assert by_error[False]["tool_input"]["command"] == "printf droid-zero-stream"
    assert by_error[False]["tool_response"].startswith("droid-zero-stream")
    assert by_error[True]["tool_name"] == "Bash"
    assert by_error[True]["tool_input"]["command"] == "sh -c 'exit 7'"
    assert by_error[True]["tool_response"].startswith("Error: Command failed (exit code: 7)")


@pytest.mark.asyncio
async def test_send_message_streams_fixture_and_writes_prompt() -> None:
    process = _FakeProcess(_fixture_lines("text_response.jsonl"))
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(Path.cwd())

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ) as create_process,
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("hello")]

    create_process.assert_called_once()
    command = create_process.call_args.args
    assert command[:6] == (
        "/bin/droid",
        "exec",
        "--input-format",
        "stream-jsonrpc",
        "--output-format",
        "stream-jsonrpc",
    )
    assert "--model" in command
    assert process.stdin.writes
    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    assert writes[0]["method"] == "droid.initialize_session"
    assert writes[0]["params"]["cwd"] == str(Path.cwd())
    assert writes[1]["method"] == "droid.add_user_message"
    assert writes[1]["params"]["text"].endswith("hello")
    assert [event.content for event in events if isinstance(event, TextChunk)] == [
        "Hello from Droid"
    ]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_send_message_answers_auto_allowed_permission_request() -> None:
    process = _FakeProcess(
        [_session_init_line()]
        + [
            _turn_response_lines("Approved")[0],
            _permission_request_line(request_id="permission-1"),
            *_turn_response_lines("Approved")[1:],
        ]
    )
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(Path.cwd())

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("read the readme")]

    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    permission_response = writes[2]
    assert permission_response["type"] == "response"
    assert permission_response["id"] == "permission-1"
    assert permission_response["result"] == {"selectedOption": "proceed_once"}
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Approved"]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_send_message_cancels_permission_when_pre_tool_blocks() -> None:
    process = _FakeProcess(
        [_session_init_line()]
        + [
            _turn_response_lines("Denied")[0],
            _permission_request_line(
                request_id="permission-1",
                tool_id="tool-1",
                tool_name="Execute",
                tool_input={"command": "python -c 'print(1)'"},
            ),
            *_turn_response_lines("Denied")[1:],
        ]
    )
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(Path.cwd())
    pre_tool_calls: list[dict[str, Any]] = []

    async def block_pre_tool(payload: dict[str, Any]) -> dict[str, Any]:
        pre_tool_calls.append(payload)
        return {"decision": "block", "reason": "blocked by test"}

    session._on_pre_tool = block_pre_tool

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("run a command")]

    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    permission_response = writes[2]
    assert permission_response["id"] == "permission-1"
    assert permission_response["result"] == {"selectedOption": "cancel"}
    assert pre_tool_calls == [
        {"tool_name": "Bash", "tool_input": {"command": "python -c 'print(1)'"}}
    ]
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Denied"]


@pytest.mark.asyncio
async def test_send_message_waits_for_user_permission_approval() -> None:
    process = _FakeProcess(
        [_session_init_line()]
        + [
            _turn_response_lines("Ran")[0],
            _permission_request_line(
                request_id="permission-1",
                tool_id="tool-1",
                tool_name="Execute",
                tool_input={"command": "python -c 'print(1)'"},
            ),
            *_turn_response_lines("Ran")[1:],
        ]
    )
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(Path.cwd())
    session.chat_mode = "auto"
    approval_calls: list[tuple[str, dict[str, Any]]] = []

    async def approve_tool(tool_use_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        approval_calls.append((tool_name, arguments))
        session.provide_approval(tool_use_id, "approve")

    session._tool_approval_callback = approve_tool

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("run a command")]

    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    permission_response = writes[2]
    assert approval_calls == [("Bash", {"command": "python -c 'print(1)'"})]
    assert permission_response["result"] == {"selectedOption": "proceed_once"}
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Ran"]


@pytest.mark.asyncio
async def test_plan_mode_cancels_unapproved_tool_and_broadcasts_plan() -> None:
    # #15664: in plan mode a tool that would otherwise need interactive approval
    # (here a read-only research Bash that is neither write-blocked nor
    # auto-allowed) must be cancelled deterministically, never awaited. Awaiting
    # it stalls the stream loop so the end-of-stream pending-plan broadcast never
    # fires and the plan card never surfaces in the web UI. With the fix the turn
    # completes and the plan broadcasts, matching Codex/ACP.
    plan_text = "Plan: add multiply helper"
    tagged_plan = f"<proposed_plan>{plan_text}</proposed_plan>"
    process = _FakeProcess(
        [_session_init_line()]
        + [
            _turn_response_lines(tagged_plan)[0],
            _permission_request_line(
                request_id="permission-1",
                tool_id="tool-1",
                tool_name="Execute",
                tool_input={"command": "grep -rn multiply src"},
            ),
            *_turn_response_lines(tagged_plan)[1:],
        ]
    )
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(Path.cwd())
    session.chat_mode = "plan"

    # If plan mode ever falls through to interactive approval, this callback
    # records it (and the assertion below fails) instead of hanging the test.
    approval_calls: list[tuple[str, dict[str, Any]]] = []

    async def approve_tool(tool_use_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        approval_calls.append((tool_name, arguments))
        session.provide_approval(tool_use_id, "approve")

    session._tool_approval_callback = approve_tool

    broadcasts: list[str | None] = []

    async def on_plan_ready(
        content: str | None, input_data: dict[str, Any], tool_use_id: str | None
    ) -> None:
        broadcasts.append(content)

    session._on_plan_ready = on_plan_ready

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("make a plan")]

    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    permission_response = writes[2]
    # Cancelled deterministically — never blocked on interactive approval.
    assert permission_response["result"] == {"selectedOption": "cancel"}
    assert approval_calls == []
    # Stream completed, so the end-of-stream pending-plan broadcast fired once.
    assert broadcasts == [plan_text]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_plan_mode_batch_blocks_destructive_tool_before_exit_spec() -> None:
    backend = DroidWebChatBackend()
    session, broadcasts = _exit_spec_session(backend)
    payload = json.loads(
        _permission_request_line(
            tool_id="tool-write",
            tool_name="Write",
            tool_input={"file_path": "src/gobby/runtime.py", "content": "x"},
        )
    )
    payload["params"]["toolUses"].append(
        {
            "toolUse": {
                "type": "tool_use",
                "id": "tool-exit",
                "name": "ExitSpecMode",
                "input": {"plan": "## Spec\n\n1. Edit runtime"},
            },
            "confirmationType": "edit",
            "details": {"type": "edit", "filePath": "src/gobby/runtime.py"},
        }
    )

    events = parse_droid_stream_line(json.dumps(payload))

    result = await backend._resolve_permission_request(session, events)

    assert result == "cancel"
    assert broadcasts == []
    assert session.has_blocking_plan_decision is False


def _exit_spec_session(
    backend: DroidWebChatBackend,
) -> tuple[DroidManagedChatSession, list[str | None]]:
    """A plan-mode Droid session wired to capture plan broadcasts."""
    session = _droid_session(backend)
    session.project_path = str(Path.cwd())
    session.chat_mode = "plan"
    broadcasts: list[str | None] = []

    async def on_plan_ready(
        content: str | None, input_data: dict[str, Any], tool_use_id: str | None
    ) -> None:
        broadcasts.append(content)

    session._on_plan_ready = on_plan_ready
    return session, broadcasts


async def _park_on_plan_gate(session: DroidManagedChatSession) -> None:
    """Yield until the resolver has broadcast + parked on the decision gate."""
    for _ in range(10):
        await asyncio.sleep(0)
        if session.has_blocking_plan_decision:
            return
    raise AssertionError("resolver did not park on the plan-decision gate")


@pytest.mark.asyncio
async def test_exit_spec_mode_broadcasts_and_blocks_then_approve_proceeds() -> None:
    # #15682: ExitSpecMode must broadcast the spec AND block on the user's
    # decision (mirroring native ExitPlanMode), not cancel-and-drop it. Approve
    # releases with Droid's approve option (proceed_once) so Droid exits Spec
    # Mode and executes.
    backend = DroidWebChatBackend()
    session, broadcasts = _exit_spec_session(backend)
    spec = "## Spec\n\n1. Add multiply helper\n2. Add tests"
    events = parse_droid_stream_line(
        _permission_request_line(tool_name="ExitSpecMode", tool_input={"plan": spec})
    )

    resolve = asyncio.create_task(backend._resolve_permission_request(session, events))
    await _park_on_plan_gate(session)

    # (a) spec broadcast as the authoritative structured plan.
    assert broadcasts == [spec]
    assert session._pending_plan_structured is True
    # (b) the resolver awaits the decision rather than returning immediately.
    assert not resolve.done()
    assert session._plan_exit_blocked_this_turn is True

    # (c) approve releases with proceed_once.
    session.provide_plan_decision(None, "approve")
    result = await asyncio.wait_for(resolve, timeout=1.0)
    assert result == "proceed_once"
    assert session._plan_approved is True
    assert session.has_blocking_plan_decision is False


@pytest.mark.asyncio
async def test_exit_spec_mode_request_changes_cancels_and_queues_feedback() -> None:
    # (d) request_changes releases the gate with cancel (Droid stays in Spec
    # Mode) and the feedback is queued onto the next turn's plan-mode context.
    backend = DroidWebChatBackend()
    session, _broadcasts = _exit_spec_session(backend)
    events = parse_droid_stream_line(
        _permission_request_line(tool_name="ExitSpecMode", tool_input={"plan": "## Spec\n\n1. x"})
    )

    resolve = asyncio.create_task(backend._resolve_permission_request(session, events))
    await _park_on_plan_gate(session)

    session.set_plan_feedback("tighten step 1")
    session.provide_plan_decision(None, "request_changes")
    result = await asyncio.wait_for(resolve, timeout=1.0)

    assert result == "cancel"
    assert session._plan_approved is False
    # Feedback rides the next plan-mode prompt context.
    plan_context = session._pop_plan_mode_context()
    assert plan_context is not None
    assert "tighten step 1" in plan_context


@pytest.mark.asyncio
async def test_exit_spec_mode_decision_gate_times_out_to_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # (e) timeout path returns reject (cancel) so Droid stays in Spec Mode
    # rather than silently proceeding.
    monkeypatch.setattr(permissions, "MANAGED_PLAN_DECISION_TIMEOUT_SECONDS", 0.01)
    backend = DroidWebChatBackend()
    session, broadcasts = _exit_spec_session(backend)
    events = parse_droid_stream_line(
        _permission_request_line(tool_name="ExitSpecMode", tool_input={"plan": "## Spec"})
    )

    result = await backend._resolve_permission_request(session, events)

    assert result == "cancel"
    assert session._plan_approved is False
    assert broadcasts == ["## Spec"]
    assert session.has_blocking_plan_decision is False


@pytest.mark.asyncio
async def test_plan_mode_still_cancels_write_tool_without_blocking() -> None:
    # Criterion 3: side-effecting tools stay cancelled in plan mode and never
    # reach the plan-decision gate.
    backend = DroidWebChatBackend()
    session, broadcasts = _exit_spec_session(backend)
    events = parse_droid_stream_line(
        _permission_request_line(
            tool_name="Write",
            tool_input={"file_path": "src/x.py", "content": "print(1)"},
        )
    )

    result = await backend._resolve_permission_request(session, events)

    assert result == "cancel"
    assert session.has_blocking_plan_decision is False
    assert broadcasts == []


@pytest.mark.asyncio
async def test_plan_mode_blocks_project_local_droid_config() -> None:
    backend = DroidWebChatBackend()
    session, broadcasts = _exit_spec_session(backend)
    events = parse_droid_stream_line(
        _permission_request_line(
            tool_name="Write",
            tool_input={"file_path": ".factory/plans/project-local.md"},
        )
    )

    result = await backend._resolve_permission_request(session, events)

    assert result == "cancel"
    assert session.has_blocking_plan_decision is False
    assert broadcasts == []


@pytest.mark.asyncio
async def test_plan_mode_allows_droid_provider_scratch_write() -> None:
    backend = DroidWebChatBackend()
    session, broadcasts = _exit_spec_session(backend)
    target = str(Path.home() / ".factory" / "scratch" / "state.json")
    events = parse_droid_stream_line(
        _permission_request_line(
            tool_name="Write",
            tool_input={"file_path": target, "content": "{}"},
        )
    )

    result = await backend._resolve_permission_request(session, events)

    assert result == "proceed_once"
    assert session.has_blocking_plan_decision is False
    assert broadcasts == []


@pytest.mark.asyncio
async def test_plan_mode_blocks_mixed_droid_scratch_write() -> None:
    backend = DroidWebChatBackend()
    session, broadcasts = _exit_spec_session(backend)
    scratch = str(Path.home() / ".factory" / "scratch" / "state.json")
    unsafe = str(Path.cwd() / "src" / "unsafe.py")
    events = parse_droid_stream_line(
        _permission_request_line(
            tool_name="Write",
            tool_input={"changes": [{"path": scratch}, {"path": unsafe}]},
        )
    )

    result = await backend._resolve_permission_request(session, events)

    assert result == "cancel"
    assert session.has_blocking_plan_decision is False
    assert broadcasts == []


@pytest.mark.asyncio
async def test_normal_mode_blocks_droid_provider_scratch_write() -> None:
    backend = DroidWebChatBackend()
    session, broadcasts = _exit_spec_session(backend)
    session.chat_mode = "normal"
    target = str(Path.home() / ".factory" / "scratch" / "state.json")
    events = parse_droid_stream_line(
        _permission_request_line(
            tool_name="Write",
            tool_input={"file_path": target, "content": "{}"},
        )
    )

    result = await backend._resolve_permission_request(session, events)

    assert result == "cancel"
    assert broadcasts == []


@pytest.mark.asyncio
async def test_wait_for_plan_decision_times_out_to_reject() -> None:
    backend = DroidWebChatBackend()
    session, _broadcasts = _exit_spec_session(backend)
    session._pending_plan_content = "## Spec"
    session._on_mode_changed = AsyncMock()

    with patch.object(session, "interrupt", new_callable=AsyncMock) as interrupt:
        decision = await session._wait_for_plan_decision(timeout=0.01)

    assert decision == "timeout"
    interrupt.assert_awaited_once()
    session._on_mode_changed.assert_awaited_once_with("plan", "plan_approval_timed_out")
    assert session._pending_plan_content is None
    assert session.has_blocking_plan_decision is False


@pytest.mark.asyncio
async def test_wait_for_plan_decision_cancellation_clears_gate() -> None:
    backend = DroidWebChatBackend()
    session, _broadcasts = _exit_spec_session(backend)

    task = asyncio.create_task(session._wait_for_plan_decision(timeout=30.0))
    await _park_on_plan_gate(session)

    assert session.has_blocking_plan_decision is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.has_blocking_plan_decision is False


async def test_interrupt_releases_parked_plan_decision() -> None:
    backend = DroidWebChatBackend()
    session, _broadcasts = _exit_spec_session(backend)

    task = asyncio.create_task(session._wait_for_plan_decision(timeout=30.0))
    await _park_on_plan_gate(session)

    await session.interrupt()
    decision = await asyncio.wait_for(task, timeout=0.2)

    assert decision == "deny"
    assert session.has_blocking_plan_decision is False


@pytest.mark.asyncio
async def test_send_message_supports_multiple_turns_on_same_process() -> None:
    process = _FakeProcess(
        [_session_init_line()] + _turn_response_lines("First") + _turn_response_lines("Second")
    )
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(Path.cwd())

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        first = [event async for event in session.send_message("one")]
        second = [event async for event in session.send_message("two")]

    writes = [json.loads(write.decode("utf-8")) for write in process.stdin.writes]
    assert [write["method"] for write in writes] == [
        "droid.initialize_session",
        "droid.add_user_message",
        "droid.add_user_message",
    ]
    assert [event.content for event in first if isinstance(event, TextChunk)] == ["First"]
    assert [event.content for event in second if isinstance(event, TextChunk)] == ["Second"]
    assert process.terminated is False


@pytest.mark.asyncio
async def test_send_message_reattaches_dead_process_before_next_turn() -> None:
    processes = [
        _FakeProcess([_session_init_line()] + _turn_response_lines("First")),
        _FakeProcess([_session_init_line()] + _turn_response_lines("Second")),
    ]
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(Path.cwd())

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            side_effect=processes,
        ) as create_process,
    ):
        await backend.attach_session(session, model="gpt-5.4")
        first = [event async for event in session.send_message("one")]
        processes[0].returncode = 0
        second = [event async for event in session.send_message("two")]

    assert create_process.call_count == 2
    first_writes = [json.loads(write.decode("utf-8")) for write in processes[0].stdin.writes]
    second_writes = [json.loads(write.decode("utf-8")) for write in processes[1].stdin.writes]
    assert [write["method"] for write in first_writes] == [
        "droid.initialize_session",
        "droid.add_user_message",
    ]
    assert [write["method"] for write in second_writes] == [
        "droid.initialize_session",
        "droid.add_user_message",
    ]
    assert second_writes[0]["params"]["sessionId"] == "droid-session-1"
    assert [event.content for event in first if isinstance(event, TextChunk)] == ["First"]
    assert [event.content for event in second if isinstance(event, TextChunk)] == ["Second"]


@pytest.mark.asyncio
async def test_eof_before_result_yields_error_event() -> None:
    process = _FakeProcess(_fixture_lines("session_init.jsonl") + _fixture_lines("eof.jsonl"))
    backend = DroidWebChatBackend()
    session = _droid_session(backend)

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session)
        events = [event async for event in session.send_message("hello")]

    assert any(
        isinstance(event, TextChunk) and "Droid stream ended" in event.content for event in events
    )
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_switch_model_respawns_session_object_first() -> None:
    processes = [
        _FakeProcess(_fixture_lines("session_init.jsonl")),
        _FakeProcess([_session_init_line("gpt-5.4-mini")]),
    ]
    backend = DroidWebChatBackend()
    session = _droid_session(backend)

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            side_effect=processes,
        ) as create_process,
    ):
        await backend.attach_session(session, model="gpt-5.4")
        await backend.switch_model(session, "gpt-5.4-mini")

    assert create_process.call_count == 2
    second_command = create_process.call_args_list[1].args
    model_index = second_command.index("--model")
    assert second_command[model_index + 1] == "gpt-5.4-mini"
    assert processes[0].terminated is True
    assert session._model == "gpt-5.4-mini"


def test_public_backend_methods_use_session_object_first_contract() -> None:
    for method_name in ("attach_session", "detach_session", "send_message", "switch_model"):
        params = list(inspect.signature(getattr(DroidWebChatBackend, method_name)).parameters)
        assert params[:2] == ["self", "session"]


@pytest.mark.asyncio
async def test_health_reports_missing_droid() -> None:
    backend = DroidWebChatBackend()

    with patch("gobby.servers.websocket.chat.backends.droid.shutil.which", return_value=None):
        await backend.start()

    health = backend.health()
    assert health.available is False
    assert health.startup_error == "droid CLI not found in PATH"


@pytest.mark.asyncio
async def test_attach_session_rejects_missing_cwd(tmp_path: Path) -> None:
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(tmp_path / "missing")

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        pytest.raises(ValueError, match="working directory does not exist"),
    ):
        await backend.attach_session(session)


@pytest.mark.parametrize(
    ("model", "reasoning_effort", "message"),
    [
        ("--help", None, "Invalid Droid model"),
        ("gpt-5.4", "--help", "Invalid Droid reasoning effort"),
    ],
)
@pytest.mark.asyncio
async def test_attach_session_rejects_invalid_option_values(
    tmp_path: Path,
    model: str,
    reasoning_effort: str | None,
    message: str,
) -> None:
    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(tmp_path)
    session.reasoning_effort = reasoning_effort

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        pytest.raises(ValueError, match=message),
    ):
        await backend.attach_session(session, model=model)


@pytest.mark.asyncio
async def test_attach_session_uses_shared_stream_reader_limit() -> None:
    process = _FakeProcess([_session_init_line()])
    backend, session = _attached_session(process)

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ) as create_process,
    ):
        await backend.attach_session(session, model="gpt-5.4")

    assert create_process.call_args.kwargs["limit"] == ACP_STREAM_READER_LIMIT_BYTES


@pytest.mark.asyncio
async def test_send_message_progress_timeout_emits_one_error_and_is_reconnectable() -> None:
    first = _FakeProcess(stdout=_HangingStdout([_session_init_line()]))
    second = _FakeProcess([_session_init_line(), *_turn_response_lines("Recovered")])
    backend, session = _attached_session(first, prompt_timeout=0.05)

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            side_effect=[first, second],
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        timed_out = [event async for event in session.send_message("hello")]
        assert session.conversation_id not in backend._handles
        recovered = [event async for event in session.send_message("again")]

    error_chunks = [
        event.content
        for event in timed_out
        if isinstance(event, TextChunk)
        and "Timed out waiting for Droid stream progress" in event.content
    ]
    assert len(error_chunks) == 1
    assert isinstance(timed_out[-1], DoneEvent)
    assert first.terminated is True
    assert [event.content for event in recovered if isinstance(event, TextChunk)] == ["Recovered"]
    assert isinstance(recovered[-1], DoneEvent)


@pytest.mark.asyncio
async def test_send_message_progress_timeout_renews_on_parsed_event() -> None:
    _result_ack, text_line, idle_line = _turn_response_lines("Still going")
    del _result_ack
    process = _FakeProcess(
        stdout=_TimedStdout(
            [
                (0.0, _session_init_line()),
                (0.04, text_line),
                (0.04, idle_line),
            ]
        )
    )
    backend, session = _attached_session(process, prompt_timeout=0.05)

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = [event async for event in session.send_message("hello")]

    assert [event.content for event in events if isinstance(event, TextChunk)] == ["Still going"]
    assert isinstance(events[-1], DoneEvent)
    assert process.terminated is False


@pytest.mark.asyncio
async def test_send_message_discarded_lines_do_not_renew_progress_timeout() -> None:
    process = _FakeProcess(
        stdout=_PrefixThenTrickleStdout(
            [_session_init_line()],
            trickle="not-json",
            interval_s=0.01,
        )
    )
    backend, session = _attached_session(process, prompt_timeout=0.05)

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        events = await asyncio.wait_for(
            _collect_chat_events(session.send_message("hello")),
            timeout=1.0,
        )

    error_chunks = [
        event.content
        for event in events
        if isinstance(event, TextChunk)
        and "Timed out waiting for Droid stream progress" in event.content
    ]
    assert len(error_chunks) == 1
    assert process.terminated is True
    assert session.conversation_id not in backend._handles


@pytest.mark.asyncio
async def test_send_message_cancellation_terminates_process_and_clears_handle() -> None:
    hanging = asyncio.Event()

    class _HangAfterPrefix(_HangingStdout):
        async def readline(self) -> bytes:
            if self._prefix:
                return await super().readline()
            hanging.set()
            return await super().readline()

    process = _FakeProcess(stdout=_HangAfterPrefix([_session_init_line()]))
    backend, session = _attached_session(process)

    with (
        patch(
            "gobby.servers.websocket.chat.backends.droid.shutil.which", return_value="/bin/droid"
        ),
        patch(
            "gobby.servers.websocket.chat.backends.droid.asyncio.create_subprocess_exec",
            return_value=process,
        ),
    ):
        await backend.attach_session(session, model="gpt-5.4")
        task = asyncio.create_task(_collect_chat_events(session.send_message("hello")))
        await hanging.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert process.terminated is True
    assert session.conversation_id not in backend._handles


async def _collect_chat_events(stream: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in stream]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_droid_exec_binary_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    droid = shutil.which("droid")
    if droid is None:
        pytest.skip("droid CLI not installed")
    monkeypatch.setenv("HOME", str(tmp_path))

    backend = DroidWebChatBackend()
    session = _droid_session(backend)
    session.project_path = str(tmp_path)

    await backend.attach_session(session)
    assert session._connected is True
    assert "conv-droid" in backend._handles
    await backend.detach_session(session)
    assert session._connected is False
    assert backend._handles == {}
