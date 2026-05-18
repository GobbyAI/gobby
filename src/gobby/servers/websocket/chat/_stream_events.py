"""Stream event handling for chat responses."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from gobby.llm.claude_models import (
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.websocket.chat._stream_persistence import ChatStreamPersistence
from gobby.servers.websocket.chat._stream_transport import ChatStreamTransport
from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks

logger = logging.getLogger("gobby.servers.websocket.chat._messaging")


@dataclass
class ChatStreamEventState:
    """Mutable per-stream event state."""

    assistant_message_id: str
    accumulated_text: str = ""
    after_tool_call: bool = False
    has_sent_text: bool = False
    pending_approval_id: str | None = None
    pending_tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)


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
            except (ValueError, RuntimeError):
                logger.warning("TTS feed_text failed", exc_info=True)
            except Exception:
                logger.warning("Unexpected TTS feed_text failure", exc_info=True)
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
        return await self.transport.safe_send(
            self._msg(
                type="tool_status",
                tool_call_id=event.tool_call_id,
                status="calling",
                tool_name=event.tool_name,
                server_name=event.server_name,
                arguments=event.arguments,
            )
        )

    async def _handle_tool_result(self, event: ToolResultEvent) -> bool:
        self.state.after_tool_call = True
        pending = self.state.pending_tool_calls.pop(event.tool_call_id, {})
        if not pending:
            logger.warning(
                f"ToolResultEvent for {event.tool_call_id} arrived before ToolCallEvent "
                "(tool_name will be 'unknown')",
            )
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

    async def _handle_done(self, event: DoneEvent, session: Any) -> bool:
        if self.tts_pipeline:
            try:
                await self.tts_pipeline.flush()
            except Exception:
                logger.debug("TTS flush failed", exc_info=True)

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
