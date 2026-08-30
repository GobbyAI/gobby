"""Droid daemon-owned web-chat backend."""

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
    StreamEvent,
    _resolve_timeout,
)
from gobby.agents.reasoning import resolve_spawn_reasoning
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.srt_runtime import prepare_sandbox_launch
from gobby.hooks.normalization import normalize_tool_fields
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.chat_session_helpers import PendingApproval, build_compaction_context
from gobby.servers.websocket.chat.backends.base import (
    ManagedChatSessionBase,
    ProviderBackendHealth,
    _extract_text,
    _log_upstream_error_event,
    launch_sandbox_config,
)
from gobby.servers.websocket.chat.backends.droid_permissions import DroidPermissionResolver
from gobby.servers.websocket.chat.backends.droid_plan import (
    _closes_plan_capture,
    _extract_plan_from_tool_args,
    _is_plan_exit_tool,
)
from gobby.servers.websocket.chat.backends.droid_stream import parse_droid_stream_line
from gobby.servers.websocket.chat.permissions import ManagedWebChatPermissionsMixin

logger = logging.getLogger(__name__)

DROID_ACP_PROMPT_TIMEOUT_ENV = "GOBBY_DROID_ACP_PROMPT_TIMEOUT_SECONDS"


class _ProgressDeadline:
    """Per-turn progress clock that renews only on accepted stream input."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._deadline = asyncio.get_running_loop().time() + timeout

    def remaining(self) -> float:
        return self._deadline - asyncio.get_running_loop().time()

    def renew(self) -> None:
        self._deadline = asyncio.get_running_loop().time() + self._timeout


async def _readline_with_progress(
    stdout: asyncio.StreamReader, deadline: _ProgressDeadline
) -> bytes:
    remaining = deadline.remaining()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(stdout.readline(), timeout=remaining)


def _validated_droid_option(value: str, option: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned.startswith("-"):
        raise ValueError(f"Invalid Droid {option}: {value!r}")
    return cleaned


def _resolve_droid_cwd(project_path: str | None) -> str:
    cwd = Path(project_path or ".").expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"Droid working directory does not exist: {cwd}")
    return str(cwd)


DROID_JSONRPC_VERSION = "2.0"
DROID_FACTORY_API_VERSION = "1.0.0"
DROID_FACTORY_PROTOCOL_VERSION = "1.25.0"
DROID_MACHINE_ID = "gobby-web-chat"
DROID_STDERR_MAX_CHARS = 1000
_DROID_STDERR_REDACTIONS = (
    re.compile(r"(?i)\b(bearer\s+)[^\s]+"),
    re.compile(
        r"(?i)\b([a-z0-9_.-]*(?:api[_-]?key|token|secret|password)[a-z0-9_.-]*)\b"
        r"(\s*[=:]\s*)[^\s]+"
    ),
)


@dataclass(slots=True)
class _DroidProcessHandle:
    process: asyncio.subprocess.Process
    stderr_task: asyncio.Task[None] | None = None
    request_counter: int = 0


def droid_tool_name_adapter(raw_tool_name: str) -> str:
    """Normalize Droid MCP tool names into Gobby's canonical form."""
    if raw_tool_name == "Execute":
        return "Bash"
    normalized = normalize_tool_fields({"tool_name": raw_tool_name})
    tool_name = normalized.get("tool_name")
    return tool_name if isinstance(tool_name, str) and tool_name else raw_tool_name


def _redact_droid_stderr(text: str) -> str:
    redacted = _DROID_STDERR_REDACTIONS[0].sub(r"\1<redacted>", text)
    redacted = _DROID_STDERR_REDACTIONS[1].sub(r"\1\2<redacted>", redacted)
    if len(redacted) <= DROID_STDERR_MAX_CHARS:
        return redacted
    return f"{redacted[:DROID_STDERR_MAX_CHARS]}... [truncated]"


@dataclass
class DroidManagedChatSession(ManagedWebChatPermissionsMixin, ManagedChatSessionBase):
    """Web-chat session backed by a per-session Droid stream-jsonrpc process."""

    provider: str = field(default="droid", init=False)
    chat_mode: str = field(default="plan")
    _pending_question: dict[str, Any] | None = field(default=None, repr=False)
    _pending_answer_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_answers: dict[str, Any] | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _pending_approval_events: dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _pending_approval_decisions: dict[str, str] = field(default_factory=dict, repr=False)
    _plan_approved: bool = field(default=False, repr=False)
    _plan_feedback: str | None = field(default=None, repr=False)
    # Turn-scoped guard set by the permission resolver when it broadcasts and
    # blocks an ExitSpecMode plan-exit tool while send_message holds self._lock.
    # Keep it coupled to resolver flow; otherwise post-loop broadcasting can
    # duplicate the structured spec after the plan decision resolves (#15682).
    _plan_exit_blocked_this_turn: bool = field(default=False, repr=False)
    _is_first_turn: bool = field(default=True, repr=False)

    def _web_chat_source(self) -> str:
        return "droid"

    def _provider_label(self) -> str:
        return "droid"

    def _tool_name_adapter(self) -> Any:
        return droid_tool_name_adapter

    async def send_message(
        self,
        content: str | list[dict[str, Any]],
        *,
        request_parameters: Mapping[str, object] | None = None,
    ) -> AsyncIterator[ChatEvent]:
        del request_parameters
        if not self._connected:
            await self.start(model=self._model)

        async with self._lock:
            self.last_activity = datetime.now(UTC)
            prompt = _extract_text(content)
            context_parts: list[str] = []

            if self._is_first_turn and self.system_prompt_override:
                context_parts.append(self.system_prompt_override)

            session_ref = (
                f"#{self.seq_num}" if self.seq_num else (self.db_session_id or self.conversation_id)
            )
            context_parts.append(
                build_compaction_context(
                    session_ref=session_ref,
                    project_id=self.project_id,
                    cwd=self.project_path,
                    source=self._web_chat_source(),
                )
            )

            plan_ctx = self._pop_plan_mode_context()
            if plan_ctx:
                context_parts.append(plan_ctx)
            deferred_context = self._consume_deferred_context()
            if deferred_context:
                context_parts.append(deferred_context)

            if self._on_before_agent:
                response = await self._on_before_agent(
                    {"prompt": prompt, "source": self._web_chat_source()}
                )
                if response and response.get("context"):
                    context_parts.append(str(response["context"]))

            full_prompt = prompt
            if context_parts:
                full_prompt = f"{'\n\n'.join(context_parts)}\n\n{prompt}"

            self._is_first_turn = False
            pending_tool_calls: dict[str, dict[str, Any]] = {}
            saw_content_delta = False
            plan_text_parts: list[str] = []
            # Prose-plan capture state (#15724). ``plan_capture_open`` closes once
            # a command/mutation tool runs after a plan body is present, so the
            # execution narration that follows is not captured. ``plan_pending_break``
            # inserts a paragraph break across a tool boundary so a heading that
            # follows the boundary still renders as markdown.
            plan_capture_open = True
            plan_pending_break = False
            # Authoritative plan body captured from a plan-exit tool argument
            # (Droid ExitSpecMode). Takes priority over accumulated prose, which
            # is often just a conversational preamble (#15693).
            structured_plan: str | None = None
            final_done: DoneEvent | None = None
            # Reset per turn: the permission resolver sets this when it
            # broadcasts + blocks an ExitSpecMode plan-exit tool, so the
            # post-loop broadcast below knows not to re-broadcast (#15682).
            self._plan_exit_blocked_this_turn = False

            try:
                async for stream_event in self._backend.send_message(self, full_prompt):
                    chat_event = self._translate_event(stream_event)

                    if stream_event.event_type == "init":
                        self.sdk_session_id = (
                            stream_event.data.get("session_id")
                            or stream_event.data.get("sessionId")
                            or self.sdk_session_id
                        )
                        model = stream_event.data.get("model")
                        if isinstance(model, str) and model:
                            self._model = model
                        continue

                    if isinstance(chat_event, ToolCallEvent):
                        pending_tool_calls[chat_event.tool_call_id] = {
                            "tool_name": chat_event.tool_name,
                            "tool_input": chat_event.arguments,
                        }
                        if _is_plan_exit_tool(chat_event.tool_name):
                            extracted = _extract_plan_from_tool_args(chat_event.arguments)
                            # Diagnostic doubles as the arg-shape confirmation:
                            # logs the actual argument keys for plan-exit tools so
                            # the extraction key set stays correct over time.
                            logger.info(
                                "[PLAN-DIAG] droid plan-exit tool=%s arg_keys=%s extracted=%s",
                                chat_event.tool_name,
                                sorted(chat_event.arguments.keys()),
                                "yes" if extracted else "no",
                            )
                            if extracted is not None:
                                structured_plan = extracted
                        if stream_event.data.get("kind") != "permission_request":
                            await self._apply_pre_tool_lifecycle(
                                chat_event.tool_name,
                                chat_event.arguments,
                            )
                    elif isinstance(chat_event, ToolResultEvent):
                        pending = pending_tool_calls.pop(chat_event.tool_call_id, {})
                        tool_input = pending.get("tool_input")
                        structured_success = stream_event.data.get("success")
                        is_error = (
                            not structured_success if isinstance(structured_success, bool) else None
                        )
                        if is_error is True:
                            tool_response = chat_event.error
                        elif is_error is False:
                            tool_response = chat_event.result
                        else:
                            tool_response = (
                                chat_event.result
                                if chat_event.result is not None
                                else chat_event.error
                            )
                        await self._apply_post_tool_lifecycle(
                            str(pending.get("tool_name", "")),
                            tool_input if isinstance(tool_input, dict) else {},
                            tool_response,
                            is_error=is_error,
                        )
                        # A completed tool ends the current prose paragraph; a
                        # following heading must start a new line to render. If the
                        # tool executed/mutated after a plan body is present, stop
                        # capturing so its narrated output is excluded (#15724).
                        plan_pending_break = True
                        if plan_capture_open and _closes_plan_capture(
                            str(pending.get("tool_name", "")), "".join(plan_text_parts)
                        ):
                            plan_capture_open = False
                    elif (
                        isinstance(chat_event, TextChunk)
                        and stream_event.event_type == "content_delta"
                    ):
                        saw_content_delta = True
                        if plan_capture_open:
                            if plan_pending_break and plan_text_parts:
                                plan_text_parts.append("\n\n")
                            plan_pending_break = False
                            plan_text_parts.append(chat_event.content)

                    if isinstance(chat_event, DoneEvent):
                        # Droid can emit an intermediate ``result``/DoneEvent
                        # before the agent is actually done (e.g. after preamble
                        # text, but before its tool calls and the real plan).
                        # Returning here truncated the turn (cancelling the
                        # in-flight tool) and broadcast only the partial preamble
                        # as the plan (#15642). Remember the latest DoneEvent and
                        # defer the plan broadcast to the true end of the stream,
                        # surfacing a single DoneEvent.
                        final_done = chat_event
                        continue

                    if chat_event is not None:
                        yield chat_event

                if self._plan_exit_blocked_this_turn:
                    # The permission resolver already broadcast the spec and
                    # parked the turn for this ExitSpecMode; the decision has
                    # since resolved, so don't re-broadcast post-loop (#15682).
                    self._plan_exit_blocked_this_turn = False
                elif structured_plan is not None:
                    await self._maybe_broadcast_pending_plan(structured_plan, True, structured=True)
                else:
                    await self._maybe_broadcast_pending_plan(
                        "".join(plan_text_parts), saw_content_delta
                    )
                yield final_done or DoneEvent(
                    tool_calls_count=0,
                    sdk_session_id=self.sdk_session_id,
                    context_window=self._resolve_context_window(),
                )
            except OSError as exc:
                logger.error("Droid managed session %s error: %s", self.conversation_id, exc)
                yield TextChunk(content=f"Generation failed: {exc}")
                yield DoneEvent(
                    tool_calls_count=0,
                    sdk_session_id=self.sdk_session_id,
                    context_window=self._resolve_context_window(),
                )

    def _translate_event(self, event: StreamEvent) -> ChatEvent | None:
        if event.event_type == "content_delta":
            kind = event.data.get("kind") or "text"
            if kind == "text":
                content = event.data.get("content") or event.data.get("text") or ""
                return TextChunk(content=content) if content else None
            if kind == "thinking":
                content = event.data.get("content") or event.data.get("thinking") or ""
                return ThinkingEvent(content=content) if content else None
            if kind in {"tool_use", "permission_request"}:
                raw_name = event.data.get("tool_name") or event.data.get("name") or "unknown"
                tool_name = self._tool_name_adapter()(str(raw_name))
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
                        normalized.get("mcp_server") or event.data.get("server_name") or "droid"
                    ),
                    arguments=arguments,
                )
            if kind == "tool_result":
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
            usage = event.data.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            return DoneEvent(
                tool_calls_count=0,
                input_tokens=usage.get("input_tokens") or usage.get("inputTokens"),
                output_tokens=usage.get("output_tokens") or usage.get("outputTokens"),
                context_window=self._resolve_context_window(),
                sdk_session_id=self.sdk_session_id,
            )

        if event.event_type == "error":
            _log_upstream_error_event("droid", self, event.data)
            message = event.data.get("message", "Unknown error")
            return TextChunk(content=f"Error: {message}")

        return None

    async def drain_pending_response(self) -> None:
        handle = self._backend._handles.get(self.conversation_id)
        if handle is None or handle.process.stdout is None:
            return
        while True:
            try:
                line = await asyncio.wait_for(handle.process.stdout.readline(), timeout=0.01)
            except TimeoutError:
                return
            if not line:
                return

    async def interrupt(self) -> None:
        self.cancel_pending_approval()
        await self._backend.interrupt(self)


class DroidWebChatBackend:
    """Per-session Droid stream-jsonrpc backend."""

    provider = "droid"

    def __init__(
        self,
        *,
        sandbox_config: SandboxConfig | None = None,
        default_model: str | None = None,
        prompt_timeout: float | None = None,
    ) -> None:
        self._sandbox_config = sandbox_config
        self._default_model = default_model
        self._prompt_timeout = _resolve_timeout(
            prompt_timeout,
            env_name=DROID_ACP_PROMPT_TIMEOUT_ENV,
            default=DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS,
        )
        self._health = ProviderBackendHealth(provider=self.provider, available=False)
        self._handles: dict[str, _DroidProcessHandle] = {}
        self._permission_resolver = DroidPermissionResolver(droid_tool_name_adapter)

    def set_sandbox_config(self, config: SandboxConfig) -> None:
        self._sandbox_config = config.model_copy(deep=True)

    async def start(self, *, background: bool = False) -> None:
        del background
        path = shutil.which("droid")
        if not path:
            self._health = ProviderBackendHealth(
                provider=self.provider,
                available=False,
                startup_error="droid CLI not found in PATH",
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

    async def attach_session(
        self,
        session: DroidManagedChatSession,
        *,
        model: str | None = None,
    ) -> None:
        if model:
            session._model = model
        elif not session._model:
            session._model = self._default_model

        await self.start()
        if not self._health.available:
            raise RuntimeError(self._health.startup_error or "Droid backend unavailable")

        await self.detach_session(session)

        droid_path = shutil.which("droid")
        if not droid_path:
            raise RuntimeError("droid CLI not found in PATH")

        cwd = await asyncio.to_thread(_resolve_droid_cwd, session.project_path)
        cmd = [
            droid_path,
            "exec",
            "--input-format",
            "stream-jsonrpc",
            "--output-format",
            "stream-jsonrpc",
            "--auto",
            "low",
            "--cwd",
            cwd,
        ]
        if session._model:
            session._model = _validated_droid_option(session._model, "model")
            cmd.extend(["--model", session._model])
        if session.reasoning_effort and session.reasoning_effort != "auto":
            reasoning_effort = _validated_droid_option(session.reasoning_effort, "reasoning effort")
            resolution = resolve_spawn_reasoning(
                provider="droid",
                model=session._model,
                requested_effort=reasoning_effort,
                reasoning_required=False,
            )
            if resolution.effective_effort is None:
                raise ValueError(resolution.message or "Unsupported Droid reasoning effort")
            session.reasoning_effort = resolution.effective_effort
            cmd.extend(["--reasoning-effort", resolution.effective_effort])

        env = os.environ.copy()
        env["GOBBY_HOOKS_DISABLED"] = "1"
        env["GOBBY_WEB_CHAT_CHILD"] = "1"
        sandbox_config = launch_sandbox_config(session, self._sandbox_config) or SandboxConfig(
            enabled=False
        )
        if sandbox_config.enabled:
            daemon_cfg = getattr(session, "_config", None)
            websocket = getattr(daemon_cfg, "websocket", None)
            launch = await prepare_sandbox_launch(
                config=sandbox_config,
                provider="droid",
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
        handle = _DroidProcessHandle(process, stderr_task=stderr_task)
        self._handles[session.conversation_id] = handle
        try:
            init_event = await self._initialize_session(handle, session, cwd)
        except Exception:
            self._handles.pop(session.conversation_id, None)
            await self._terminate_handle(handle)
            raise

        session.sdk_session_id = str(init_event.data.get("session_id") or "")
        model = init_event.data.get("model")
        if isinstance(model, str) and model:
            session._model = model
        session._connected = True
        session.last_activity = datetime.now(UTC)

    async def detach_session(self, session: DroidManagedChatSession) -> None:
        handle = self._handles.pop(session.conversation_id, None)
        if handle is not None:
            await self._terminate_handle(handle)
        session._connected = False

    async def send_message(
        self,
        session: DroidManagedChatSession,
        prompt: str,
    ) -> AsyncIterator[StreamEvent]:
        handle = self._handles.get(session.conversation_id)
        if handle is None or handle.process.returncode is not None:
            await self.attach_session(session, model=session._model)
            handle = self._handles[session.conversation_id]
        if handle.process.stdin is None or handle.process.stdout is None:
            raise RuntimeError("Droid process streams unavailable")

        try:
            await self._send_jsonrpc_request(
                handle,
                "message",
                "droid.add_user_message",
                {"text": prompt},
            )
        except (BrokenPipeError, ConnectionError, RuntimeError) as exc:
            if "Connection lost" not in str(exc) and not isinstance(exc, BrokenPipeError):
                raise
            logger.warning(
                "Droid stream for %s was closed before send; reattaching once",
                session.conversation_id,
            )
            await self.detach_session(session)
            await self.attach_session(session, model=session._model)
            handle = self._handles[session.conversation_id]
            await self._send_jsonrpc_request(
                handle,
                "message",
                "droid.add_user_message",
                {"text": prompt},
            )

        async for event in self._read_until_terminal(handle, session):
            if event.event_type == "error" and event.data.get("code") in {"eof", "timeout"}:
                await self.detach_session(session)
            yield event

    async def interrupt(self, session: DroidManagedChatSession) -> None:
        handle = self._handles.get(session.conversation_id)
        if handle is None or handle.process.returncode is not None:
            return
        handle.process.send_signal(signal.SIGINT)

    async def switch_model(self, session: DroidManagedChatSession, new_model: str) -> None:
        session._model = new_model
        await self.detach_session(session)
        await self.attach_session(session, model=new_model)

    async def _initialize_session(
        self,
        handle: _DroidProcessHandle,
        session: DroidManagedChatSession,
        cwd: str,
    ) -> StreamEvent:
        params: dict[str, Any] = {
            "machineId": DROID_MACHINE_ID,
            "cwd": cwd,
        }
        if session.sdk_session_id:
            params["sessionId"] = session.sdk_session_id
        if session._model:
            params["modelId"] = session._model
        if session.reasoning_effort and session.reasoning_effort != "auto":
            params["reasoningEffort"] = session.reasoning_effort

        request_id = await self._send_jsonrpc_request(
            handle,
            "init",
            "droid.initialize_session",
            params,
        )
        async for event in self._read_until_init(handle, request_id):
            if event.event_type == "error":
                message = event.data.get("message") or "Droid initialize_session failed"
                raise RuntimeError(str(message))
            return event
        raise RuntimeError("Droid initialize_session completed without a session id")

    async def _send_jsonrpc_request(
        self,
        handle: _DroidProcessHandle,
        prefix: str,
        method: str,
        params: dict[str, Any],
    ) -> str:
        if handle.process.stdin is None:
            raise RuntimeError("Droid stdin stream unavailable")

        handle.request_counter += 1
        request_id = f"gobby-{prefix}-{handle.request_counter}"
        payload = {
            "factoryApiVersion": DROID_FACTORY_API_VERSION,
            "factoryProtocolVersion": DROID_FACTORY_PROTOCOL_VERSION,
            "type": "request",
            "jsonrpc": DROID_JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        }
        handle.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await handle.process.stdin.drain()
        return request_id

    async def _send_jsonrpc_response(
        self,
        handle: _DroidProcessHandle,
        request_id: str,
        result: dict[str, Any],
    ) -> None:
        if handle.process.stdin is None:
            raise RuntimeError("Droid stdin stream unavailable")

        payload = {
            "factoryApiVersion": DROID_FACTORY_API_VERSION,
            "factoryProtocolVersion": DROID_FACTORY_PROTOCOL_VERSION,
            "type": "response",
            "jsonrpc": DROID_JSONRPC_VERSION,
            "id": request_id,
            "result": result,
        }
        handle.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await handle.process.stdin.drain()

    async def _read_until_init(
        self,
        handle: _DroidProcessHandle,
        request_id: str,
    ) -> AsyncIterator[StreamEvent]:
        stdout = handle.process.stdout
        if stdout is None:
            yield StreamEvent(
                event_type="error",
                data={"code": "stdout", "message": "Droid stdout stream unavailable"},
            )
            return

        deadline = _ProgressDeadline(self._prompt_timeout)
        while True:
            try:
                line = await _readline_with_progress(stdout, deadline)
            except TimeoutError as exc:
                raise TimeoutError(
                    "Timed out waiting for Droid initialize_session after "
                    f"{self._prompt_timeout:.1f}s"
                ) from exc
            if not line:
                yield StreamEvent(
                    event_type="error",
                    data={
                        "code": "eof",
                        "message": "Droid stream ended before initialize_session completed",
                    },
                )
                return
            events = parse_droid_stream_line(line)
            if not events:
                continue
            deadline.renew()
            for event in events:
                event_request_id = event.data.get("request_id")
                if event.event_type == "init" and event_request_id == request_id:
                    yield event
                    return
                if event.event_type == "error" and event_request_id in {None, request_id}:
                    yield event
                    return

    async def _read_until_terminal(
        self,
        handle: _DroidProcessHandle,
        session: DroidManagedChatSession,
    ) -> AsyncIterator[StreamEvent]:
        stdout = handle.process.stdout
        if stdout is None:
            yield StreamEvent(
                event_type="error",
                data={"code": "stdout", "message": "Droid stdout stream unavailable"},
            )
            return

        deadline = _ProgressDeadline(self._prompt_timeout)
        try:
            while True:
                try:
                    line = await _readline_with_progress(stdout, deadline)
                except TimeoutError:
                    await self._abandon_handle(handle, session.conversation_id)
                    yield StreamEvent(
                        event_type="error",
                        data={
                            "code": "timeout",
                            "message": (
                                "Timed out waiting for Droid stream progress after "
                                f"{self._prompt_timeout:.1f}s"
                            ),
                        },
                    )
                    return
                if not line:
                    yield StreamEvent(
                        event_type="error",
                        data={"code": "eof", "message": "Droid stream ended before result"},
                    )
                    return
                events = parse_droid_stream_line(line)
                if not events:
                    continue
                deadline.renew()
                permission_events = [
                    event
                    for event in events
                    if event.event_type == "content_delta"
                    and event.data.get("kind") == "permission_request"
                ]
                if permission_events:
                    await self._handle_permission_request(handle, session, permission_events)

                for event in events:
                    if event in permission_events:
                        continue
                    yield event
                    if event.event_type in {"result", "error"}:
                        return
        except asyncio.CancelledError:
            await self._abandon_handle(handle, session.conversation_id)
            raise

    async def _handle_permission_request(
        self,
        handle: _DroidProcessHandle,
        session: DroidManagedChatSession,
        events: list[StreamEvent],
    ) -> None:
        request_id = events[0].data.get("request_id") if events else None
        if not isinstance(request_id, str) or not request_id:
            logger.warning("Droid permission request missing JSON-RPC request id")
            return

        selected_option = await self._permission_resolver.resolve(session, events)
        await self._send_jsonrpc_response(
            handle,
            request_id,
            {"selectedOption": selected_option},
        )

    async def _resolve_permission_request(
        self,
        session: DroidManagedChatSession,
        events: list[StreamEvent],
    ) -> str:
        return await self._permission_resolver.resolve(session, events)

    async def _abandon_handle(self, handle: _DroidProcessHandle, conversation_id: str) -> None:
        current = self._handles.get(conversation_id)
        if current is handle:
            self._handles.pop(conversation_id, None)
        await self._terminate_handle(handle)

    async def _terminate_handle(self, handle: _DroidProcessHandle) -> None:
        if handle.process.stdin is not None:
            handle.process.stdin.close()
        if handle.process.returncode is None:
            pid = getattr(handle.process, "pid", None)
            signaled = False
            if pid is not None:
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
                if pid is not None:
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
                        "Droid process %s stderr: %s",
                        conversation_id,
                        _redact_droid_stderr(text),
                    )
        except (RuntimeError, OSError) as exc:
            logger.debug("Failed to read Droid process stderr for %s: %s", conversation_id, exc)


__all__ = [
    "DroidManagedChatSession",
    "DroidWebChatBackend",
    "droid_tool_name_adapter",
    "parse_droid_stream_line",
]
