"""AGY daemon-owned web-chat backend."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.adapters.acp_client import (
    ACP_STREAM_READER_LIMIT_BYTES,
    DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS,
    _resolve_timeout,
)
from gobby.adapters.acp_stream import StreamEvent
from gobby.agents.constants import GOBBY_PROJECT_ID, GOBBY_SESSION_ID
from gobby.agents.srt_runtime import prepare_sandbox_launch
from gobby.hooks.normalization import normalize_tool_fields
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.chat_session_helpers import PendingApproval
from gobby.servers.websocket.chat.backends.agy_stream import (
    agy_tool_name_adapter,
    iter_agy_turn_events,
    parse_agy_stream_line,
)
from gobby.servers.websocket.chat.backends.base import (
    ManagedChatSessionBase,
    ProviderBackendHealth,
    _extract_text,
    _log_upstream_error_event,
    launch_sandbox_config,
)
from gobby.servers.websocket.chat.permissions import ManagedWebChatPermissionsMixin

logger = logging.getLogger(__name__)

AGY_ACP_PROMPT_TIMEOUT_ENV = "GOBBY_AGY_ACP_PROMPT_TIMEOUT_SECONDS"
AGY_PRINT_TIMEOUT = "2562047h"
AGY_STDERR_MAX_CHARS = 1000
_AGY_STDERR_REDACTIONS = (
    re.compile(r"(?i)\b(bearer\s+)[^\s]+"),
    re.compile(
        r"(?i)\b([a-z0-9_.-]*(?:api[_-]?key|token|secret|password)[a-z0-9_.-]*)\b"
        r"(\s*[=:]\s*)[^\s]+"
    ),
)


class _ProgressDeadline:
    """Per-turn progress clock that renews only on accepted stream input."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._deadline = asyncio.get_running_loop().time() + timeout

    def remaining(self) -> float:
        return self._deadline - asyncio.get_running_loop().time()

    def renew(self) -> None:
        self._deadline = asyncio.get_running_loop().time() + self._timeout


@dataclass
class _TurnProgressState:
    seen_init: bool = False
    emitted: bool = False


async def _readline_with_progress(
    stdout: asyncio.StreamReader, deadline: _ProgressDeadline
) -> bytes:
    remaining = deadline.remaining()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(stdout.readline(), timeout=remaining)


def _validated_agy_option(value: str, option: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned.startswith("-"):
        raise ValueError(f"Invalid AGY {option}: {value!r}")
    return cleaned


def _resolve_agy_cwd(project_path: str | None) -> str:
    cwd = Path(project_path or ".").expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"AGY working directory does not exist: {cwd}")
    return str(cwd)


def _redact_agy_stderr(text: str) -> str:
    redacted = _AGY_STDERR_REDACTIONS[0].sub(r"\1<redacted>", text)
    redacted = _AGY_STDERR_REDACTIONS[1].sub(r"\1\2<redacted>", redacted)
    if len(redacted) <= AGY_STDERR_MAX_CHARS:
        return redacted
    return f"{redacted[:AGY_STDERR_MAX_CHARS]}... [truncated]"


def _validate_agy_outbound(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("event") != "user":
        raise ValueError("AGY outbound line requires event=user")
    message = record.get("message")
    if not isinstance(message, dict):
        raise ValueError("AGY outbound line requires a message object")
    content = message.get("content")
    if isinstance(content, str):
        return record
    if isinstance(content, list):
        for block in content:
            if (
                not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
            ):
                raise ValueError("AGY outbound line requires text content blocks")
        return record
    raise ValueError("AGY outbound line requires text content")


def _usage_int(usage: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
    return None


def _agy_line_is_progress(events: list[StreamEvent], state: _TurnProgressState) -> bool:
    progressed = False
    for event in events:
        if event.event_type == "init":
            if state.seen_init or state.emitted:
                continue
            state.seen_init = True
            progressed = True
            continue
        state.emitted = True
        progressed = True
    return progressed


def _error_text(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, str) and error:
        return error
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    message = payload.get("message")
    if isinstance(message, str) and message:
        return message
    return "Unknown error"


@dataclass(slots=True)
class _AgyProcessHandle:
    process: asyncio.subprocess.Process
    stderr_task: asyncio.Task[None] | None = None


@dataclass
class AgyManagedChatSession(ManagedWebChatPermissionsMixin, ManagedChatSessionBase):
    """Web-chat session backed by a per-session AGY stream-json process."""

    provider: str = field(default="agy", init=False)
    chat_mode: str = field(default="plan")
    _pending_question: dict[str, Any] | None = field(default=None, repr=False)
    _pending_answer_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_answers: dict[str, Any] | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _pending_approval_events: dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _pending_approval_decisions: dict[str, str] = field(default_factory=dict, repr=False)
    _plan_approved: bool = field(default=False, repr=False)
    _plan_feedback: str | None = field(default=None, repr=False)
    _turn_tool_calls: int = field(default=0, repr=False)

    def _web_chat_source(self) -> str:
        return "agy"

    def _provider_label(self) -> str:
        return "agy"

    def _tool_name_adapter(self) -> Any:
        return agy_tool_name_adapter

    def _confirm_conversation_id(self, value: object) -> None:
        if isinstance(value, str) and value:
            self.sdk_session_id = value

    async def _apply_pre_tool_lifecycle(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Native ghook is the sole BEFORE_TOOL authority for AGY (plan row 5.3.5)."""
        del tool_name, tool_input
        return None

    async def _apply_post_tool_lifecycle(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_response: Any,
        *,
        is_error: bool | None = None,
    ) -> dict[str, Any] | None:
        """Native ghook is the sole AFTER_TOOL authority for AGY (plan row 5.3.5)."""
        del tool_name, tool_input, tool_response, is_error
        return None

    async def send_message(
        self,
        content: str | list[dict[str, Any]],
        *,
        request_parameters: Mapping[str, object] | None = None,
    ) -> AsyncIterator[ChatEvent]:
        del request_parameters
        if self._lock.locked():
            yield TextChunk(content="Error: AGY session turn already in progress")
            yield DoneEvent(
                tool_calls_count=0,
                sdk_session_id=self.sdk_session_id,
                context_window=self._resolve_context_window(),
            )
            return
        if not self._connected:
            await self.start(model=self._model)

        async with self._lock:
            self.last_activity = datetime.now(UTC)
            self._turn_tool_calls = 0
            prompt = _extract_text(content)
            final_done: DoneEvent | None = None
            try:
                async for stream_event in self._backend.send_message(self, prompt):
                    chat_event = self._translate_event(stream_event)
                    if stream_event.event_type == "init":
                        self._confirm_conversation_id(
                            stream_event.data.get("conversation_id")
                            or stream_event.data.get("session_id")
                        )
                        model = stream_event.data.get("model")
                        if isinstance(model, str) and model:
                            self._model = model
                        continue
                    if isinstance(chat_event, DoneEvent):
                        final_done = chat_event
                        continue
                    if chat_event is not None:
                        yield chat_event
            except OSError as exc:
                logger.error("AGY managed session %s error: %s", self.conversation_id, exc)
                yield TextChunk(content=f"Generation failed: {exc}")
            yield final_done or DoneEvent(
                tool_calls_count=self._turn_tool_calls,
                sdk_session_id=self.sdk_session_id,
                context_window=self._resolve_context_window(),
            )

    def _translate_event(self, event: StreamEvent) -> ChatEvent | None:
        if event.event_type == "content_delta":
            kind = event.data.get("kind") or "text"
            if kind == "text":
                content = event.data.get("content") or event.data.get("text") or ""
                return TextChunk(content=content) if content else None
            if kind == "tool_use":
                # parse_agy_stream_line already applied agy_tool_name_adapter.
                tool_name = str(event.data.get("tool_name") or event.data.get("name") or "unknown")
                tool_input = event.data.get("tool_input") or event.data.get("input") or {}
                normalized = normalize_tool_fields(
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_input if isinstance(tool_input, dict) else {},
                    }
                )
                call_id = event.data.get("call_id") or event.data.get("id") or "unknown"
                arguments = normalized.get("tool_input")
                if not isinstance(arguments, dict):
                    arguments = {}
                return ToolCallEvent(
                    tool_call_id=str(call_id),
                    tool_name=str(normalized.get("tool_name") or tool_name),
                    server_name=str(
                        normalized.get("mcp_server") or event.data.get("server_name") or "agy"
                    ),
                    arguments=arguments,
                )
            if kind == "tool_result":
                self._turn_tool_calls += 1
                call_id = event.data.get("call_id") or event.data.get("id") or "unknown"
                success = bool(event.data.get("success", True))
                return ToolResultEvent(
                    tool_call_id=str(call_id),
                    success=success,
                    result=event.data.get("result") or event.data.get("output"),
                    error=event.data.get("error"),
                )
            return None

        if event.event_type == "result":
            self._confirm_conversation_id(event.data.get("conversation_id"))
            if event.data.get("status") == "ERROR":
                return TextChunk(content=f"Error: {_error_text(event.data)}")
            usage = event.data.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            input_tokens = _usage_int(usage, "input_tokens")
            output_tokens = _usage_int(usage, "output_tokens")
            cache_read = _usage_int(usage, "cache_read_tokens", "cache_read_input_tokens")
            total = input_tokens
            if input_tokens is not None or cache_read is not None:
                total = (input_tokens or 0) + (cache_read or 0)
            return DoneEvent(
                tool_calls_count=self._turn_tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
                total_input_tokens=total,
                context_window=self._resolve_context_window(),
                sdk_session_id=self.sdk_session_id,
            )

        if event.event_type == "error":
            _log_upstream_error_event("agy", self, event.data)
            return TextChunk(content=f"Error: {_error_text(event.data)}")

        return None

    async def interrupt(self) -> None:
        self.cancel_pending_approval()
        await self._backend.interrupt(self)


class AgyWebChatBackend:
    """Per-session AGY stream-json backend."""

    provider = "agy"

    def __init__(
        self,
        *,
        default_model: str | None = None,
        prompt_timeout: float | None = None,
    ) -> None:
        self._default_model = default_model
        self._prompt_timeout = _resolve_timeout(
            prompt_timeout,
            env_name=AGY_ACP_PROMPT_TIMEOUT_ENV,
            default=DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS,
        )
        self._health = ProviderBackendHealth(provider=self.provider, available=False)
        self._handles: dict[str, _AgyProcessHandle] = {}

    async def start(self, *, background: bool = False) -> None:
        del background
        path = shutil.which("agy")
        if not path:
            self._health = ProviderBackendHealth(
                provider=self.provider,
                available=False,
                startup_error="agy CLI not found in PATH",
            )
            return
        self._health = ProviderBackendHealth(provider=self.provider, available=True)

    async def stop(self) -> None:
        for conversation_id in list(self._handles):
            handle = self._handles.get(conversation_id)
            if handle is not None:
                await self._terminate_handle(handle)
        self._handles.clear()
        self._health = ProviderBackendHealth(provider=self.provider, available=False)

    def health(self) -> ProviderBackendHealth:
        return self._health

    def _build_argv(self, session: AgyManagedChatSession, cwd: str, agy_path: str) -> list[str]:
        cmd = [
            agy_path,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
            "--add-dir",
            cwd,
            "--print-timeout",
            AGY_PRINT_TIMEOUT,
        ]
        sandbox_config = launch_sandbox_config(session)
        if sandbox_config.enabled and sandbox_config.backend == "srt":
            cmd.append("--sandbox=false")
        if session._model:
            session._model = _validated_agy_option(session._model, "model")
            cmd.extend(["--model", session._model])
        if session.reasoning_effort and session.reasoning_effort != "auto":
            effort = _validated_agy_option(session.reasoning_effort, "effort")
            session.reasoning_effort = effort
            cmd.extend(["--effort", effort])
        if session.sdk_session_id:
            cmd.extend(["--conversation", session.sdk_session_id])
        return cmd

    def _identity_env(self, session: AgyManagedChatSession) -> dict[str, str]:
        env = os.environ.copy()
        # The daemon's own environment may disable hooks; the AGY child must
        # still run native ghook (plan row 5.2.19).
        env.pop("GOBBY_HOOKS_DISABLED", None)
        env["GOBBY_WEB_CHAT_CHILD"] = "1"
        if session.db_session_id:
            env[GOBBY_SESSION_ID] = session.db_session_id
        if session.project_id:
            env[GOBBY_PROJECT_ID] = session.project_id
        return env

    async def attach_session(
        self,
        session: AgyManagedChatSession,
        *,
        model: str | None = None,
    ) -> None:
        if model:
            session._model = model
        elif not session._model:
            session._model = self._default_model

        await self.start()
        if not self._health.available:
            raise RuntimeError(self._health.startup_error or "AGY backend unavailable")

        await self.detach_session(session)

        agy_path = shutil.which("agy")
        if not agy_path:
            raise RuntimeError("agy CLI not found in PATH")

        cwd = await asyncio.to_thread(_resolve_agy_cwd, session.project_path)
        cmd = self._build_argv(session, cwd, agy_path)
        env = self._identity_env(session)
        sandbox_config = launch_sandbox_config(session)
        if sandbox_config.enabled:
            daemon_cfg = getattr(session, "_config", None)
            websocket = getattr(daemon_cfg, "websocket", None)
            launch = await prepare_sandbox_launch(
                config=sandbox_config,
                provider="agy",
                workspace_path=cwd,
                run_id=session.db_session_id or session.conversation_id,
                resolver=None,
                daemon_port=int(getattr(daemon_cfg, "daemon_port", 60887)),
                websocket_port=int(getattr(websocket, "port", 60888)),
                api_base=None,
                env=env,
            )
            cmd, env = launch.compose_subprocess(cmd, env)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=ACP_STREAM_READER_LIMIT_BYTES,
            start_new_session=True,
        )
        stderr_task = None
        process_stderr = getattr(process, "stderr", None)
        if process_stderr is not None:
            stderr_task = asyncio.create_task(
                self._log_process_stderr(process_stderr, session.conversation_id)
            )
        self._handles[session.conversation_id] = _AgyProcessHandle(process, stderr_task=stderr_task)
        session._connected = True
        session.last_activity = datetime.now(UTC)

    async def detach_session(self, session: AgyManagedChatSession) -> None:
        handle = self._handles.pop(session.conversation_id, None)
        if handle is not None:
            await self._terminate_handle(handle)
        session._connected = False

    async def send_message(
        self,
        session: AgyManagedChatSession,
        prompt: str,
    ) -> AsyncIterator[StreamEvent]:
        handle = self._handles.get(session.conversation_id)
        if handle is None or handle.process.returncode is not None:
            await self.attach_session(session, model=session._model)
            handle = self._handles[session.conversation_id]
        if handle.process.stdin is None or handle.process.stdout is None:
            raise RuntimeError("AGY process streams unavailable")

        payload = _validate_agy_outbound({"event": "user", "message": {"content": prompt}})
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            handle.process.stdin.write(encoded)
            await handle.process.stdin.drain()
        except (BrokenPipeError, ConnectionError) as exc:
            logger.warning(
                "AGY stream for %s was closed before send; reattaching once",
                session.conversation_id,
            )
            del exc
            await self.detach_session(session)
            await self.attach_session(session, model=session._model)
            handle = self._handles[session.conversation_id]
            if handle.process.stdin is None:
                raise RuntimeError("AGY stdin stream unavailable") from None
            handle.process.stdin.write(encoded)
            await handle.process.stdin.drain()

        deadline = _ProgressDeadline(self._prompt_timeout)
        state = _TurnProgressState()
        try:
            async for event in iter_agy_turn_events(
                self._iter_progress_events(handle, session, deadline, state)
            ):
                if event.event_type == "error" and event.data.get("code") in {
                    "eof",
                    "timeout",
                }:
                    await self.detach_session(session)
                yield event
                if event.event_type == "result":
                    if event.data.get("status") == "ERROR":
                        await self.detach_session(session)
                    return
        except TimeoutError:
            yield StreamEvent(
                event_type="error",
                data={
                    "code": "timeout",
                    "message": (
                        "Timed out waiting for AGY stream progress after "
                        f"{self._prompt_timeout:.1f}s"
                    ),
                },
            )
            return
        except asyncio.CancelledError:
            await self._abandon_handle(handle, session.conversation_id)
            raise

    async def interrupt(self, session: AgyManagedChatSession) -> None:
        handle = self._handles.get(session.conversation_id)
        if handle is None:
            return
        await self._abandon_handle(handle, session.conversation_id)
        session._connected = False

    async def switch_model(self, session: AgyManagedChatSession, new_model: str) -> None:
        session._model = new_model
        await self.detach_session(session)
        await self.attach_session(session, model=new_model)

    async def _iter_progress_events(
        self,
        handle: _AgyProcessHandle,
        session: AgyManagedChatSession,
        deadline: _ProgressDeadline,
        state: _TurnProgressState,
    ) -> AsyncIterator[list[StreamEvent]]:
        """Parse each stdout line exactly once, renewing the clock on accepted input."""
        stdout = handle.process.stdout
        if stdout is None:
            return
        while True:
            try:
                line = await _readline_with_progress(stdout, deadline)
            except TimeoutError:
                await self._abandon_handle(handle, session.conversation_id)
                raise
            if not line:
                return
            events = parse_agy_stream_line(line)
            if _agy_line_is_progress(events, state):
                deadline.renew()
            yield events

    async def _abandon_handle(self, handle: _AgyProcessHandle, conversation_id: str) -> None:
        current = self._handles.get(conversation_id)
        if current is handle:
            self._handles.pop(conversation_id, None)
        await self._terminate_handle(handle)

    async def _terminate_handle(self, handle: _AgyProcessHandle) -> None:
        if handle.process.stdin is not None:
            handle.process.stdin.close()
        if handle.process.returncode is None:
            pid = getattr(handle.process, "pid", None)
            signaled = False
            if isinstance(pid, int):
                try:
                    os.killpg(pid, signal.SIGTERM)
                    signaled = True
                except (AttributeError, ProcessLookupError, PermissionError, OSError):
                    signaled = False
            if not signaled:
                handle.process.terminate()
            try:
                await asyncio.wait_for(handle.process.wait(), timeout=2.0)
            except TimeoutError:
                if isinstance(pid, int):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except (AttributeError, ProcessLookupError, PermissionError, OSError):
                        handle.process.kill()
                else:
                    handle.process.kill()
                await handle.process.wait()
        if handle.stderr_task is not None and not handle.stderr_task.done():
            handle.stderr_task.cancel()
            try:
                await handle.stderr_task
            except asyncio.CancelledError:
                pass

    async def _log_process_stderr(self, stderr: asyncio.StreamReader, conversation_id: str) -> None:
        try:
            while line := await stderr.readline():
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug(
                        "AGY process %s stderr: %s",
                        conversation_id,
                        _redact_agy_stderr(text),
                    )
        except (RuntimeError, OSError) as exc:
            logger.debug("Failed to read AGY process stderr for %s: %s", conversation_id, exc)


__all__ = [
    "AgyManagedChatSession",
    "AgyWebChatBackend",
    "_redact_agy_stderr",
    "_validate_agy_outbound",
]
