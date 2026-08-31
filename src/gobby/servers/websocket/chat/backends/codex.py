"""Codex daemon-owned web-chat backend."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.codex_impl.item_normalization import (
    parse_mcp_arguments,
)
from gobby.agents.local_model import LocalModelError, ensure_local_model
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_resolvers import CodexSandboxResolver
from gobby.ai.endpoints import parse_endpoint_model_selector
from gobby.config.ai import GenerationEndpointConfig
from gobby.llm.claude_models import ChatEvent, DoneEvent, TextChunk
from gobby.servers.chat_session_helpers import (
    _BASH_WRITE_PATTERNS,
    PendingApproval,
    build_compaction_context,
)
from gobby.servers.tool_approvals import (
    DEFAULT_GLOBAL_APPROVAL_RULES,
    are_plan_mode_write_paths_allowed,
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
    launch_sandbox_config,
)
from gobby.servers.websocket.chat.backends.codex_turns import stream_codex_turn
from gobby.servers.websocket.chat.permissions import ManagedWebChatPermissionsMixin
from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcripts.base import ParsedMessage, ParsedToolEvent
from gobby.sessions.transcripts.codex import CodexTranscriptParser


def local_model_preflight_message(
    endpoint: GenerationEndpointConfig,
    error: LocalModelError,
) -> str:
    """Describe a failed local-model pre-flight without hiding the resolver's diagnosis.

    For non-owning runtimes such as vLLM the only remedy is a config change, so
    the served-model list / connection failure in ``error`` is the actionable
    part of the message.
    """
    return (
        "Codex local model pre-flight failed "
        f"(protocol={endpoint.protocol}, model={endpoint.model}): {error}"
    )


logger = logging.getLogger(__name__)

_CODEX_PROVIDER_ID = "codex"
_CODEX_TRANSCRIPT_RETRY_ATTEMPTS = 5
_CODEX_TRANSCRIPT_RETRY_DELAY_SECONDS = 0.1
_CODEX_WEB_CHAT_APPROVAL_POLICY = "on-request"


@dataclass
class CodexManagedChatSession(
    ManagedWebChatPermissionsMixin,
    ManagedChatSessionBase,
):
    """Web-chat session backed by the shared Codex app-server backend."""

    provider: str = field(default=_CODEX_PROVIDER_ID, init=False)
    chat_mode: str = field(default="plan")
    _model_selector: str | None = field(default=None, repr=False)
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
    _app_client: CodexAppServerClient | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _pending_approval_events: dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _pending_approval_decisions: dict[str, str] = field(default_factory=dict, repr=False)
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

    @property
    def model(self) -> str | None:
        """Return the UI-facing selector while keeping the wire model canonical."""
        return self._model_selector or self._model

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

    async def send_message(
        self,
        content: str | list[dict[str, Any]],
        *,
        request_parameters: Mapping[str, object] | None = None,
    ) -> AsyncIterator[ChatEvent]:
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
            saw_text_output = False
            plan_text_parts: list[str] = []
            async for event in self._backend.send_message(
                self,
                prompt,
                context_prefix=context_prefix or None,
                request_parameters=request_parameters,
            ):
                if isinstance(event, TextChunk) and event.content:
                    plan_text_parts.append(event.content)
                    saw_text_output = True
                if isinstance(event, DoneEvent):
                    # Managed CLIs present a plan as a normal assistant turn (no
                    # ExitPlanMode tool); surface it before the turn's DoneEvent
                    # so it flows through the shared plan-approval UX.
                    await self._maybe_broadcast_pending_plan(
                        "".join(plan_text_parts), saw_text_output
                    )
                yield event

    async def interrupt(self) -> None:
        await self._backend.interrupt(self)

    async def clear_context(self) -> bool:
        """Real context clear: archive the current thread and start a fresh one.

        Invoked by the plan-approval handler for Codex's "approve + clear
        context" option; the approved plan is re-seeded into the continuation
        turn so implementation proceeds on a clean thread.
        """
        cleared = await self._backend.clear_session_context(self)
        return bool(cleared)

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
                    parsed = normalize_transcript_records(
                        parser.parse_lines(handle.readlines()), self.provider
                    )
            except OSError:
                return ""

            assistant_chunks = [
                message.content.strip()
                for message in parsed
                if isinstance(message, ParsedMessage)
                and message.role == "assistant"
                and isinstance(message.content, str)
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
                    return normalize_transcript_records(
                        parser.parse_lines(handle.readlines()), self.provider
                    )
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

    provider = _CODEX_PROVIDER_ID

    def __init__(
        self,
        *,
        client: CodexAppServerClient | None = None,
        client_factory: Callable[..., CodexAppServerClient] | None = None,
        generation_endpoint: GenerationEndpointConfig | None = None,
        transcript_retry_attempts: int = _CODEX_TRANSCRIPT_RETRY_ATTEMPTS,
        transcript_retry_delay_seconds: float = _CODEX_TRANSCRIPT_RETRY_DELAY_SECONDS,
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        self._generation_endpoint = generation_endpoint
        self._health = ProviderBackendHealth(
            provider=self.provider,
            available=False,
            startup_error="Codex app-server client not configured",
        )
        self._sessions_by_thread: dict[str, CodexManagedChatSession] = {}
        self._startup_task: asyncio.Task[None] | None = None
        self.transcript_retry_attempts = transcript_retry_attempts
        self.transcript_retry_delay_seconds = transcript_retry_delay_seconds

    @staticmethod
    def native_sandbox_pin(config: SandboxConfig) -> str | None:
        """Return the Codex thread sandbox when SRT is the outer boundary."""
        if not config.enabled:
            return None
        if config.backend == "srt":
            return "danger-full-access"
        return CodexSandboxResolver.thread_sandbox_policy(config)

    @property
    def client(self) -> CodexAppServerClient | None:
        """Expose the shared Codex app-server client for callers."""
        return self._client

    async def _start_inner(self) -> None:
        if self._client is not None and self._client.is_connected:
            self._health = ProviderBackendHealth(provider=self.provider, available=True)
            return
        if self._client is not None or shutil.which("codex"):
            self._health = ProviderBackendHealth(provider=self.provider, available=True)
            return
        self._health = ProviderBackendHealth(
            provider=self.provider,
            available=False,
            startup_error="codex CLI not found in PATH",
        )

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

    def _client_for(self, session: CodexManagedChatSession) -> CodexAppServerClient | None:
        return session._app_client or self._client

    async def attach_session(
        self,
        session: CodexManagedChatSession,
        *,
        model: str | None = None,
    ) -> None:
        if model:
            self._apply_requested_model(session, model)

        await self.start()
        client = self._client_for(session)
        if client is None and self._client_factory is not None:
            client = self._client_factory()
            session._app_client = client
        if not self._health.available or client is None:
            raise RuntimeError(self._health.startup_error or "Codex backend unavailable")
        if not client.is_connected:
            try:
                await client.start()
            except Exception:
                session._app_client = None
                raise

        if (
            self._generation_endpoint is not None
            and self._generation_endpoint.wire_api == "chat-completions"
        ):
            local_endpoint = self._generation_endpoint
            if session._model:
                local_endpoint = local_endpoint.model_copy(update={"model": session._model})
            try:
                session._model = await ensure_local_model(local_endpoint, run_manager=None)
            except LocalModelError as exc:
                raise RuntimeError(local_model_preflight_message(local_endpoint, exc)) from exc

        if session._thread_id:
            thread = await client.resume_thread(session._thread_id)
        elif session.resume_session_id:
            thread = await client.resume_thread(session.resume_session_id)
        else:
            terminal_context = None
            if session.db_session_id:
                terminal_context = {
                    "gobby_session_id": session.db_session_id,
                    "gobby_web_chat_child": "1",
                }
            thread = await client.start_thread(
                cwd=session.project_path or ".",
                model=session._model,
                approval_policy=_CODEX_WEB_CHAT_APPROVAL_POLICY,
                sandbox=self.native_sandbox_pin(launch_sandbox_config(session)),
                terminal_context=terminal_context,
            )

        session._thread_id = thread.id
        session.sdk_session_id = thread.id
        session._transcript_path = getattr(thread, "path", None)
        session._connected = True
        session.last_activity = datetime.now(UTC)
        self._sessions_by_thread[thread.id] = session

    def _apply_requested_model(
        self,
        session: CodexManagedChatSession,
        requested_model: str,
    ) -> None:
        selector = parse_endpoint_model_selector(requested_model)
        if selector is None:
            if session._model_selector is not None:
                raise RuntimeError(
                    "Switching between a generation endpoint and native Codex "
                    "requires a new web-chat session"
                )
            session._model = requested_model
            return

        if self._generation_endpoint is None:
            raise RuntimeError(
                "Switching between native Codex and a generation endpoint "
                "requires a new web-chat session"
            )
        active_selector = parse_endpoint_model_selector(session._model_selector)
        if active_selector is not None and active_selector.endpoint_name != selector.endpoint_name:
            raise RuntimeError("Switching generation endpoints requires a new web-chat session")

        session._model_selector = requested_model
        session._model = selector.model or self._generation_endpoint.model

    async def detach_session(self, session: CodexManagedChatSession) -> None:
        session._connected = False
        session._turn_id = None
        if session._thread_id:
            self._sessions_by_thread.pop(session._thread_id, None)
        client = session._app_client
        session._app_client = None
        if client is not None and client is not self._client:
            try:
                await client.stop()
            except Exception:
                logger.debug("Codex session client stop failed", exc_info=True)

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
                elicitation_data.setdefault("server_name", server_name)
                elicitation_data.setdefault("tool_name", tool_name)
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
        config_runtime = getattr(session, "_config_runtime_ref", None)
        if config_runtime is None:
            return list(DEFAULT_GLOBAL_APPROVAL_RULES)
        return get_global_approval_rules(config_runtime.snapshot)

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
            plan_scratch_provider=session.provider if session.chat_mode == "plan" else None,
        )
        if out_of_repo_path:
            return self._decline_response(method)

        plan_mode_write_allowed = False
        if session.chat_mode == "plan":
            if tool_name in {"Write", "Edit", "NotebookEdit"}:
                plan_mode_write_allowed = are_plan_mode_write_paths_allowed(
                    tool_name,
                    input_data,
                    provider=session.provider,
                    project_path=session.project_path,
                )
                if not plan_mode_write_allowed:
                    return self._decline_response(method)
            elif (
                tool_name == "Bash"
                and _BASH_WRITE_PATTERNS.search(str(input_data.get("command", "")))
                and not is_gcode_shell_command(input_data)
            ):
                return self._decline_response(method)

        if plan_mode_write_allowed:
            return self._accept_response(method)

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
        request_parameters: Mapping[str, object] | None = None,
    ) -> AsyncIterator[ChatEvent]:
        client = self._client_for(session)
        if not self._health.available or client is None:
            raise RuntimeError(self._health.startup_error or "Codex backend unavailable")
        if not session._thread_id:
            raise RuntimeError("Codex session missing threadId")

        async for event in stream_codex_turn(
            client=client,
            session=session,
            prompt=prompt,
            context_prefix=context_prefix,
            request_parameters=request_parameters,
            extract_before_tool_dedup_key=self._extract_before_tool_dedup_key,
        ):
            yield event

    async def interrupt(self, session: CodexManagedChatSession) -> None:
        client = self._client_for(session)
        if not client or not session._thread_id or not session._turn_id:
            return
        await client.interrupt_turn(session._thread_id, session._turn_id)
        session._turn_id = None

    async def switch_model(self, session: CodexManagedChatSession, new_model: str) -> None:
        self._apply_requested_model(session, new_model)

    async def clear_session_context(self, session: CodexManagedChatSession) -> bool:
        """Reset the session's conversation context to a fresh Codex thread.

        Codex has no in-place context wipe, so a real "clear context" archives
        the current thread and starts a new one. The caller (plan approval with
        the "approve + clear context" option) re-seeds the approved plan into
        the next turn, so implementation continues on a clean thread.
        """
        client = self._client_for(session)
        if client is None or not self._health.available:
            logger.warning("Codex clear-context requested while backend unavailable")
            return False
        old_thread_id = session._thread_id
        try:
            await self.detach_session(session)
            session._reset_continuation_state()
            if old_thread_id:
                try:
                    await client.archive_thread(old_thread_id)
                except Exception:
                    logger.debug(
                        "Failed to archive Codex thread %s during context clear",
                        old_thread_id,
                        exc_info=True,
                    )
            requested_model = session._model_selector or session._model
            await self.attach_session(session, model=requested_model)
        except Exception:
            logger.exception(
                "Failed to clear Codex context for conversation=%s",
                session.conversation_id,
            )
            return False
        return True


__all__ = [
    "CodexManagedChatSession",
    "CodexWebChatBackend",
]
