"""Shared managed session wrapper for ACP web-chat providers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.adapters.acp_client import StreamEvent
from gobby.adapters.acp_client_requests import is_pre_tool_decision_denied
from gobby.adapters.acp_commands import normalize_available_commands
from gobby.adapters.acp_content import normalize_prompt_blocks
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    SessionAvailableCommandsEvent,
    SessionInfoUpdateEvent,
    SessionModeUpdateEvent,
    SessionUsageUpdateEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.chat_session_helpers import PendingApproval, build_compaction_context
from gobby.servers.websocket.chat.backends.base import (
    ManagedChatSessionBase,
    _extract_text,
    _log_upstream_error_event,
)
from gobby.servers.websocket.chat.permissions import ManagedWebChatPermissionsMixin

logger = logging.getLogger(__name__)

_ACP_MODE_TO_GOBBY_MODE = {
    "plan": "plan",
    "act": "normal",
    "normal": "normal",
    "accept_edits": "normal",
    "yolo": "bypass",
    "bypass": "bypass",
}


@dataclass
class ACPManagedChatSession(
    ManagedWebChatPermissionsMixin,
    ManagedChatSessionBase,
):
    """Web-chat session backed by a shared ACP backend."""

    provider: str = field(default="acp", init=False)
    chat_mode: str = field(default="plan")
    _pending_question: dict[str, Any] | None = field(default=None, repr=False)
    _pending_answer_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_answers: dict[str, Any] | None = field(default=None, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _pending_approval_events: dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _pending_approval_decisions: dict[str, str] = field(default_factory=dict, repr=False)
    _plan_approved: bool = field(default=False, repr=False)
    _plan_feedback: str | None = field(default=None, repr=False)
    _is_first_turn: bool = field(default=True, repr=False)
    available_commands: list[dict[str, Any]] = field(default_factory=list)
    _acp_client: Any = field(default=None, repr=False)

    def _web_chat_source(self) -> str:
        return f"{self.provider}_web_chat"

    def _provider_label(self) -> str:
        return self.provider.capitalize()

    def _tool_name_adapter(self) -> Any:
        return None

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

            prompt = _extract_text(content) if isinstance(content, list) else content
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
                resp = await self._on_before_agent(
                    {
                        "prompt": prompt,
                        "source": self._web_chat_source(),
                    }
                )
                if resp and resp.get("context"):
                    context_parts.append(str(resp["context"]))

            prompt_payload = normalize_prompt_blocks(
                content,
                agent_capabilities=getattr(self._backend, "agent_capabilities", {}),
                prefix_text="\n\n".join(context_parts) if context_parts else None,
            )

            self._is_first_turn = False
            saw_content_delta = False
            pending_tool_calls: dict[str, dict[str, Any]] = {}
            blocked_tool_call_ids: set[str] = set()
            plan_text_parts: list[str] = []

            try:
                async for stream_event in self._backend.send_message(self, prompt_payload):
                    if stream_event.event_type == "init":
                        self.sdk_session_id = (
                            stream_event.data.get("session_id")
                            or stream_event.data.get("sessionId")
                            or self.sdk_session_id
                        )
                        model_name = stream_event.data.get("model")
                        if isinstance(model_name, str) and model_name:
                            self._model = model_name
                    elif stream_event.event_type == "plan_update":
                        plan_text = _format_plan_update(stream_event.data)
                        await self._maybe_broadcast_pending_plan(
                            plan_text,
                            bool(plan_text),
                            structured=True,
                        )
                        continue
                    elif stream_event.event_type == "session_info_update":
                        session_info = _session_info_update_payload(stream_event.data)
                        self.last_activity = _updated_at_or_now(session_info.get("updatedAt"))
                        yield SessionInfoUpdateEvent(session_info=session_info)
                        continue
                    elif stream_event.event_type == "current_mode_update":
                        current_mode_id = stream_event.data.get("current_mode_id")
                        if isinstance(current_mode_id, str) and current_mode_id:
                            chat_mode = _map_acp_mode_to_gobby_mode(current_mode_id)
                            if chat_mode is not None:
                                self.set_chat_mode(chat_mode)
                            yield SessionModeUpdateEvent(
                                current_mode_id=current_mode_id,
                                chat_mode=chat_mode,
                            )
                        continue
                    elif stream_event.event_type == "usage_update":
                        yield SessionUsageUpdateEvent(
                            usage=_normalize_usage_update(stream_event.data)
                        )
                        continue
                    elif stream_event.event_type == "available_commands_update":
                        self.available_commands = normalize_available_commands(
                            stream_event.data.get("commands")
                        )
                        yield SessionAvailableCommandsEvent(
                            available_commands=self.available_commands
                        )
                        continue
                    elif stream_event.event_type == "content_delta":
                        saw_content_delta = True

                    if stream_event.event_type in {"tool_call", "call_tool"}:
                        chat_event = self._translate_event(
                            stream_event,
                            allow_message_fallback=not saw_content_delta,
                        )
                        if isinstance(chat_event, ToolCallEvent):
                            if not stream_event.data.get("is_update"):
                                pre_tool_response = None
                                if stream_event.data.get("pre_tool_checked"):
                                    pre_tool_response = stream_event.data.get("pre_tool_response")
                                else:
                                    pre_tool_response = await self._apply_pre_tool_lifecycle(
                                        chat_event.tool_name,
                                        chat_event.arguments,
                                    )
                                if is_pre_tool_decision_denied(pre_tool_response):
                                    blocked_tool_call_ids.add(chat_event.tool_call_id)
                                    continue
                                pending_tool_calls[chat_event.tool_call_id] = {
                                    "tool_name": chat_event.tool_name,
                                    "tool_input": chat_event.arguments,
                                }
                        if chat_event is not None:
                            yield chat_event
                        continue

                    if stream_event.event_type == "tool_result":
                        chat_event = self._translate_event(
                            stream_event,
                            allow_message_fallback=not saw_content_delta,
                        )
                        if isinstance(chat_event, ToolResultEvent):
                            if chat_event.tool_call_id in blocked_tool_call_ids:
                                blocked_tool_call_ids.remove(chat_event.tool_call_id)
                                continue
                            pending = pending_tool_calls.pop(chat_event.tool_call_id, None)
                            if pending is not None:
                                tool_input = pending.get("tool_input")
                                tool_input_payload: dict[str, Any] = (
                                    tool_input if isinstance(tool_input, dict) else {}
                                )
                                await self._apply_post_tool_lifecycle(
                                    str(pending.get("tool_name", "")),
                                    tool_input_payload,
                                    chat_event.result if chat_event.success else chat_event.error,
                                )
                        if chat_event is not None:
                            yield chat_event
                        continue

                    chat_event = self._translate_event(
                        stream_event,
                        allow_message_fallback=not saw_content_delta,
                    )
                    if chat_event is not None:
                        if isinstance(chat_event, TextChunk) and stream_event.event_type in {
                            "content_delta",
                            "message",
                        }:
                            plan_text_parts.append(chat_event.content)
                        yield chat_event

                await self._maybe_broadcast_pending_plan(
                    "".join(plan_text_parts), saw_content_delta
                )

                yield DoneEvent(
                    tool_calls_count=0,
                    sdk_session_id=self.sdk_session_id,
                    context_window=self._resolve_context_window(),
                )
            except OSError as exc:
                logger.exception(
                    "%s managed session %s error: %s",
                    self._provider_label(),
                    self.conversation_id,
                    exc,
                )
                yield TextChunk(content=f"Generation failed: {exc}")
                yield DoneEvent(tool_calls_count=0, context_window=self._resolve_context_window())

    def _translate_event(
        self,
        event: StreamEvent,
        *,
        allow_message_fallback: bool = True,
    ) -> ChatEvent | None:
        if event.event_type == "content_delta":
            content = event.data.get("content", "")
            content_blocks = event.data.get("content_blocks")
            if content or content_blocks:
                return TextChunk(
                    content=content,
                    content_blocks=content_blocks if isinstance(content_blocks, list) else None,
                )
            return None

        if event.event_type == "message" and allow_message_fallback:
            role = event.data.get("role", "")
            content = event.data.get("content", "")
            if role == "assistant" and content:
                return TextChunk(content=content)
            return None

        if event.event_type == "thinking_delta":
            content = event.data.get("content", "")
            if content:
                return ThinkingEvent(content=content)
            return None

        if event.event_type in {"tool_call", "call_tool"}:
            tool_name = event.data.get("tool_name") or event.data.get("name")
            if not tool_name:
                return None

            adapter = self._tool_name_adapter()
            if callable(adapter):
                tool_name = adapter(str(tool_name))
            elif adapter is not None:
                tool_name = adapter.normalize_tool_name(tool_name)

            tool_input = event.data.get("tool_input") or event.data.get("arguments") or {}
            mcp_server = event.data.get("mcp_server") or event.data.get("server_name")
            call_id = event.data.get("call_id") or event.data.get("id") or "unknown"

            return ToolCallEvent(
                tool_call_id=str(call_id),
                tool_name=str(tool_name),
                server_name=mcp_server or self.provider,
                arguments=tool_input,
                status=event.data.get("tool_status"),
                tool_kind=event.data.get("tool_kind"),
                locations=event.data.get("locations"),
                content_blocks=event.data.get("content_blocks"),
                raw_output=event.data.get("raw_output"),
            )

        if event.event_type == "tool_result":
            call_id = event.data.get("call_id") or event.data.get("id") or "unknown"
            success = event.data.get("success", True)
            result = event.data.get("result") or event.data.get("output")
            error = event.data.get("error")

            return ToolResultEvent(
                tool_call_id=str(call_id),
                success=success,
                result=result,
                error=error,
                locations=event.data.get("locations"),
                content_blocks=event.data.get("content_blocks"),
                raw_output=event.data.get("raw_output"),
            )

        if event.event_type == "error":
            _log_upstream_error_event(self.provider, self, event.data)
            message = event.data.get("message", "Unknown error")
            return TextChunk(content=f"Error: {message}")

        return None

    async def interrupt(self) -> None:
        self.cancel_pending_approval()
        await self._backend.interrupt(self)
        logger.debug("%s interrupt requested for %s", self._provider_label(), self.conversation_id)


def _map_acp_mode_to_gobby_mode(mode_id: str) -> str | None:
    return _ACP_MODE_TO_GOBBY_MODE.get(mode_id.strip().lower())


def _format_plan_update(update: dict[str, Any]) -> str:
    entries = update.get("entries")
    if isinstance(entries, list):
        lines: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            status = entry.get("status")
            if isinstance(status, str) and status:
                lines.append(f"- [{status}] {content.strip()}")
            else:
                lines.append(f"- {content.strip()}")
        return "\n".join(lines)

    content = update.get("content") or update.get("plan")
    return content.strip() if isinstance(content, str) else ""


def _session_info_update_payload(update: dict[str, Any]) -> dict[str, Any]:
    session_info = update.get("session_info")
    if not isinstance(session_info, dict):
        session_info = {}
    payload = dict(session_info)
    for key in ("title", "updatedAt"):
        if key in update:
            payload[key] = update[key]
    return payload


def _updated_at_or_now(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            logger.debug("Ignoring invalid ACP updatedAt value: %r", value)
    return datetime.now(UTC)


def _normalize_usage_update(update: dict[str, Any]) -> dict[str, Any]:
    context_window = _nonnegative_int(update.get("size"))
    context_used_tokens = _nonnegative_int(update.get("used"))
    context_usage_ratio: float | None = None
    if context_window and context_used_tokens is not None:
        context_usage_ratio = min(1.0, context_used_tokens / context_window)

    payload: dict[str, Any] = {
        "context_window": context_window,
        "context_used_tokens": context_used_tokens,
        "context_usage_ratio": context_usage_ratio,
        "context_usage_source": "acp",
        "context_usage_confidence": "reported",
    }
    cost = update.get("cost")
    if isinstance(cost, dict):
        payload["cost"] = dict(cost)
    return payload


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


__all__ = ["ACPManagedChatSession"]
