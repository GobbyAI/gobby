"""Codex daemon-owned web-chat backend."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.codex_impl.item_normalization import (
    build_post_tool_lifecycle_payload,
    build_pre_tool_lifecycle_payload,
    parse_mcp_arguments,
)
from gobby.agents.sandbox import CodexSandboxResolver, SandboxConfig
from gobby.llm.claude_models import ChatEvent, DoneEvent, TextChunk, ToolCallEvent, ToolResultEvent
from gobby.servers.chat_session_helpers import (
    _BASH_WRITE_PATTERNS,
    _PLAN_FILE_PATTERN,
    PendingApproval,
    build_compaction_context,
)
from gobby.servers.gemini_permissions import GeminiWebChatPermissionsMixin
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
    _BACKEND_START_TIMEOUT_SECONDS,
    ManagedChatSessionBase,
    ProviderBackendHealth,
    _error_message,
    _extract_text,
)
from gobby.servers.websocket.chat.backends.codex_events import (
    codex_context_window_from_record,
    codex_record_from_notification,
    codex_tool_event_data,
    codex_tool_event_data_from_record,
    codex_usage_from_parsed_message,
    normalize_codex_usage,
    prefer_codex_usage,
)
from gobby.sessions.transcripts.base import ParsedMessage, ParsedToolEvent
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.storage.config_store import ConfigStore

logger = logging.getLogger(__name__)

_CODEX_TRANSCRIPT_RETRY_ATTEMPTS = 5
_CODEX_TRANSCRIPT_RETRY_DELAY_SECONDS = 0.1
_CODEX_WEB_CHAT_APPROVAL_POLICY = "on-request"


def _extract_codex_delta(params: dict[str, Any]) -> str:
    """Extract a text delta from Codex notification params."""
    delta = params.get("delta")
    if isinstance(delta, str) and delta:
        return delta

    item = params.get("item")
    if isinstance(item, dict):
        content = item.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("delta") or ""
                    if isinstance(text, str) and text:
                        chunks.append(text)
            if chunks:
                return "".join(chunks)

    return ""


@dataclass
class CodexManagedChatSession(
    GeminiWebChatPermissionsMixin,
    ManagedChatSessionBase,
):
    """Web-chat session backed by the shared Codex app-server backend."""

    provider: str = field(default="codex", init=False)
    chat_mode: str = field(default="plan")
    _thread_id: str | None = field(default=None, repr=False)
    _turn_id: str | None = field(default=None, repr=False)
    _transcript_path: str | None = field(default=None, repr=False)
    _transcript_retry_attempts: int = field(
        default=_CODEX_TRANSCRIPT_RETRY_ATTEMPTS,
        repr=False,
    )
    _transcript_retry_delay_seconds: float = field(
        default=_CODEX_TRANSCRIPT_RETRY_DELAY_SECONDS,
        repr=False,
    )
    _pending_approval: PendingApproval | None = field(default=None, repr=False)
    _pending_approval_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_approval_decision: str | None = field(default=None, repr=False)
    _plan_approved: bool = field(default=False, repr=False)
    _plan_feedback: str | None = field(default=None, repr=False)
    _before_tool_cached_responses: dict[str, dict[str, Any] | None] = field(
        default_factory=dict,
        repr=False,
    )
    _before_tool_inflight_tasks: dict[str, asyncio.Task[dict[str, Any] | None]] = field(
        default_factory=dict,
        repr=False,
    )

    def _reset_before_tool_state(self) -> None:
        """Clear per-turn pre-tool lifecycle dedup state."""
        for task in self._before_tool_inflight_tasks.values():
            if not task.done():
                task.cancel()
        self._before_tool_inflight_tasks.clear()
        self._before_tool_cached_responses.clear()

    async def _dispatch_before_tool_once(
        self,
        dedup_key: str | None,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not dedup_key:
            return await self._apply_pre_tool_lifecycle(tool_name, tool_input)

        if dedup_key in self._before_tool_cached_responses:
            return self._before_tool_cached_responses[dedup_key]

        task_key = dedup_key
        task = self._before_tool_inflight_tasks.get(task_key)
        if task is None:
            task = asyncio.create_task(self._apply_pre_tool_lifecycle(tool_name, tool_input))
            self._before_tool_inflight_tasks[task_key] = task

            def _finalize_pre_tool_task(
                completed_task: asyncio.Task[dict[str, Any] | None],
                *,
                key: str = task_key,
            ) -> None:
                current_task = self._before_tool_inflight_tasks.get(key)
                if current_task is completed_task:
                    self._before_tool_inflight_tasks.pop(key, None)
                if completed_task.cancelled():
                    self._before_tool_cached_responses.pop(key, None)
                    return
                if completed_task.exception() is not None:
                    self._before_tool_cached_responses.pop(key, None)
                    return
                self._before_tool_cached_responses[key] = completed_task.result()

            task.add_done_callback(_finalize_pre_tool_task)

        return await asyncio.shield(task)

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
        if not self._connected:
            await self.start(model=self._model)

        prompt = _extract_text(content)
        context_parts: list[str] = []
        if self.system_prompt_override:
            context_parts.append(self.system_prompt_override)

        session_ref = (
            f"#{self.seq_num}" if self.seq_num else (self.db_session_id or self.conversation_id)
        )
        context_parts.append(
            build_compaction_context(
                session_ref=session_ref,
                project_id=self.project_id,
                cwd=self.project_path,
                source="codex_web_chat",
            )
        )
        plan_ctx = self._pop_plan_mode_context()
        if plan_ctx:
            context_parts.append(plan_ctx)
        deferred_context = self._consume_deferred_context()
        if deferred_context:
            context_parts.append(deferred_context)

        if self._on_before_agent:
            resp = await self._on_before_agent({"prompt": prompt, "source": "codex_web_chat"})
            if resp and resp.get("context"):
                context_parts.append(str(resp["context"]))

        context_prefix = "\n\n".join(part for part in context_parts if part)

        async with self._lock:
            self.last_activity = datetime.now(UTC)
            self.message_index += 1
            async for event in self._backend.send_message(
                self,
                prompt,
                context_prefix=context_prefix or None,
            ):
                yield event

    async def interrupt(self) -> None:
        await self._backend.interrupt(self)

    async def _get_transcript_offset(self) -> int:
        if not self._transcript_path:
            return 0

        def _stat_size() -> int:
            try:
                return os.path.getsize(self._transcript_path or "")
            except OSError:
                return 0

        return await asyncio.to_thread(_stat_size)

    async def _get_transcript_assistant_text_since(self, offset: int) -> str:
        if not self._transcript_path:
            return ""

        def _read_assistant_text() -> str:
            try:
                with open(self._transcript_path or "", encoding="utf-8") as handle:
                    handle.seek(offset)
                    parser = CodexTranscriptParser(session_id=self._thread_id)
                    parsed = parser.parse_lines(handle.readlines())
            except OSError:
                return ""

            from gobby.sessions.transcripts.base import ParsedMessage

            assistant_chunks = [
                message.content.strip()
                for message in parsed
                if isinstance(message, ParsedMessage)
                and message.role == "assistant"
                and message.content.strip()
            ]
            return "\n\n".join(assistant_chunks)

        for _ in range(self._transcript_retry_attempts):
            assistant_text = await asyncio.to_thread(_read_assistant_text)
            if assistant_text:
                return assistant_text
            await asyncio.sleep(self._transcript_retry_delay_seconds)
        return ""

    async def _get_transcript_records_since(
        self,
        offset: int,
    ) -> list[ParsedMessage | ParsedToolEvent]:
        if not self._transcript_path:
            return []

        def _read_records() -> list[ParsedMessage | ParsedToolEvent]:
            try:
                with open(self._transcript_path or "", encoding="utf-8") as handle:
                    handle.seek(offset)
                    parser = CodexTranscriptParser(session_id=self._thread_id)
                    return parser.parse_lines(handle.readlines())
            except OSError:
                return []

        records: list[ParsedMessage | ParsedToolEvent] = []
        for _ in range(self._transcript_retry_attempts):
            records = await asyncio.to_thread(_read_records)
            if records:
                return records
            await asyncio.sleep(self._transcript_retry_delay_seconds)
        return records


class CodexWebChatBackend:
    """Shared daemon-owned Codex app-server backend."""

    provider = "codex"

    def __init__(
        self,
        *,
        client: CodexAppServerClient | None = None,
        transcript_retry_attempts: int = _CODEX_TRANSCRIPT_RETRY_ATTEMPTS,
        transcript_retry_delay_seconds: float = _CODEX_TRANSCRIPT_RETRY_DELAY_SECONDS,
        sandbox_config: SandboxConfig | None = None,
    ) -> None:
        self._client = client
        self._sandbox_config = sandbox_config
        self._health = ProviderBackendHealth(
            provider=self.provider,
            available=False,
            startup_error="Codex app-server client not configured",
        )
        self._sessions_by_thread: dict[str, CodexManagedChatSession] = {}
        self._startup_task: asyncio.Task[None] | None = None
        self.transcript_retry_attempts = transcript_retry_attempts
        self.transcript_retry_delay_seconds = transcript_retry_delay_seconds

    @property
    def client(self) -> CodexAppServerClient | None:
        """Expose the shared Codex app-server client for callers."""
        return self._client

    async def _start_inner(self) -> None:
        if self._client is None:
            self._health = ProviderBackendHealth(
                provider=self.provider,
                available=False,
                startup_error="codex CLI not found in PATH",
            )
            return

        if self._client.is_connected:
            self._health = ProviderBackendHealth(provider=self.provider, available=True)
            return

        try:
            await asyncio.wait_for(
                self._client.start(),
                timeout=_BACKEND_START_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            try:
                await self._client.stop()
            except Exception:
                logger.debug("Codex backend cleanup after failed startup", exc_info=True)
            self._health = ProviderBackendHealth(
                provider=self.provider,
                available=False,
                startup_error=_error_message(exc),
            )
            logger.warning("Codex backend startup failed: %s", exc)
            return

        self._client.register_approval_handler(self.handle_approval_request)
        self._health = ProviderBackendHealth(provider=self.provider, available=True)

    async def start(self, *, background: bool = False) -> None:
        if self._health.available:
            return
        if self._startup_task and not self._startup_task.done():
            if not background:
                await self._startup_task
            return

        self._startup_task = asyncio.create_task(self._start_inner())
        if not background:
            await self._startup_task

    async def stop(self) -> None:
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            try:
                await self._startup_task
            except asyncio.CancelledError:
                pass
        self._startup_task = None
        if self._client and self._client.is_connected:
            await self._client.stop()
        self._health = ProviderBackendHealth(provider=self.provider, available=False)

    def health(self) -> ProviderBackendHealth:
        return self._health

    async def attach_session(
        self,
        session: CodexManagedChatSession,
        *,
        model: str | None = None,
    ) -> None:
        if model:
            session._model = model

        await self.start()
        if not self._health.available or self._client is None:
            raise RuntimeError(self._health.startup_error or "Codex backend unavailable")

        if session._thread_id:
            thread = await self._client.resume_thread(session._thread_id)
        elif session.resume_session_id:
            thread = await self._client.resume_thread(session.resume_session_id)
        else:
            thread = await self._client.start_thread(
                cwd=session.project_path or ".",
                model=session._model,
                approval_policy=_CODEX_WEB_CHAT_APPROVAL_POLICY,
                sandbox=CodexSandboxResolver.thread_sandbox_policy(self._sandbox_config),
            )

        session._thread_id = thread.id
        session.sdk_session_id = thread.id
        session._transcript_path = getattr(thread, "path", None)
        session._connected = True
        session.last_activity = datetime.now(UTC)
        self._sessions_by_thread[thread.id] = session

    async def detach_session(self, session: CodexManagedChatSession) -> None:
        session._connected = False
        session._turn_id = None
        if session._thread_id:
            self._sessions_by_thread.pop(session._thread_id, None)

    @staticmethod
    def _decline_response(method: str) -> dict[str, Any]:
        if method == "mcpServer/elicitation/request":
            return {"action": "cancel", "content": None, "_meta": None}
        return {"decision": "decline"}

    @staticmethod
    def _accept_response(method: str) -> dict[str, Any]:
        if method == "mcpServer/elicitation/request":
            return {"action": "accept", "content": None, "_meta": None}
        return {"decision": "accept"}

    @staticmethod
    def _extract_mcp_tool_name(message: Any) -> str | None:
        if not isinstance(message, str):
            return None
        match = re.search(r'run tool "([^"]+)"', message)
        if not match:
            return None
        tool_name = match.group(1).strip()
        return tool_name or None

    @staticmethod
    def _extract_before_tool_dedup_key(params: dict[str, Any]) -> str | None:
        for key in ("itemId", "elicitationId"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        item_id = item.get("id") or item.get("itemId")
        if isinstance(item_id, str) and item_id:
            return item_id
        return None

    def _translate_approval_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any]]:
        if method == "mcpServer/elicitation/request":
            meta = params.get("_meta")
            if not isinstance(meta, dict) or meta.get("codex_approval_kind") != "mcp_tool_call":
                return None, {}
            server_name = params.get("serverName")
            tool_name = self._extract_mcp_tool_name(params.get("message"))
            elicitation_data = parse_mcp_arguments(meta.get("tool_params"))
            if (
                isinstance(server_name, str)
                and server_name
                and isinstance(tool_name, str)
                and tool_name
            ):
                elicitation_data["server_name"] = server_name
                elicitation_data["tool_name"] = tool_name
                return "mcp__gobby__call_tool", elicitation_data
            return None, {}

        item_type = method.removeprefix("item/").removesuffix("/requestApproval")
        nested_payload = params.get(item_type)
        payload: dict[str, Any] = {}
        if isinstance(nested_payload, dict):
            payload.update(nested_payload)
        payload.update(params)

        if item_type == "commandExecution":
            command = payload.get("parsedCmd") or payload.get("command") or ""
            if isinstance(command, str):
                return "Bash", {"command": command}
            return "Bash", {}

        if item_type == "fileChange":
            changes = payload.get("changes")
            file_change_input: dict[str, Any] = {}
            if isinstance(changes, list):
                file_change_input["changes"] = changes
                if changes and isinstance(changes[0], dict):
                    first = changes[0]
                    for key in ("file_path", "path", "target_path"):
                        value = first.get(key)
                        if isinstance(value, str) and value:
                            file_change_input["file_path"] = value
                            break
            return "Write", file_change_input

        if item_type == "mcpToolCall":
            server_name = payload.get("serverName") or payload.get("server")
            raw_name = (
                payload.get("tool_name")
                or payload.get("toolName")
                or payload.get("name")
                or payload.get("tool")
            )
            mcp_input_data: dict[str, Any] = {}
            for key in ("tool_input", "toolArgs", "arguments", "input", "params"):
                if key not in payload:
                    continue
                mcp_input_data = parse_mcp_arguments(payload.get(key))
                break
            if (
                isinstance(server_name, str)
                and server_name
                and isinstance(raw_name, str)
                and raw_name
            ):
                mcp_input_data["server_name"] = server_name
                mcp_input_data["tool_name"] = raw_name
                return "mcp__gobby__call_tool", mcp_input_data
            return None, mcp_input_data

        return None, {}

    @staticmethod
    def _global_rules_for_session(session: CodexManagedChatSession) -> list[str]:
        session_manager = getattr(session, "_session_manager_ref", None)
        db = getattr(session_manager, "db", None) if session_manager else None
        if db is None:
            return list(DEFAULT_GLOBAL_APPROVAL_RULES)
        return get_global_approval_rules(ConfigStore(db))

    async def handle_approval_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            return self._decline_response(method)

        session = self._sessions_by_thread.get(thread_id)
        if session is None:
            return self._decline_response(method)

        tool_name, input_data = self._translate_approval_request(method, params)
        if not tool_name:
            return self._decline_response(method)

        lifecycle_response = await session._dispatch_before_tool_once(
            self._extract_before_tool_dedup_key(params),
            tool_name,
            input_data,
        )
        if isinstance(lifecycle_response, dict) and lifecycle_response.get("decision") == "block":
            return self._decline_response(method)

        out_of_repo_path = find_out_of_repo_write_path(
            tool_name,
            input_data,
            project_path=session.project_path,
        )
        if out_of_repo_path:
            return self._decline_response(method)

        if session.chat_mode == "plan":
            if tool_name in {"Write", "Edit", "NotebookEdit"}:
                file_path = input_data.get("file_path", "")
                if (
                    not isinstance(file_path, str)
                    or not file_path
                    or not _PLAN_FILE_PATTERN.match(file_path)
                ):
                    return self._decline_response(method)
            elif (
                tool_name == "Bash"
                and _BASH_WRITE_PATTERNS.search(str(input_data.get("command", "")))
                and not is_gcode_shell_command(input_data)
            ):
                return self._decline_response(method)

        if session.chat_mode == "bypass":
            return self._accept_response(method)

        if is_tool_auto_allowed(
            tool_name,
            input_data,
            session_rules=normalize_approved_tool_keys(session._approved_tools),
            project_rules=await load_project_approval_rules_async(session.project_path),
            global_rules=self._global_rules_for_session(session),
        ):
            return self._accept_response(method)

        approval = await session._wait_for_tool_approval(tool_name, input_data)
        if isinstance(approval, dict) and approval.get("decision") == "accept":
            return self._accept_response(method)
        return self._decline_response(method)

    async def send_message(
        self,
        session: CodexManagedChatSession,
        prompt: str,
        *,
        context_prefix: str | None = None,
    ) -> AsyncIterator[ChatEvent]:
        if not self._health.available or self._client is None:
            raise RuntimeError(self._health.startup_error or "Codex backend unavailable")
        if not session._thread_id:
            raise RuntimeError("Codex session missing threadId")

        event_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        turn_completed = asyncio.Event()
        saw_text_output = False
        tool_calls_count = 0
        started_tool_call_ids: set[str] = set()
        completed_tool_call_ids: set[str] = set()
        latest_transcript_usage: dict[str, int | None] | None = None
        latest_transcript_context_window: int | None = None
        transcript_offset = await session._get_transcript_offset()
        session._reset_before_tool_state()

        def _remember_record_usage(record: ParsedMessage | ParsedToolEvent) -> None:
            nonlocal latest_transcript_context_window, latest_transcript_usage
            if not isinstance(record, ParsedMessage):
                return
            usage = codex_usage_from_parsed_message(record)
            if usage is not None:
                latest_transcript_usage = usage
            context_window = codex_context_window_from_record(record)
            if context_window is not None:
                latest_transcript_context_window = context_window

        def _start_tool_event(tool_event_data: dict[str, Any]) -> ToolCallEvent | None:
            nonlocal tool_calls_count
            tool_call_id = str(tool_event_data["tool_call_id"])
            if not tool_call_id or tool_call_id in started_tool_call_ids:
                return None
            started_tool_call_ids.add(tool_call_id)
            tool_calls_count += 1
            return ToolCallEvent(
                tool_call_id=tool_call_id,
                tool_name=str(tool_event_data["tool_name"]),
                server_name=str(tool_event_data["server_name"]),
                arguments=tool_event_data["arguments"],
            )

        def _complete_tool_events(tool_event_data: dict[str, Any]) -> list[ChatEvent]:
            tool_call_id = str(tool_event_data["tool_call_id"])
            if not tool_call_id or tool_call_id in completed_tool_call_ids:
                return []
            completed_tool_call_ids.add(tool_call_id)

            events: list[ChatEvent] = []
            start_event = _start_tool_event(tool_event_data)
            if start_event is not None:
                events.append(start_event)
            events.append(
                ToolResultEvent(
                    tool_call_id=tool_call_id,
                    success=bool(tool_event_data["success"]),
                    result=tool_event_data["result"],
                    error=tool_event_data["error"],
                )
            )
            return events

        def _events_from_transcript_record(
            record: ParsedMessage | ParsedToolEvent,
        ) -> list[ChatEvent]:
            _remember_record_usage(record)
            tool_event_data = codex_tool_event_data_from_record(record)
            if tool_event_data is None:
                return []
            if tool_event_data["phase"] == "begin":
                start_event = _start_tool_event(tool_event_data)
                return [start_event] if start_event is not None else []
            return _complete_tool_events(tool_event_data)

        def _matches(params: dict[str, Any]) -> bool:
            thread_id = params.get("threadId")
            if (
                isinstance(thread_id, str)
                and session._thread_id
                and thread_id != session._thread_id
            ):
                return False
            turn_id = params.get("turnId")
            if isinstance(turn_id, str) and session._turn_id and turn_id != session._turn_id:
                return False
            turn = params.get("turn")
            if isinstance(turn, dict):
                turn_identifier = turn.get("id")
                if (
                    isinstance(turn_identifier, str)
                    and session._turn_id
                    and turn_identifier != session._turn_id
                ):
                    return False
            return True

        def _enqueue(method: str, params: dict[str, Any]) -> None:
            if _matches(params):
                event_queue.put_nowait((method, params))

        event_methods = [
            "turn/started",
            "turn/completed",
            "thread/closed",
            "agent/messageDelta",
            "item/agentMessage/delta",
            "item/started",
            "item/completed",
            "response_item",
            "event_msg",
        ]
        for method in event_methods:
            self._client.add_notification_handler(method, _enqueue)

        try:
            turn = await self._client.start_turn(
                session._thread_id,
                prompt,
                context_prefix=context_prefix,
                model=session._model,
                effort=session.reasoning_effort,
            )
            session._turn_id = turn.id or session._turn_id

            while not turn_completed.is_set():
                try:
                    method, params = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                except TimeoutError:
                    continue

                if method in {"agent/messageDelta", "item/agentMessage/delta"}:
                    delta = _extract_codex_delta(params)
                    if delta:
                        saw_text_output = True
                        yield TextChunk(content=delta)
                    continue

                if method == "item/completed":
                    tool_event_data = codex_tool_event_data(params)
                    if tool_event_data is not None:
                        tool_call_id = str(tool_event_data["tool_call_id"])
                        if tool_call_id in completed_tool_call_ids:
                            continue
                        completed_tool_call_ids.add(tool_call_id)

                        post_tool_payload = build_post_tool_lifecycle_payload(
                            params,
                            tool_name_map=CodexAdapter.TOOL_MAP,
                        )
                        if post_tool_payload is not None:
                            tool_name, tool_input, tool_response = post_tool_payload
                            await session._apply_post_tool_lifecycle(
                                tool_name,
                                tool_input,
                                tool_response,
                            )

                        if tool_call_id not in started_tool_call_ids:
                            start_event = _start_tool_event(tool_event_data)
                            if start_event is not None:
                                yield start_event

                        yield ToolResultEvent(
                            tool_call_id=tool_call_id,
                            success=bool(tool_event_data["success"]),
                            result=tool_event_data["result"],
                            error=tool_event_data["error"],
                        )
                    continue

                if method == "turn/started":
                    session._reset_before_tool_state()
                    turn_id = params.get("turnId")
                    if not turn_id:
                        turn_data = params.get("turn")
                        if isinstance(turn_data, dict):
                            turn_id = turn_data.get("id")
                    if isinstance(turn_id, str) and turn_id:
                        session._turn_id = turn_id
                    continue

                if method == "item/started":
                    tool_event_data = codex_tool_event_data(params)
                    pre_tool_payload = build_pre_tool_lifecycle_payload(
                        params,
                        tool_name_map=CodexAdapter.TOOL_MAP,
                    )
                    if pre_tool_payload is not None:
                        tool_name, tool_input = pre_tool_payload
                        await session._dispatch_before_tool_once(
                            self._extract_before_tool_dedup_key(params),
                            tool_name,
                            tool_input,
                        )
                    if tool_event_data is not None:
                        start_event = _start_tool_event(tool_event_data)
                        if start_event is not None:
                            yield start_event
                    continue

                if method in {"response_item", "event_msg"}:
                    record = codex_record_from_notification(method, params)
                    if record is None:
                        continue
                    for event in _events_from_transcript_record(record):
                        yield event
                    continue

                if method == "thread/closed":
                    session._turn_id = None
                    yield DoneEvent(
                        tool_calls_count=tool_calls_count,
                        context_window=session._resolve_context_window(),
                    )
                    turn_completed.set()
                    continue

                if method == "turn/completed":
                    usage = params.get("usage", {})
                    if not isinstance(usage, dict):
                        usage = {}
                    transcript_records = await session._get_transcript_records_since(
                        transcript_offset
                    )
                    transcript_assistant_text: list[str] = []
                    for record in transcript_records:
                        for event in _events_from_transcript_record(record):
                            yield event
                        if (
                            isinstance(record, ParsedMessage)
                            and record.role == "assistant"
                            and record.content.strip()
                        ):
                            transcript_assistant_text.append(record.content.strip())

                    normalized_usage = prefer_codex_usage(
                        normalize_codex_usage(usage),
                        latest_transcript_usage,
                    )
                    session._turn_id = None
                    if not saw_text_output:
                        fallback_text = "\n\n".join(transcript_assistant_text)
                        if not fallback_text:
                            fallback_text = await session._get_transcript_assistant_text_since(
                                transcript_offset
                            )
                        if fallback_text:
                            yield TextChunk(content=fallback_text)

                    context_window = (
                        latest_transcript_context_window or session._resolve_context_window()
                    )
                    yield DoneEvent(
                        tool_calls_count=tool_calls_count,
                        input_tokens=normalized_usage["input_tokens"],
                        output_tokens=normalized_usage["output_tokens"],
                        cache_read_input_tokens=normalized_usage["cache_read_input_tokens"],
                        cache_creation_input_tokens=normalized_usage["cache_creation_input_tokens"],
                        total_input_tokens=normalized_usage["total_input_tokens"],
                        context_window=context_window,
                        sdk_session_id=session.sdk_session_id,
                    )
                    turn_completed.set()
        except Exception as exc:
            logger.error("Codex managed session %s error: %s", session.conversation_id, exc)
            yield TextChunk(content=f"Error: {exc}")
            yield DoneEvent(
                tool_calls_count=0,
                sdk_session_id=session.sdk_session_id,
                context_window=session._resolve_context_window(),
            )
        finally:
            for method in event_methods:
                self._client.remove_notification_handler(method, _enqueue)

    async def interrupt(self, session: CodexManagedChatSession) -> None:
        if not self._client or not session._thread_id or not session._turn_id:
            return
        await self._client.interrupt_turn(session._thread_id, session._turn_id)
        session._turn_id = None

    async def switch_model(self, session: CodexManagedChatSession, new_model: str) -> None:
        session._model = new_model


__all__ = [
    "CodexManagedChatSession",
    "CodexWebChatBackend",
]
