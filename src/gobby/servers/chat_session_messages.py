"""Conversation history loading and response streaming for ChatSession."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.llm.context_windows import resolve_context_window
from gobby.llm.sdk_utils import (
    format_exception_group,
    sanitize_error,
)
from gobby.llm.sdk_utils import (
    parse_server_name as _parse_server_name,
)

logger = logging.getLogger(__name__)

_HISTORY_MESSAGE_LIMIT = 50
_HISTORY_WRAPPER_OVERHEAD_CHARS = 200
_DRAIN_PENDING_RESPONSE_TIMEOUT_SECONDS = 1.0
_EXPECTED_CHAT_ERRORS: tuple[type[Exception], ...] = (
    ClaudeSDKError,
    TimeoutError,
    OSError,
    psycopg.Error,
)


class ChatSessionMessagesMixin:
    """History injection and streaming response handling for ChatSession."""

    conversation_id: str
    db_session_id: str | None
    last_activity: datetime
    sdk_session_id: str | None
    _active_reasoning_effort: str | None
    _client: Any | None
    _connected: bool
    _context_window_overrides: dict[str, int]
    _last_model: str | None
    _lock: asyncio.Lock
    _max_history_message_chars: int
    _max_history_total_chars: int
    _message_manager: Any | None
    _message_manager_source_session_id: str | None
    _abort_pending_interactions: Callable[[], None]

    async def _load_history_context(self, max_total_chars: int | None = None) -> str | None:
        """Load prior conversation messages and format as context for injection.

        Args:
            max_total_chars: Override for maximum total characters. If None, uses
                the instance default (_max_history_total_chars). Callers should pass
                a budget that accounts for other additionalContext content to avoid
                Claude Code's 10K truncation limit.

        Returns a formatted string with conversation history, or None if
        no messages exist or an error occurs.
        """
        if not self._message_manager:
            return None
        target_id = self._message_manager_source_session_id or self.db_session_id
        if not target_id:
            return None

        try:
            messages = await self._message_manager.get_messages(
                target_id,
                limit=_HISTORY_MESSAGE_LIMIT,
            )
            if not messages:
                return None

            text_messages = [
                m
                for m in messages
                if m.get("role") in ("user", "assistant")
                and m.get("content_type") == "text"
                and m.get("content")
            ]
            if not text_messages:
                return None

            effective_max = (
                max_total_chars if max_total_chars is not None else self._max_history_total_chars
            )
            content_budget = effective_max - _HISTORY_WRAPPER_OVERHEAD_CHARS
            if content_budget <= 0:
                return None

            parts: list[str] = []
            total = 0
            session_ref = str(target_id)

            for index, message in enumerate(text_messages):
                role_label = "**User:**" if message["role"] == "user" else "**Assistant:**"
                content = str(message["content"])
                entry = f"{role_label} {content}"
                separator = 2 if parts else 0
                if total + separator + len(entry) <= content_budget:
                    parts.append(entry)
                    total += separator + len(entry)
                    continue

                if not parts:
                    pointer_entry = (
                        f"{role_label} [omitted {len(content)} chars; "
                        f"get_session_messages session_id={session_ref}]"
                    )
                    if len(pointer_entry) <= content_budget:
                        parts.append(pointer_entry)
                        total += len(pointer_entry)
                        continue
                    return None

                omitted = len(text_messages) - index
                omit_line = (
                    f"[omitted {omitted} messages to fit history budget; "
                    f"get_session_messages session_id={session_ref}]"
                )
                if total + separator + len(omit_line) <= content_budget:
                    parts.append(omit_line)
                break

            if not parts:
                return None

            return (
                "<conversation-history>\n"
                "The following is the prior conversation history for this session, "
                "restored after session recreation. Use it to maintain continuity.\n\n"
                + "\n\n".join(parts)
                + "\n</conversation-history>"
            )
        except _EXPECTED_CHAT_ERRORS as e:
            logger.warning(
                "Failed to load history context for %s: %s",
                self.conversation_id,
                e,
            )
            return None

    async def send_message(
        self,
        content: str | list[dict[str, Any]],
        *,
        request_parameters: Mapping[str, object] | None = None,
    ) -> AsyncIterator[ChatEvent]:
        """
        Send a user message and yield streaming events.

        Content can be a plain string or a list of content blocks
        (e.g. text + images in the standard Claude API format).

        Yields ChatEvent instances (TextChunk, ToolCallEvent,
        ToolResultEvent, DoneEvent) matching the existing protocol.
        """
        del request_parameters
        if not self._client or not self._connected:
            raise RuntimeError("ChatSession not connected. Call start() first.")

        async with self._lock:
            self.last_activity = datetime.now(UTC)
            session = cast(Any, self)
            if session._resolve_reasoning_effort() != self._active_reasoning_effort:
                await session._reconnect_for_reasoning_effort_change()

            tool_calls_count = 0
            needs_spacing_before_text = False
            has_text = False
            context_window: int | None = None
            # Track the LAST API call's input usage from message_start stream
            # events. ResultMessage.usage accumulates across ALL API calls in
            # the agentic loop, making total_input wildly exceed context_window
            # for tool-heavy turns. message_start gives per-call values.
            _last_call_input: dict[str, int] | None = None
            try:
                if isinstance(content, list):
                    # SDK streaming mode expects the transport protocol format:
                    # {"type": "user", "message": {"role": "user", "content": ...}}
                    # NOT just {"role": "user", "content": ...}
                    async def _content_blocks() -> AsyncIterator[dict[str, Any]]:
                        yield {
                            "type": "user",
                            "message": {"role": "user", "content": content},
                            "parent_tool_use_id": None,
                        }

                    await self._client.query(_content_blocks())
                else:
                    await self._client.query(content)

                async for message in self._client.receive_response():
                    if message is None:
                        continue
                    if isinstance(message, StreamEvent):
                        # Capture per-API-call input usage from message_start.
                        # Each API call in the agentic loop emits one; the last
                        # one reflects the actual current context window load.
                        ev = message.event
                        if isinstance(ev, dict) and ev.get("type") == "message_start":
                            msg_body = ev.get("message")
                            if isinstance(msg_body, dict):
                                u = msg_body.get("usage")
                                if isinstance(u, dict):
                                    _last_call_input = u
                        continue
                    if isinstance(message, ResultMessage):
                        if not self.sdk_session_id:
                            self.sdk_session_id = message.session_id
                        if message.result and not has_text:
                            yield TextChunk(content=message.result)
                        duration_ms = getattr(message, "duration_ms", None)
                        _raw_usage = getattr(message, "usage", None)
                        has_usage = isinstance(_raw_usage, dict)
                        if not has_usage:
                            logger.warning(
                                "ResultMessage missing usage for session %s",
                                self.conversation_id[:8],
                            )
                        usage: dict[str, Any] = (
                            cast(dict[str, Any], _raw_usage) if has_usage else {}
                        )

                        if _last_call_input:
                            uncached_input = _last_call_input.get("input_tokens", 0) or 0
                            cache_read = _last_call_input.get("cache_read_input_tokens", 0) or 0
                            cache_creation = (
                                _last_call_input.get("cache_creation_input_tokens", 0) or 0
                            )
                        else:
                            uncached_input = usage.get("input_tokens", 0) or 0
                            cache_read = usage.get("cache_read_input_tokens", 0) or 0
                            cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                        total_input = uncached_input + cache_read + cache_creation
                        output_tokens = usage.get("output_tokens", 0) or 0

                        context_window = resolve_context_window(
                            self._last_model,
                            None,
                            overrides=self._context_window_overrides or None,
                            provider="claude",
                        )

                        logger.info(
                            "DoneEvent: uncached=%s cache_read=%s cache_creation=%s "
                            "total_input=%s output=%s context_window=%s per_call=%s",
                            uncached_input,
                            cache_read,
                            cache_creation,
                            total_input,
                            output_tokens,
                            context_window,
                            _last_call_input is not None,
                        )
                        yield DoneEvent(
                            tool_calls_count=tool_calls_count,
                            duration_ms=duration_ms,
                            input_tokens=uncached_input if has_usage else None,
                            output_tokens=output_tokens if has_usage else None,
                            cache_read_input_tokens=cache_read if has_usage else None,
                            cache_creation_input_tokens=cache_creation if has_usage else None,
                            total_input_tokens=total_input if has_usage else None,
                            context_window=context_window,
                            sdk_session_id=self.sdk_session_id,
                        )

                    elif isinstance(message, AssistantMessage):
                        self._last_model = getattr(message, "model", None)
                        logger.debug("AssistantMessage model=%s", self._last_model)
                        for block in message.content:
                            if isinstance(block, ThinkingBlock):
                                yield ThinkingEvent(content=block.thinking)
                            elif isinstance(block, TextBlock):
                                has_text = True
                                text = block.text
                                if needs_spacing_before_text and text:
                                    text = text.lstrip("\n")
                                    if text:
                                        text = "\n\n" + text
                                yield TextChunk(content=text)
                                needs_spacing_before_text = False
                            elif isinstance(block, ToolUseBlock):
                                tool_calls_count += 1
                                server_name = _parse_server_name(block.name)
                                # Set spacing flag eagerly; denied tools may not
                                # produce ToolResultBlock before more text arrives.
                                needs_spacing_before_text = True
                                yield ToolCallEvent(
                                    tool_call_id=block.id,
                                    tool_name=block.name,
                                    server_name=server_name,
                                    arguments=block.input if isinstance(block.input, dict) else {},
                                )

                    elif isinstance(message, UserMessage):
                        if isinstance(message.content, list):
                            for block in message.content:
                                if isinstance(block, ToolResultBlock):
                                    is_error = getattr(block, "is_error", False)
                                    raw = block.content
                                    if isinstance(raw, str):
                                        content_str = raw
                                    elif isinstance(raw, list):
                                        parts = []
                                        for item in raw:
                                            item_text: str | None = getattr(item, "text", None)
                                            if item_text is not None:
                                                parts.append(item_text)
                                            else:
                                                parts.append(str(item))
                                        content_str = "\n".join(parts)
                                    else:
                                        content_str = str(raw) if raw is not None else ""
                                    yield ToolResultEvent(
                                        tool_call_id=block.tool_use_id,
                                        success=not is_error,
                                        result=content_str if not is_error else None,
                                        error=content_str if is_error else None,
                                    )
                                    needs_spacing_before_text = True

            except ExceptionGroup as eg:
                yield TextChunk(content=f"Generation failed: {format_exception_group(eg)}")
                if context_window is None:
                    context_window = self._resolve_context_window_fallback()
                yield DoneEvent(tool_calls_count=tool_calls_count, context_window=context_window)
            except Exception as e:
                logger.exception("ChatSession %s error: %s", self.conversation_id, e)
                yield TextChunk(content=f"Generation failed: {sanitize_error(e)}")
                if context_window is None:
                    context_window = self._resolve_context_window_fallback()
                yield DoneEvent(tool_calls_count=tool_calls_count, context_window=context_window)

    def _resolve_context_window_fallback(self) -> int | None:
        """Resolve context_window from _last_model for error paths."""
        return resolve_context_window(
            self._last_model,
            None,
            overrides=self._context_window_overrides or None,
            provider="claude",
        )

    async def interrupt(self) -> None:
        """Interrupt the current response stream."""
        self._abort_pending_interactions()
        if self._client and self._connected:
            try:
                await self._client.interrupt()
            except _EXPECTED_CHAT_ERRORS as e:
                logger.warning("ChatSession %s interrupt error: %s", self.conversation_id, e)

    async def drain_pending_response(self) -> None:
        """Drain any buffered response events from the SDK after an interrupt.

        After ``interrupt()`` + task cancellation, the SDK may still have
        stale response events in its internal buffer. If not consumed,
        those events leak into the next ``receive_response()`` call, causing
        the off-by-one bug where the response to message N+1 actually
        contains content generated for message N.
        """
        if not self._client or not self._connected:
            return
        try:
            async with asyncio.timeout(_DRAIN_PENDING_RESPONSE_TIMEOUT_SECONDS):
                async for _ in self._client.receive_response():
                    pass
        except TimeoutError:
            logger.debug("ChatSession %s: drain timed out (no stale events)", self.conversation_id)
        except _EXPECTED_CHAT_ERRORS as e:
            logger.debug("ChatSession %s: drain error (expected): %s", self.conversation_id, e)
