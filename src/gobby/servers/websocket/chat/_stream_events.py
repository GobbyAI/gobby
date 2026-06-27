"""Stream event handling for chat responses."""

from __future__ import annotations

import logging
from collections.abc import Sized
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from gobby.llm.claude_models import (
    DoneEvent,
    SessionInfoUpdateEvent,
    SessionModeUpdateEvent,
    SessionUsageUpdateEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.websocket.chat._stream_persistence import ChatStreamPersistence
from gobby.servers.websocket.chat._stream_transport import ChatStreamTransport
from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks

logger = logging.getLogger(__name__)
EXPECTED_TTS_ERRORS: tuple[type[Exception], ...] = (ValueError, RuntimeError, OSError)


@dataclass
class ChatStreamEventState:
    """Mutable per-stream event state."""

    assistant_message_id: str
    accumulated_text: str = ""
    after_tool_call: bool = False
    has_sent_text: bool = False
    pending_approval_id: str | None = None
    pending_tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Tool results whose ToolCallEvent has not arrived yet (out-of-order ACP
    # delivery). Buffered here and reconciled once the matching call lands so the UI
    # shows the real tool name instead of "unknown". Example lifecycle:
    # ToolResultEvent(call-1) -> buffer, ToolCallEvent(call-1) -> emit calling,
    # then apply the buffered result and remove it. If the stream ends first,
    # _flush_orphan_tool_results synthesizes an "unknown" call before completion.
    orphan_tool_results: dict[str, ToolResultEvent] = field(default_factory=dict)


class ChatStreamEventHandler:
    """Convert backend stream events into UI frames and persisted blocks."""

    def __init__(
        self,
        owner: Any,
        conversation_id: str,
        transport: ChatStreamTransport,
        persistence: ChatStreamPersistence,
        assistant_blocks: AssistantContentBlocks,
        state: ChatStreamEventState,
        tts_pipeline: Any,
    ) -> None:
        self.owner = owner
        self.conversation_id = conversation_id
        self.transport = transport
        self.persistence = persistence
        self.assistant_blocks = assistant_blocks
        self.state = state
        self.tts_pipeline = tts_pipeline

    async def emit_pending_approval(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """Emit pending_approval tool_status to the client."""
        approval_id = f"approval-{uuid4().hex[:8]}"
        self.state.pending_approval_id = approval_id
        await self.transport.safe_send(
            self._msg(
                type="tool_status",
                tool_call_id=approval_id,
                status="pending_approval",
                tool_name=tool_name,
                arguments=arguments,
            )
        )

    async def handle_event(self, event: Any, session: Any) -> bool:
        """Handle one backend event. Return False to stop streaming."""
        if isinstance(event, ThinkingEvent):
            return await self._handle_thinking(event)
        if isinstance(event, TextChunk):
            return await self._handle_text(event, session)
        if isinstance(event, ToolCallEvent):
            return await self._handle_tool_call(event)
        if isinstance(event, ToolResultEvent):
            return await self._handle_tool_result(event)
        if isinstance(event, SessionInfoUpdateEvent):
            return await self._handle_session_info_update(event, session)
        if isinstance(event, SessionModeUpdateEvent):
            return await self._handle_session_mode_update(event)
        if isinstance(event, SessionUsageUpdateEvent):
            return await self._handle_session_usage_update(event, session)
        if isinstance(event, DoneEvent):
            return await self._handle_done(event, session)
        return True

    def _msg(self, **fields: Any) -> dict[str, Any]:
        """Build a request-correlated frame for the current assistant message."""
        return self.transport.base_msg(
            message_id=self.state.assistant_message_id,
            conversation_id=self.conversation_id,
            **fields,
        )

    async def _handle_thinking(self, event: ThinkingEvent) -> bool:
        self.assistant_blocks.append_thinking(event.content)
        return await self.transport.safe_send(
            self._msg(type="chat_thinking", content=event.content)
        )

    async def _handle_session_info_update(
        self,
        event: SessionInfoUpdateEvent,
        session: Any,
    ) -> bool:
        info = event.session_info
        payload = self._msg(type="session_info")
        db_session_id = getattr(session, "db_session_id", None)
        if isinstance(db_session_id, str) and db_session_id:
            payload["db_session_id"] = db_session_id
        seq_num = getattr(session, "seq_num", None)
        if isinstance(seq_num, int) and seq_num > 0:
            payload["session_ref"] = f"#{seq_num}"

        if "title" in info:
            title = info.get("title")
            if isinstance(title, str) or title is None:
                payload["title"] = title
                payload["session_title"] = title

        updated_at = info.get("updatedAt")
        if isinstance(updated_at, str) and updated_at:
            payload["updated_at"] = updated_at

        return await self.transport.safe_send(payload)

    async def _handle_session_mode_update(self, event: SessionModeUpdateEvent) -> bool:
        if event.chat_mode is None:
            return True
        return await self.transport.safe_send(
            self._msg(
                type="mode_changed",
                mode=event.chat_mode,
                reason="acp_current_mode_update",
                provider_current_mode_id=event.current_mode_id,
            )
        )

    async def _handle_session_usage_update(
        self,
        event: SessionUsageUpdateEvent,
        session: Any,
    ) -> bool:
        session_id = getattr(session, "db_session_id", None)
        if not isinstance(session_id, str) or not session_id:
            session_id = self.conversation_id
        payload = self._msg(
            type="session_usage_updated",
            session_id=session_id,
            updated_at=datetime.now(UTC).isoformat(),
            **event.usage,
        )
        project_id = getattr(session, "project_id", None)
        if isinstance(project_id, str) and project_id:
            payload["project_id"] = project_id
        model = getattr(session, "model", None)
        if isinstance(model, str) and model:
            payload["model"] = model
        return await self.transport.safe_send(payload)

    async def _handle_text(self, event: TextChunk, session: Any) -> bool:
        content = event.content
        session_obj = self.owner._chat_sessions.get(self.conversation_id)
        if session_obj and getattr(session_obj, "_plan_approval_completed", False):
            session_obj._plan_approval_completed = False
            if self.assistant_blocks.has_content():
                await self.persistence.persist_current_assistant(session)
                self.state.accumulated_text = ""
            self.state.assistant_message_id = f"assistant-{uuid4().hex[:12]}"
            self.state.after_tool_call = False
            self.state.has_sent_text = False
        elif self.state.after_tool_call:
            self.state.after_tool_call = False
            if self.state.has_sent_text:
                content = "\n\n" + content

        if content.strip():
            self.state.has_sent_text = True
        self.state.accumulated_text += content
        self.assistant_blocks.append_text(content)
        if not await self.transport.safe_send(
            self._msg(type="chat_stream", content=content, done=False)
        ):
            return False

        if self.tts_pipeline and content.strip():
            try:
                self.tts_pipeline.feed_text(content)
            except EXPECTED_TTS_ERRORS:
                logger.warning(
                    "TTS feed_text failed",
                    extra={
                        "conversation_id": self.conversation_id,
                        "content_length": len(content),
                    },
                    exc_info=True,
                )
            except Exception:
                logger.error(
                    "Unexpected TTS feed_text failure",
                    extra={"conversation_id": self.conversation_id},
                    exc_info=True,
                )
                raise
        return True

    async def _handle_tool_call(self, event: ToolCallEvent) -> bool:
        if self.state.pending_approval_id is not None:
            await self.transport.safe_send(
                self._msg(
                    type="tool_status",
                    tool_call_id=self.state.pending_approval_id,
                    status="calling",
                    tool_name=event.tool_name,
                    server_name=event.server_name,
                    arguments=event.arguments,
                )
            )
            self.state.pending_approval_id = None

        self.state.pending_tool_calls[event.tool_call_id] = {
            "tool_name": event.tool_name,
            "arguments": event.arguments,
        }
        self.assistant_blocks.append_tool_call(
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
            server_name=event.server_name,
            arguments=event.arguments,
        )
        sent = await self.transport.safe_send(
            self._msg(
                type="tool_status",
                tool_call_id=event.tool_call_id,
                status="calling",
                tool_name=event.tool_name,
                server_name=event.server_name,
                arguments=event.arguments,
            )
        )

        # The result may have arrived before this call (out-of-order ACP
        # delivery). Reconcile it now that the tool name is known so the UI
        # transitions calling -> completed in order with the real name.
        orphan = self.state.orphan_tool_results.get(event.tool_call_id)
        if orphan is not None and sent:
            applied = await self._apply_tool_result(orphan)
            if applied:
                self.state.orphan_tool_results.pop(event.tool_call_id, None)
                self.state.pending_tool_calls.pop(event.tool_call_id, None)
            return applied
        return sent

    async def _handle_tool_result(self, event: ToolResultEvent) -> bool:
        self.state.after_tool_call = True
        pending = self.state.pending_tool_calls.pop(event.tool_call_id, {})
        if not pending:
            # Out-of-order ACP delivery: the result beat its ToolCallEvent.
            # Buffer it so _handle_tool_call can reconcile against the real tool
            # name (emitting calling -> completed in order) rather than sending
            # an orphan "completed" the UI renders as an "unknown" tool.
            self.state.orphan_tool_results[event.tool_call_id] = event
            logger.info(
                "Buffered ToolResultEvent for %s pending its ToolCallEvent",
                event.tool_call_id,
            )
            return True
        return await self._apply_tool_result(event)

    async def _apply_tool_result(self, event: ToolResultEvent) -> bool:
        """Complete the matching tool-call block and broadcast its terminal status."""
        self.assistant_blocks.complete_tool_call(
            tool_call_id=event.tool_call_id,
            success=event.success,
            result=event.result,
            error=event.error,
        )
        return await self.transport.safe_send(
            self._msg(
                type="tool_status",
                tool_call_id=event.tool_call_id,
                status="completed" if event.success else "error",
                result=event.result,
                error=event.error,
            )
        )

    async def _flush_orphan_tool_results(self) -> None:
        """Emit buffered results whose ToolCallEvent never arrived.

        Out-of-order delivery is normally reconciled in ``_handle_tool_call``.
        If the matching call never lands before the stream ends, surface the
        result anyway by synthesizing a provisional tool call so the work is
        still rendered and persisted; the name is "unknown" only because the
        backend never emitted the call event.
        """
        if not self.state.orphan_tool_results:
            return
        for call_id, result in list(self.state.orphan_tool_results.items()):
            logger.info(
                "Flushing orphan ToolResultEvent as unknown tool call",
                extra={
                    "call_id": call_id,
                    "tool_name": "unknown",
                    "server_name": "unknown",
                    "success": result.success,
                    "result_type": type(result.result).__name__
                    if result.result is not None
                    else None,
                    "result_length": _safe_len(result.result),
                    "has_error": result.error is not None,
                },
            )
            self.assistant_blocks.append_tool_call(
                tool_call_id=call_id,
                tool_name="unknown",
                server_name="unknown",
                arguments={},
            )
            sent = await self.transport.safe_send(
                self._msg(
                    type="tool_status",
                    tool_call_id=call_id,
                    status="calling",
                    tool_name="unknown",
                    server_name="unknown",
                    arguments={},
                )
            )
            if not sent:
                continue
            if await self._apply_tool_result(result):
                self.state.orphan_tool_results.pop(call_id, None)

    async def _handle_done(self, event: DoneEvent, session: Any) -> bool:
        if self.tts_pipeline:
            try:
                await self.tts_pipeline.flush()
            except EXPECTED_TTS_ERRORS:
                logger.warning(
                    "TTS flush failed",
                    extra={"conversation_id": self.conversation_id},
                    exc_info=True,
                )
            except Exception:
                logger.error(
                    "Unexpected TTS flush failure",
                    extra={"conversation_id": self.conversation_id},
                    exc_info=True,
                )
                raise

        await self._flush_orphan_tool_results()
        await self.persistence.persist_current_assistant(session)

        done_msg = self._msg(
            type="chat_stream",
            content="",
            done=True,
            tool_calls_count=event.tool_calls_count,
        )
        ref = self.persistence.session_ref()
        if ref:
            done_msg["session_ref"] = ref
        if event.total_input_tokens is not None or event.input_tokens is not None:
            done_msg["usage"] = {
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "cache_read_input_tokens": event.cache_read_input_tokens,
                "cache_creation_input_tokens": event.cache_creation_input_tokens,
                "total_input_tokens": event.total_input_tokens,
            }
        if event.context_window is not None:
            done_msg["context_window"] = event.context_window

        logger.info(
            "DoneEvent context_window=%s total_input=%s "
            "(uncached=%s cache_read=%s cache_creation=%s) output=%s",
            event.context_window,
            event.total_input_tokens,
            event.input_tokens,
            event.cache_read_input_tokens,
            event.cache_creation_input_tokens,
            event.output_tokens,
        )

        sdk_sid = event.sdk_session_id
        if sdk_sid:
            done_msg["sdk_session_id"] = sdk_sid
        await self.persistence.persist_sdk_session_id(session, sdk_sid)
        await self.transport.safe_send(done_msg)
        await self.persistence.persist_done_metadata(session, event)
        return True


def _safe_len(value: Any) -> int | None:
    if not isinstance(value, Sized):
        return None
    try:
        return len(value)
    except (TypeError, ValueError, RuntimeError):
        return None
