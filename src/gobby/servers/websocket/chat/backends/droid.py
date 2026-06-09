"""Droid daemon-owned web-chat backend."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.adapters.gemini_acp_client import StreamEvent
from gobby.agents.sandbox import SandboxConfig
from gobby.hooks.normalization import normalize_tool_fields
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.chat_session_helpers import (
    _BASH_WRITE_PATTERNS,
    _PLAN_FILE_PATTERN,
    PendingApproval,
    build_compaction_context,
)
from gobby.servers.tool_approvals import (
    DEFAULT_GLOBAL_APPROVAL_RULES,
    find_out_of_repo_write_path,
    get_global_approval_rules,
    is_gcode_shell_command,
    is_tool_auto_allowed,
    load_project_approval_rules_async,
    normalize_approved_tool_keys,
)
from gobby.servers.websocket.chat.backends.base import (
    ManagedChatSessionBase,
    ProviderBackendHealth,
    _extract_text,
    _log_upstream_error_event,
)
from gobby.servers.websocket.chat.backends.droid_stream import parse_droid_stream_line
from gobby.servers.websocket.chat.permissions import ManagedWebChatPermissionsMixin
from gobby.storage.config_store import ConfigStore

logger = logging.getLogger(__name__)

DROID_JSONRPC_VERSION = "2.0"
DROID_FACTORY_API_VERSION = "1.0.0"
DROID_FACTORY_PROTOCOL_VERSION = "1.25.0"
DROID_MACHINE_ID = "gobby-web-chat"
DROID_PERMISSION_CANCEL = "cancel"
DROID_PERMISSION_PROCEED_ONCE = "proceed_once"
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


# Plan-exit / spec tools whose argument carries the structured plan body. Droid
# presents a plan via ``ExitSpecMode`` (Factory "spec mode"); the others cover
# Claude-style ``ExitPlanMode`` and Codex-style ``update_plan`` should Droid ever
# surface them. Compared as alphanumeric-only lowercase so separators/casing in
# the upstream tool name don't matter.
_PLAN_EXIT_TOOL_KEYS = frozenset({"exitspecmode", "exitplanmode", "updateplan"})

# Argument keys, in priority order, that may hold the plan/spec body.
_PLAN_TOOL_ARG_KEYS: tuple[str, ...] = ("plan", "spec", "content", "markdown", "text")

# Tools that execute a command or mutate state. Droid narrates their output as
# assistant content *after* they run (e.g. reformatting ``git status`` into a
# results table). That post-execution narration is not plan content, so once
# such a tool completes the prose-plan capture stops (see ``_closes_plan_capture``).
# Compared as alphanumeric-only lowercase so separators/casing/namespacing in the
# upstream tool name don't matter.
_PLAN_CAPTURE_CLOSING_TOOLS = frozenset(
    {
        "bash",
        "shell",
        "sh",
        "exec",
        "execcommand",
        "runcommand",
        "command",
        "edit",
        "write",
        "multiedit",
        "notebookedit",
        "applypatch",
        "strreplace",
        "strreplaceeditor",
        "strreplacebasededittool",
    }
)

# A plan body is "present" once the captured prose has a markdown heading or
# enough substance. Guards capture-close so a research-first turn (run a read
# command, then present the plan) still surfaces the plan.
_PLAN_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s")
_PLAN_CAPTURE_MIN_CHARS = 160


def _is_plan_exit_tool(tool_name: str) -> bool:
    """Whether ``tool_name`` is a plan-exit/spec tool carrying a plan body."""
    key = "".join(ch for ch in tool_name.lower() if ch.isalnum())
    return key in _PLAN_EXIT_TOOL_KEYS


def _closes_plan_capture(tool_name: str, captured: str) -> bool:
    """Whether a completed tool ends prose-plan capture for this turn.

    Command/mutation tools narrate their output as assistant content *after*
    they run, which is execution narration rather than plan. Once such a tool
    completes and a plan body is already captured, stop accumulating prose so
    the post-execution narration does not leak into the broadcast plan
    (#15724). Guarded on a plan already being present so a research-first turn
    (read command, then present the plan) still surfaces the plan.
    """
    key = "".join(ch for ch in tool_name.lower() if ch.isalnum())
    if key not in _PLAN_CAPTURE_CLOSING_TOOLS:
        return False
    return (
        bool(_PLAN_HEADING_RE.search(captured)) or len(captured.strip()) >= _PLAN_CAPTURE_MIN_CHARS
    )


def _extract_plan_from_tool_args(arguments: dict[str, Any]) -> str | None:
    """Pull the plan/spec body from a plan-exit tool's arguments.

    Tries the known plan argument keys in priority order and returns the first
    non-empty string. Returns ``None`` when the tool carries no plan body (some
    CLIs use the tool purely as an exit signal), so the caller falls back to the
    accumulated assistant prose.
    """
    for key in _PLAN_TOOL_ARG_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


@dataclass
class DroidManagedChatSession(ManagedWebChatPermissionsMixin, ManagedChatSessionBase):
    """Web-chat session backed by a per-session Droid stream-jsonrpc process."""

    provider: str = field(default="droid", init=False)
    chat_mode: str = field(default="plan")
    _pending_question: dict[str, Any] | None = field(default=None, repr=False)
    _pending_answer_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_answers: dict[str, str] | None = field(default=None, repr=False)
    _pending_approval: PendingApproval | None = field(default=None, repr=False)
    _pending_approval_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_approval_decision: str | None = field(default=None, repr=False)
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

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
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
                        await self._apply_post_tool_lifecycle(
                            str(pending.get("tool_name", "")),
                            tool_input if isinstance(tool_input, dict) else {},
                            chat_event.result if chat_event.success else chat_event.error,
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
            except (RuntimeError, OSError, ConnectionError, asyncio.CancelledError) as exc:
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
        await self._backend.interrupt(self)


class DroidWebChatBackend:
    """Per-session Droid stream-jsonrpc backend."""

    provider = "droid"

    def __init__(
        self,
        *,
        sandbox_config: SandboxConfig | None = None,
        default_model: str | None = None,
    ) -> None:
        self._sandbox_config = sandbox_config
        self._default_model = default_model
        self._health = ProviderBackendHealth(provider=self.provider, available=False)
        self._handles: dict[str, _DroidProcessHandle] = {}

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

        cwd = session.project_path or "."
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
            cmd.extend(["--model", session._model])
        if session.reasoning_effort and session.reasoning_effort != "auto":
            cmd.extend(["--reasoning-effort", session.reasoning_effort])

        env = os.environ.copy()
        env["GOBBY_HOOKS_DISABLED"] = "1"
        env["GOBBY_WEB_CHAT_CHILD"] = "1"

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
            if event.event_type == "error" and event.data.get("code") == "eof":
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

        while True:
            line = await stdout.readline()
            if not line:
                yield StreamEvent(
                    event_type="error",
                    data={
                        "code": "eof",
                        "message": "Droid stream ended before initialize_session completed",
                    },
                )
                return
            for event in parse_droid_stream_line(line):
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

        while True:
            line = await stdout.readline()
            if not line:
                yield StreamEvent(
                    event_type="error",
                    data={"code": "eof", "message": "Droid stream ended before result"},
                )
                return
            events = parse_droid_stream_line(line)
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

    @staticmethod
    def _global_rules_for_session(session: DroidManagedChatSession) -> list[str]:
        session_manager = getattr(session, "_session_manager_ref", None)
        db = getattr(session_manager, "db", None) if session_manager else None
        if db is None:
            return list(DEFAULT_GLOBAL_APPROVAL_RULES)
        return get_global_approval_rules(ConfigStore(db))

    @staticmethod
    def _permission_tool_payload(event: StreamEvent) -> tuple[str, dict[str, Any], str]:
        raw_name = event.data.get("tool_name") or event.data.get("name") or "unknown"
        tool_name = droid_tool_name_adapter(str(raw_name))
        tool_input = event.data.get("tool_input") or event.data.get("input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_id = event.data.get("call_id") or event.data.get("id") or "unknown"
        return tool_name, tool_input, str(tool_id)

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

        selected_option = await self._resolve_permission_request(session, events)
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
        tool_payloads = [self._permission_tool_payload(event) for event in events]
        if not tool_payloads:
            return DROID_PERMISSION_CANCEL

        for tool_name, tool_input, _tool_id in tool_payloads:
            lifecycle_response = await session._apply_pre_tool_lifecycle(tool_name, tool_input)
            if (
                isinstance(lifecycle_response, dict)
                and lifecycle_response.get("decision") == "block"
            ):
                return DROID_PERMISSION_CANCEL

            if find_out_of_repo_write_path(
                tool_name,
                tool_input,
                project_path=session.project_path,
            ):
                return DROID_PERMISSION_CANCEL

            if session.chat_mode == "plan" and self._plan_mode_blocks_tool(tool_name, tool_input):
                return DROID_PERMISSION_CANCEL

        if session.chat_mode == "bypass":
            return DROID_PERMISSION_PROCEED_ONCE

        project_rules = await load_project_approval_rules_async(session.project_path)
        global_rules = self._global_rules_for_session(session)
        session_rules = normalize_approved_tool_keys(session._approved_tools)
        if all(
            is_tool_auto_allowed(
                tool_name,
                tool_input,
                session_rules=session_rules,
                project_rules=project_rules,
                global_rules=global_rules,
            )
            for tool_name, tool_input, _tool_id in tool_payloads
        ):
            return DROID_PERMISSION_PROCEED_ONCE

        if session.chat_mode == "plan":
            # Tool-plan model (#15682): Droid presents its finalized plan via the
            # ExitSpecMode plan-exit tool, riding the spec in the tool input.
            # ExitSpecMode arrives only as a permission request and is filtered
            # out of the session stream (_read_until_terminal), so this resolver
            # is the only place that sees it. Broadcast the spec and BLOCK on the
            # user's decision here — mirroring the native ExitPlanMode gate —
            # instead of cancelling and dropping it (which left the card never
            # surfacing). proceed_once exits Spec Mode and executes; cancel keeps
            # Droid in Spec Mode for revision (request_changes / reject /
            # timeout). Queued feedback rides the next turn's plan-mode context.
            for tool_name, tool_input, _tool_id in tool_payloads:
                if not _is_plan_exit_tool(tool_name):
                    continue
                spec = _extract_plan_from_tool_args(tool_input)
                if not spec:
                    # No spec body to show; fall through to the cancel below
                    # rather than blocking on an invisible plan card.
                    break
                await session._maybe_broadcast_pending_plan(spec, True, structured=True)
                session._plan_exit_blocked_this_turn = True
                decision = await session._wait_for_plan_decision()
                if decision == "approve":
                    return DROID_PERMISSION_PROCEED_ONCE
                return DROID_PERMISSION_CANCEL

            # Read-only planning mode (#15664): destructive tools were cancelled
            # in the per-tool loop above and auto-allowed reads already
            # proceeded. Any non-plan-exit tool still here needs interactive
            # approval, which cannot be granted during the headless plan turn;
            # cancel deterministically so the turn completes.
            return DROID_PERMISSION_CANCEL

        approval_tool_name, approval_input = self._approval_prompt_payload(tool_payloads)
        approval = await session._wait_for_tool_approval(approval_tool_name, approval_input)
        if isinstance(approval, dict) and approval.get("decision") == "accept":
            return DROID_PERMISSION_PROCEED_ONCE
        return DROID_PERMISSION_CANCEL

    @staticmethod
    def _approval_prompt_payload(
        tool_payloads: list[tuple[str, dict[str, Any], str]],
    ) -> tuple[str, dict[str, Any]]:
        if len(tool_payloads) == 1:
            tool_name, tool_input, _tool_id = tool_payloads[0]
            return tool_name, tool_input
        return (
            "DroidToolBatch",
            {
                "tool_uses": [
                    {"tool_name": tool_name, "tool_input": tool_input, "tool_id": tool_id}
                    for tool_name, tool_input, tool_id in tool_payloads
                ]
            },
        )

    @staticmethod
    def _plan_mode_blocks_tool(tool_name: str, tool_input: dict[str, Any]) -> bool:
        if tool_name in {"Write", "Edit", "NotebookEdit"}:
            file_path = tool_input.get("file_path", "")
            return (
                not isinstance(file_path, str)
                or not file_path
                or not _PLAN_FILE_PATTERN.match(file_path)
            )
        if tool_name == "Bash":
            if is_gcode_shell_command(tool_input):
                return False
            return bool(_BASH_WRITE_PATTERNS.search(str(tool_input.get("command", ""))))
        return False

    async def _terminate_handle(self, handle: _DroidProcessHandle) -> None:
        if handle.process.stdin is not None:
            handle.process.stdin.close()
        if handle.process.returncode is None:
            handle.process.terminate()
            try:
                await asyncio.wait_for(handle.process.wait(), timeout=2.0)
            except TimeoutError:
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
