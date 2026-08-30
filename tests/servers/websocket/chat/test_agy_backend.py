"""Tests for the AGY web-chat backend."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.acp_client import (
    ACP_STREAM_READER_LIMIT_BYTES,
    DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS,
)
from gobby.adapters.agy import AgyAdapter
from gobby.hooks.events import HookResponse
from gobby.llm.claude_models import DoneEvent, TextChunk, ToolCallEvent, ToolResultEvent
from gobby.servers.chat_stream_transport import ChatStreamTransport
from gobby.servers.websocket.chat._stream_events import (
    ChatStreamEventHandler,
    ChatStreamEventState,
)
from gobby.servers.websocket.chat._stream_persistence import ChatStreamPersistence
from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks

pytestmark = pytest.mark.unit

_AGY_MOD = "gobby.servers.websocket.chat.backends.agy"
CONV = "agy-upstream-1"
GOBBY_CONV = "gobby-web-conv"


def _agy() -> Any:
    spec = importlib.util.find_spec(_AGY_MOD)
    assert spec is not None
    return importlib.import_module(_AGY_MOD)


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [(line + "\n").encode("utf-8") for line in lines]
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def readline(self) -> bytes:
        if self._closed or not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise BrokenPipeError("stdin closed")
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, lines: list[str] | None = None, *, stdout: Any | None = None) -> None:
        self.stdin = _FakeStdin()
        self.stdout = stdout if stdout is not None else _FakeStdout(lines or [])
        self.stderr = None
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.terminated = False
        self.killed = False
        self.pid = 4242

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        closer = getattr(self.stdout, "close", None)
        if callable(closer):
            closer()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        closer = getattr(self.stdout, "close", None)
        if callable(closer):
            closer()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _HangingStdout:
    def __init__(self, prefix: list[str]) -> None:
        self._prefix = list(prefix)
        self._closed = asyncio.Event()

    def close(self) -> None:
        self._closed.set()

    async def readline(self) -> bytes:
        if self._prefix:
            return (self._prefix.pop(0) + "\n").encode("utf-8")
        await self._closed.wait()
        return b""


class _TimedStdout:
    def __init__(self, steps: list[tuple[float, str | None]]) -> None:
        self._steps = list(steps)
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def readline(self) -> bytes:
        if self._closed or not self._steps:
            return b""
        delay, line = self._steps.pop(0)
        if delay > 0:
            await asyncio.sleep(delay)
        if line is None:
            await asyncio.get_running_loop().create_future()
            raise AssertionError("timed stdout hang resumed")
        return (line + "\n").encode("utf-8")


class _PrefixThenTrickleStdout:
    def __init__(self, prefix: list[str], *, trickle: str, interval_s: float) -> None:
        self._prefix = list(prefix)
        self._trickle = trickle
        self._interval_s = interval_s
        self._closed = asyncio.Event()

    def close(self) -> None:
        self._closed.set()

    async def readline(self) -> bytes:
        if self._prefix:
            return (self._prefix.pop(0) + "\n").encode("utf-8")
        if self._closed.is_set():
            return b""
        try:
            await asyncio.wait_for(self._closed.wait(), timeout=self._interval_s)
            return b""
        except TimeoutError:
            return (self._trickle + "\n").encode("utf-8")


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def base_msg(self, **fields: Any) -> dict[str, Any]:
        return dict(fields)

    async def safe_send(self, msg: dict[str, Any]) -> bool:
        self.sent.append(msg)
        return True


class _RecordingPersistence:
    def __init__(self) -> None:
        self.done_events: list[Any] = []
        self.sdk_ids: list[str | None] = []

    async def persist_current_assistant(self, session: Any) -> None:
        return None

    def session_ref(self) -> str | None:
        return None

    async def persist_sdk_session_id(self, session: Any, sdk_sid: str | None) -> None:
        self.sdk_ids.append(sdk_sid)

    async def persist_done_metadata(self, session: Any, event: Any) -> None:
        self.done_events.append(event)


def _init(**fields: Any) -> str:
    body: dict[str, Any] = {"cwd": "/workspace", "model": "gemini-3-flash", "tools": []}
    body.update(fields)
    return json.dumps({"event": "init", "conversation_id": CONV, "init": body})


def _text(text: str, *, step_index: int = 0) -> str:
    return json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": CONV,
                "step_index": step_index,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": text,
            },
        }
    )


def _result(
    *,
    status: str = "SUCCESS",
    usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> str:
    body: dict[str, Any] = {
        "conversation_id": CONV,
        "status": status,
        "num_turns": 1,
        "duration_seconds": 0.4,
    }
    if usage is not None:
        body["usage"] = usage
    if error is not None:
        body["error"] = error
    return json.dumps({"event": "result", "result": body})


def _usage() -> dict[str, int]:
    return {
        "input_tokens": 10,
        "output_tokens": 4,
        "thinking_tokens": 2,
        "cache_read_tokens": 7,
        "total_tokens": 16,
    }


def _tool_active(*, output: str | None = None) -> str:
    info: dict[str, Any] = {"name": "write_to_file", "parameters": {"TargetFile": "a.py"}}
    if output is not None:
        info["output"] = output
    return json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": CONV,
                "step_index": 3,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "write_to_file",
                "tool_info": info,
            },
        }
    )


def _bookkeeping(step_type: str) -> str:
    return json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": CONV,
                "step_index": 8,
                "state": "DONE",
                "step_type": step_type,
            },
        }
    )


def _turn_lines(text: str) -> list[str]:
    return [_text(text), _result(usage=_usage())]


def _session(
    _process: _FakeProcess,
    *,
    prompt_timeout: float | None = None,
    project_path: str | None = None,
) -> tuple[Any, Any]:
    mod = _agy()
    backend = mod.AgyWebChatBackend(prompt_timeout=prompt_timeout)
    session = mod.AgyManagedChatSession(conversation_id=GOBBY_CONV, _backend=backend)
    session.project_path = project_path or str(Path.cwd())
    session.db_session_id = "sess-agy-web"
    session.project_id = "proj-agy"
    return backend, session


def _spawn_patches(process: Any, *, side_effect: Any | None = None) -> tuple[Any, Any]:
    which = patch(f"{_AGY_MOD}.shutil.which", return_value="/bin/agy")
    create = patch(
        f"{_AGY_MOD}.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process if side_effect is None else None,
        side_effect=side_effect,
    )
    return which, create


async def _collect(stream: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in stream]


def test_agy_backend_module_exists() -> None:
    assert importlib.util.find_spec(_AGY_MOD) is not None


def test_public_backend_methods_use_session_object_first_contract() -> None:
    backend_cls = _agy().AgyWebChatBackend
    for method_name in ("attach_session", "detach_session", "send_message", "switch_model"):
        params = list(inspect.signature(getattr(backend_cls, method_name)).parameters)
        assert params[:2] == ["self", "session"]


@pytest.mark.asyncio
async def test_health_reports_missing_agy() -> None:
    backend = _agy().AgyWebChatBackend()
    with patch(f"{_AGY_MOD}.shutil.which", return_value=None):
        await backend.start()
    health = backend.health()
    assert health.available is False
    assert health.startup_error == "agy CLI not found in PATH"


@pytest.mark.asyncio
async def test_first_turn_spawns_documented_argv_and_streams_text(tmp_path: Path) -> None:
    process = _FakeProcess([_init(), *_turn_lines("hello from agy")])
    backend, session = _session(process, project_path=str(tmp_path))
    which, create = _spawn_patches(process)
    with which, create as create_process:
        await backend.attach_session(session, model="gemini-3-flash")
        events = [event async for event in session.send_message("hi")]

    argv = list(create_process.call_args.args)
    assert "-p" not in argv
    assert "--print" not in argv
    assert "--mode" not in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path)
    assert argv[argv.index("--print-timeout") + 1] == "2562047h"
    assert "--conversation" not in argv
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["hello from agy"]
    payloads = [json.loads(raw.decode("utf-8")) for raw in process.stdin.writes]
    assert len(payloads) == 1
    assert payloads[0]["event"] == "user"
    assert payloads[0]["message"]["content"] == "hi"


@pytest.mark.asyncio
async def test_second_turn_writes_one_line_without_respawn() -> None:
    process = _FakeProcess([_init(), *_turn_lines("one"), *_turn_lines("two")])
    backend, session = _session(process)
    which, create = _spawn_patches(process)
    with which, create as create_process:
        await backend.attach_session(session, model="gemini-3-flash")
        first = [event async for event in session.send_message("one")]
        second = [event async for event in session.send_message("two")]

    assert create_process.call_count == 1
    assert len(process.stdin.writes) == 2
    assert [event.content for event in first if isinstance(event, TextChunk)] == ["one"]
    assert [event.content for event in second if isinstance(event, TextChunk)] == ["two"]
    assert process.terminated is False
    assert "--conversation" not in list(create_process.call_args.args)


@pytest.mark.asyncio
async def test_conversation_id_is_distinct_from_gobby_identity() -> None:
    process = _FakeProcess([_init(), *_turn_lines("ok")])
    backend, session = _session(process)
    which, create = _spawn_patches(process)
    with which, create:
        await backend.attach_session(session)
        events = [event async for event in session.send_message("hi")]

    assert session.conversation_id == GOBBY_CONV
    assert session.sdk_session_id == CONV
    assert session.sdk_session_id != session.conversation_id
    assert session.sdk_session_id != session.db_session_id
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.sdk_session_id == CONV


@pytest.mark.asyncio
async def test_concurrent_turn_on_locked_session_is_rejected() -> None:
    hanging = asyncio.Event()

    class _HangAfterInit(_HangingStdout):
        async def readline(self) -> bytes:
            if self._prefix:
                return await super().readline()
            hanging.set()
            return await super().readline()

    process = _FakeProcess(stdout=_HangAfterInit([_init(), _text("partial")]))
    backend, session = _session(process, prompt_timeout=5.0)
    which, create = _spawn_patches(process)
    with which, create:
        await backend.attach_session(session)
        task = asyncio.create_task(_collect(session.send_message("one")))
        await hanging.wait()
        rejected = [event async for event in session.send_message("two")]
        await session.interrupt()
        await asyncio.wait_for(task, timeout=1.0)

    assert any(
        isinstance(event, TextChunk) and "already in progress" in event.content
        for event in rejected
    )
    assert session._lock.locked() is False


@pytest.mark.asyncio
async def test_interrupt_terminates_process_tree_and_preserves_id() -> None:
    process = _FakeProcess([_init(), *_turn_lines("ok")])
    backend, session = _session(process)
    killpg = MagicMock()
    which, create = _spawn_patches(process)
    with which, create, patch(f"{_AGY_MOD}.os.killpg", killpg):
        await backend.attach_session(session)
        [event async for event in session.send_message("hi")]
        confirmed = session.sdk_session_id
        await session.interrupt()

    assert killpg.call_count >= 1
    assert killpg.call_args_list[0].args[0] == process.pid
    assert process.signals == []
    assert process.stdin.closed is True
    assert session.sdk_session_id == confirmed == CONV
    assert GOBBY_CONV not in backend._handles
    assert session._lock.locked() is False


@pytest.mark.asyncio
async def test_model_and_effort_reach_argv(tmp_path: Path) -> None:
    process = _FakeProcess([_init()])
    backend, session = _session(process, project_path=str(tmp_path))
    session.reasoning_effort = "high"
    which, create = _spawn_patches(process)
    with which, create as create_process:
        await backend.attach_session(session, model="gemini-3-pro")

    argv = list(create_process.call_args.args)
    assert argv[argv.index("--model") + 1] == "gemini-3-pro"
    assert argv[argv.index("--effort") + 1] == "high"


@pytest.mark.asyncio
async def test_stderr_is_redacted() -> None:
    redact = getattr(_agy(), "_redact_agy_stderr", None)
    assert callable(redact)
    text = redact("Authorization: Bearer super-secret token=abcd")
    assert "super-secret" not in text
    assert "abcd" not in text
    assert "<redacted>" in text


@pytest.mark.asyncio
async def test_nonzero_cli_timeout_exit_is_one_terminal_error() -> None:
    process = _FakeProcess([_init(), _result(status="ERROR", error="timeout waiting for response")])
    backend, session = _session(process)
    which, create = _spawn_patches(process)
    with which, create:
        await backend.attach_session(session)
        events = [event async for event in session.send_message("hi")]

    errors = [
        event
        for event in events
        if isinstance(event, TextChunk) and event.content.startswith("Error:")
    ]
    assert len(errors) == 1
    assert "timeout waiting for response" in errors[0].content
    assert session.sdk_session_id == CONV


@pytest.mark.asyncio
async def test_tool_output_above_64kib_is_read_without_overrun() -> None:
    huge = "x" * (64 * 1024 + 32)
    process = _FakeProcess(
        [
            _init(),
            _tool_active(output=huge),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": CONV,
                        "step_index": 3,
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_name": "write_to_file",
                        "tool_info": {"name": "write_to_file", "output": huge},
                    },
                }
            ),
            _result(usage=_usage()),
        ]
    )
    backend, session = _session(process)
    which, create = _spawn_patches(process)
    with which, create as create_process:
        await backend.attach_session(session)
        events = [event async for event in session.send_message("write")]

    assert create_process.call_args.kwargs["limit"] == ACP_STREAM_READER_LIMIT_BYTES
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert results
    assert huge in str(results[0].result)


@pytest.mark.asyncio
async def test_reconstructed_session_argv_carries_conversation() -> None:
    first = _FakeProcess([_init(), *_turn_lines("one")])
    second = _FakeProcess([_init(), *_turn_lines("two")])
    backend, session = _session(first)
    which = patch(f"{_AGY_MOD}.shutil.which", return_value="/bin/agy")
    create = patch(
        f"{_AGY_MOD}.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        side_effect=[first, second],
    )
    with which, create as create_process:
        await backend.attach_session(session)
        [event async for event in session.send_message("one")]
        await backend.detach_session(session)
        rebuilt = _agy().AgyManagedChatSession(
            conversation_id="gobby-web-conv-2",
            _backend=backend,
        )
        rebuilt.project_path = session.project_path
        rebuilt.sdk_session_id = session.sdk_session_id
        await backend.attach_session(rebuilt)
        argv = list(create_process.call_args_list[1].args)

    assert session.sdk_session_id == CONV
    assert argv[argv.index("--conversation") + 1] == CONV
    first_argv = list(create_process.call_args_list[0].args)
    assert "--conversation" not in first_argv


@pytest.mark.asyncio
async def test_failed_result_preserves_confirmed_conversation_id() -> None:
    process = _FakeProcess([_init(), *_turn_lines("ok"), _result(status="ERROR", error="boom")])
    backend, session = _session(process)
    which, create = _spawn_patches(process)
    with which, create:
        await backend.attach_session(session)
        [event async for event in session.send_message("one")]
        confirmed = session.sdk_session_id
        [event async for event in session.send_message("two")]

    assert confirmed == CONV
    assert session.sdk_session_id == CONV


@pytest.mark.asyncio
async def test_prompt_timeout_uses_shared_default_and_agy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_AGY_ACP_PROMPT_TIMEOUT_SECONDS", raising=False)
    backend = _agy().AgyWebChatBackend()
    assert backend._prompt_timeout == DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS
    monkeypatch.setenv("GOBBY_AGY_ACP_PROMPT_TIMEOUT_SECONDS", "0.05")
    overridden = _agy().AgyWebChatBackend()
    assert overridden._prompt_timeout == 0.05


@pytest.mark.asyncio
async def test_progress_timeout_emits_one_error_and_stays_reconstructable() -> None:
    first = _FakeProcess(stdout=_HangingStdout([_init()]))
    second = _FakeProcess([_init(), *_turn_lines("recovered")])
    backend, session = _session(first, prompt_timeout=0.05)
    which = patch(f"{_AGY_MOD}.shutil.which", return_value="/bin/agy")
    create = patch(
        f"{_AGY_MOD}.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        side_effect=[first, second],
    )
    with which, create:
        await backend.attach_session(session)
        timed_out = [event async for event in session.send_message("hello")]
        assert GOBBY_CONV not in backend._handles
        recovered = [event async for event in session.send_message("again")]

    errors = [
        event
        for event in timed_out
        if isinstance(event, TextChunk) and "Timed out waiting for AGY" in event.content
    ]
    assert len(errors) == 1
    assert first.terminated is True
    assert session._lock.locked() is False
    assert [event.content for event in recovered if isinstance(event, TextChunk)] == ["recovered"]


@pytest.mark.asyncio
async def test_progress_clock_renews_on_translated_events() -> None:
    process = _FakeProcess(
        stdout=_TimedStdout(
            [
                (0.0, _init()),
                (0.04, _text("still going")),
                (0.04, _result(usage=_usage())),
            ]
        )
    )
    backend, session = _session(process, prompt_timeout=0.05)
    which, create = _spawn_patches(process)
    with which, create:
        await backend.attach_session(session)
        events = [event async for event in session.send_message("hello")]

    assert [event.content for event in events if isinstance(event, TextChunk)] == ["still going"]
    assert process.terminated is False


@pytest.mark.asyncio
async def test_print_timeout_flag_is_effectively_unbounded() -> None:
    process = _FakeProcess([_init()])
    backend, session = _session(process)
    which, create = _spawn_patches(process)
    with which, create as create_process:
        await backend.attach_session(session)
    argv = list(create_process.call_args.args)
    assert argv[argv.index("--print-timeout") + 1] == "2562047h"


@pytest.mark.asyncio
async def test_identity_env_is_exported_and_hooks_resolve_canonical_row() -> None:
    process = _FakeProcess([_init(), *_turn_lines("ok")])
    backend, session = _session(process)
    which, create = _spawn_patches(process)
    with which, create as create_process:
        await backend.attach_session(session)
        [event async for event in session.send_message("hi")]

    env = create_process.call_args.kwargs["env"]
    assert env["GOBBY_SESSION_ID"] == "sess-agy-web"
    assert env["GOBBY_PROJECT_ID"] == "proj-agy"
    assert "GOBBY_HOOKS_DISABLED" not in env
    assert env["GOBBY_WEB_CHAT_CHILD"] == "1"

    web_row = SimpleNamespace(
        id="sess-agy-web",
        project_id="proj-agy",
        source="agy",
        machine_id="machine-1",
        session_type="web_chat",
        workspace_path=str(Path.cwd()),
        tombstoned=False,
        external_id=CONV,
    )
    terminal_row = SimpleNamespace(
        id="sess-agy-term",
        project_id="proj-agy",
        source="agy",
        machine_id="machine-1",
        session_type="interactive",
        workspace_path=str(Path.cwd()),
        tombstoned=False,
        external_id=CONV,
    )
    sessions = {"sess-agy-web": web_row, "sess-agy-term": terminal_row}
    manager = MagicMock()
    manager.get.side_effect = lambda sid: sessions.get(sid)
    manager.db = object()
    manager.find_by_external_id.side_effect = lambda **kwargs: (
        web_row if kwargs.get("session_type") == "web_chat" else terminal_row
    )

    from gobby.hooks.startup_claim_preflight import preflight_agy_startup_claim

    web_payload = {
        "source": "agy",
        "hook_type": "PreInvocation",
        "_platform_session_id": env["GOBBY_SESSION_ID"],
        "project_id": env["GOBBY_PROJECT_ID"],
        "input_data": {"cwd": str(Path.cwd()), "conversationId": CONV},
    }
    term_payload = {
        "source": "agy",
        "hook_type": "PreInvocation",
        "_platform_session_id": "sess-agy-term",
        "project_id": "proj-agy",
        "input_data": {"cwd": str(Path.cwd()), "conversationId": CONV},
    }

    def _claim(_self: object, session_id: str, owner_token: str | None = None) -> SimpleNamespace:
        del session_id
        return SimpleNamespace(mode="full", state="claimed", owner_token=owner_token, generation=1)

    with patch(
        "gobby.hooks.startup_claim_preflight.SessionVariableManager.claim_startup_context",
        _claim,
    ):
        web_lease = preflight_agy_startup_claim(
            web_payload, SimpleNamespace(session_manager=manager)
        )
        term_lease = preflight_agy_startup_claim(
            term_payload, SimpleNamespace(session_manager=manager)
        )

    assert web_lease is not None and web_lease.session_id == "sess-agy-web"
    assert term_lease is not None and term_lease.session_id == "sess-agy-term"
    manager.get.assert_any_call("sess-agy-web")
    assert manager.get.call_args_list[0].args[0] != CONV


@pytest.mark.asyncio
async def test_checkpoint_does_not_invoke_compaction_or_lifecycle() -> None:
    process = _FakeProcess(
        [_init(), _bookkeeping("checkpoint"), _text("after"), _result(usage=_usage())]
    )
    backend, session = _session(process)
    session._on_pre_compact = AsyncMock(return_value=None)
    session._on_pre_tool = AsyncMock(return_value=None)
    which, create = _spawn_patches(process)
    with which, create:
        await backend.attach_session(session)
        events = [event async for event in session.send_message("hi")]

    session._on_pre_compact.assert_not_awaited()
    session._on_pre_tool.assert_not_awaited()
    assert [event.content for event in events if isinstance(event, TextChunk)] == ["after"]
    source = inspect.getsource(_agy())
    assert "PRE_COMPACT" not in source
    assert "build_compaction_context" not in source


@pytest.mark.asyncio
async def test_outbound_validation_rejects_fatal_agy_lines() -> None:
    validate = getattr(_agy(), "_validate_agy_outbound", None)
    assert callable(validate)
    with pytest.raises(ValueError, match="event"):
        validate({"message": {"content": "hi"}})
    with pytest.raises(ValueError, match="text"):
        validate({"event": "user", "message": {"content": [{"type": "image", "url": "x"}]}})
    ok = validate({"event": "user", "message": {"content": "hi"}})
    assert ok["event"] == "user"


@pytest.mark.asyncio
async def test_usage_reaches_persist_done_metadata() -> None:
    process = _FakeProcess([_init(), *_turn_lines("ok")])
    backend, session = _session(process)
    which, create = _spawn_patches(process)
    persistence = _RecordingPersistence()
    handler = ChatStreamEventHandler(
        SimpleNamespace(_chat_sessions={}),
        GOBBY_CONV,
        cast(ChatStreamTransport, _FakeTransport()),
        cast(ChatStreamPersistence, persistence),
        AssistantContentBlocks(),
        ChatStreamEventState(assistant_message_id="a1"),
        None,
    )
    with which, create:
        await backend.attach_session(session)
        async for event in session.send_message("hi"):
            await handler.handle_event(event, session)

    assert len(persistence.done_events) == 1
    done = persistence.done_events[0]
    assert isinstance(done, DoneEvent)
    assert done.input_tokens == 10
    assert done.output_tokens == 4
    assert done.cache_read_input_tokens == 7
    assert done.context_window == session._resolve_context_window()
    assert persistence.sdk_ids == [CONV]


@pytest.mark.asyncio
async def test_eof_and_switch_model_reattach_with_conversation() -> None:
    first = _FakeProcess([_init(), _text("partial")])
    second = _FakeProcess([_init(), *_turn_lines("resumed")])
    third = _FakeProcess([_init(), *_turn_lines("switched")])
    backend, session = _session(first)
    which = patch(f"{_AGY_MOD}.shutil.which", return_value="/bin/agy")
    create = patch(
        f"{_AGY_MOD}.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        side_effect=[first, second, third],
    )
    with which, create as create_process:
        await backend.attach_session(session, model="gemini-3-flash")
        eof_events = [event async for event in session.send_message("one")]
        resumed = [event async for event in session.send_message("two")]
        await backend.switch_model(session, "gemini-3-pro")

    errors = [
        event for event in eof_events if isinstance(event, TextChunk) and "Error:" in event.content
    ]
    assert len(errors) == 1
    assert session.sdk_session_id == CONV
    assert [event.content for event in resumed if isinstance(event, TextChunk)] == ["resumed"]
    second_argv = list(create_process.call_args_list[1].args)
    third_argv = list(create_process.call_args_list[2].args)
    assert second_argv[second_argv.index("--conversation") + 1] == CONV
    assert third_argv[third_argv.index("--conversation") + 1] == CONV
    assert third_argv[third_argv.index("--model") + 1] == "gemini-3-pro"


@pytest.mark.asyncio
async def test_child_env_and_native_pretooluse_deny_without_mode_flag() -> None:
    process = _FakeProcess([_init(), _tool_active(), _result(usage=_usage())])
    backend, session = _session(process)
    session.chat_mode = "plan"
    session._on_pre_tool = AsyncMock(return_value={"decision": "deny"})
    session._on_before_agent = AsyncMock(return_value=None)
    session._on_post_tool = AsyncMock(return_value=None)
    session._on_stop = AsyncMock(return_value=None)
    session._on_pre_compact = AsyncMock(return_value=None)
    which, create = _spawn_patches(process)
    with which, create as create_process:
        await backend.attach_session(session)
        events = [event async for event in session.send_message("write")]

    env = create_process.call_args.kwargs["env"]
    argv = list(create_process.call_args.args)
    assert "GOBBY_HOOKS_DISABLED" not in env
    assert env["GOBBY_WEB_CHAT_CHILD"] == "1"
    assert "--mode" not in argv
    session._on_pre_tool.assert_not_awaited()
    session._on_before_agent.assert_not_awaited()
    session._on_post_tool.assert_not_awaited()
    session._on_stop.assert_not_awaited()
    session._on_pre_compact.assert_not_awaited()
    assert any(isinstance(event, ToolCallEvent) for event in events)
    denied = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="deny", reason="plan mode write blocked"),
        hook_type="PreToolUse",
    )
    assert denied.get("decision") == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trickle",
    [
        "not-json",
        _init(),
        json.dumps({"event": "unknown_kind", "unknown_kind": {}}),
        _bookkeeping("user_input"),
        _bookkeeping("checkpoint"),
        _bookkeeping("system_message"),
        _bookkeeping("error_message"),
        _bookkeeping("unknown"),
    ],
)
async def test_ignored_lines_expire_on_progress_clock(trickle: str) -> None:
    process = _FakeProcess(
        stdout=_PrefixThenTrickleStdout([_init()], trickle=trickle, interval_s=0.01)
    )
    backend, session = _session(process, prompt_timeout=0.05)
    which, create = _spawn_patches(process)
    with which, create:
        await backend.attach_session(session)
        events = await asyncio.wait_for(_collect(session.send_message("hello")), timeout=1.0)

    errors = [
        event
        for event in events
        if isinstance(event, TextChunk) and "Timed out waiting for AGY" in event.content
    ]
    assert len(errors) == 1
    assert process.terminated is True
    assert GOBBY_CONV not in backend._handles
    assert session._lock.locked() is False


@pytest.mark.asyncio
async def test_attach_session_rejects_missing_cwd(tmp_path: Path) -> None:
    backend = _agy().AgyWebChatBackend()
    session = _agy().AgyManagedChatSession(conversation_id=GOBBY_CONV, _backend=backend)
    session.project_path = str(tmp_path / "missing")
    with (
        patch(f"{_AGY_MOD}.shutil.which", return_value="/bin/agy"),
        pytest.raises(ValueError, match="working directory does not exist"),
    ):
        await backend.attach_session(session)


@pytest.mark.asyncio
async def test_attach_session_uses_start_new_session_for_process_group() -> None:
    process = _FakeProcess([_init()])
    backend, session = _session(process)
    which, create = _spawn_patches(process)
    with which, create as create_process:
        await backend.attach_session(session)
    assert create_process.call_args.kwargs["start_new_session"] is True
    assert create_process.call_args.kwargs["limit"] == ACP_STREAM_READER_LIMIT_BYTES


def test_agy_tool_name_adapter_is_wired() -> None:
    session = _agy().AgyManagedChatSession(
        conversation_id=GOBBY_CONV,
        _backend=_agy().AgyWebChatBackend(),
    )
    adapter = session._tool_name_adapter()
    assert adapter("write_to_file") == "Write"
