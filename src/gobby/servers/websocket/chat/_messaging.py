"""Chat message handling and streaming mixin."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.hooks.events import HookEvent, HookEventType
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat._session import _resolve_git_branch
from gobby.sessions.transcripts.base import ParsedMessage

logger = logging.getLogger(__name__)


class ChatMessagingMixin:
    """Message processing methods for ChatMixin."""

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, ChatSession]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_modes: dict[str, str]
    _pending_worktree_paths: dict[str, str]
    _pending_agents: dict[str, str]

    if TYPE_CHECKING:

        async def _create_chat_session(
            self,
            conversation_id: str,
            model: str | None = None,
            project_id: str | None = None,
            resume_session_id: str | None = None,
        ) -> ChatSession: ...

        async def _send_error(
            self,
            websocket: Any,
            message: str,
            request_id: str | None = None,
            code: str = "ERROR",
        ) -> None: ...

        async def broadcast_session_event(
            self,
            event: str,
            session_id: str,
            **kwargs: Any,
        ) -> None: ...

        async def _fire_lifecycle(
            self,
            conversation_id: str,
            event_type: HookEventType,
            data: dict[str, Any],
        ) -> dict[str, Any] | None: ...

        async def _cancel_active_chat(self, conversation_id: str) -> None: ...

        async def _evaluate_blocking_webhooks(
            self,
            event: HookEvent,
        ) -> dict[str, Any] | None: ...

    def _inject_pending_messages(
        self,
        db_session_id: str,
        event_type: HookEventType,
    ) -> str | None:
        """Check for and inject undelivered inter-session messages.

        Runs on BEFORE_TOOL, AFTER_TOOL, and BEFORE_AGENT to match the CLI
        path's EventEnricher piggyback behavior. BEFORE_AGENT ensures messages
        arrive at agent turn start, even before any tool calls.
        """
        _PIGGYBACK_EVENTS = {
            HookEventType.BEFORE_TOOL,
            HookEventType.AFTER_TOOL,
            HookEventType.BEFORE_AGENT,
        }
        if event_type not in _PIGGYBACK_EVENTS:
            return None

        inter_session_msg_manager = getattr(self, "inter_session_msg_manager", None)
        if not inter_session_msg_manager:
            return None

        try:
            undelivered = inter_session_msg_manager.get_undelivered_messages(db_session_id)
            if not undelivered:
                return None

            # Group by message_type
            groups: dict[str, list[Any]] = {}
            for msg in undelivered:
                msg_type = getattr(msg, "message_type", "message") or "message"
                groups.setdefault(msg_type, []).append(msg)
                try:
                    inter_session_msg_manager.mark_delivered(msg.id)
                except Exception:
                    pass

            # Format each group
            sections: list[str] = []
            for msg_type, msgs in groups.items():
                header = self._message_group_header(msg_type)
                lines = [header]
                for msg in msgs:
                    urgent = "[URGENT] " if getattr(msg, "priority", "normal") == "urgent" else ""
                    sender = self._resolve_chat_sender(getattr(msg, "from_session", None))
                    lines.append(f"- {urgent}{sender}{msg.content}")
                sections.append("\n".join(lines))

            return "\n\n".join(sections)
        except Exception as exc:
            logger.debug("Inter-session message piggyback failed: %s", exc)
            return None

    @staticmethod
    def _message_group_header(message_type: str) -> str:
        """Return the context header for a message type group."""
        if message_type == "web_chat":
            return "[Pending messages from web chat user]:"
        if message_type == "command_result":
            return "[Pending command results]:"
        return "[Pending P2P messages from other sessions]:"

    @staticmethod
    def _resolve_chat_sender(from_session: str | None) -> str:
        """Resolve sender label using truncated UUID (no session storage in chat path)."""
        if not from_session:
            return ""
        return f"Session {from_session[:8]}: "

    @staticmethod
    def _classify_chat_error(exc: Exception) -> tuple[str, str]:
        """Return (user_message, error_code) for a chat exception."""
        msg = str(exc).lower()
        if "rate_limit" in msg or "429" in msg:
            return "Rate limited by Claude API. Please wait and try again.", "RATE_LIMITED"
        if "auth" in msg or "401" in msg or "403" in msg or "api_key" in msg:
            return "Authentication error with Claude API.", "AUTH_ERROR"
        if isinstance(exc, TimeoutError) or "timeout" in msg:
            return "Request timed out. Please try again.", "TIMEOUT"
        if "connection" in msg:
            return "Connection to Claude API failed. Please try again.", "CONNECTION_ERROR"
        exc_type = type(exc).__name__
        return f"An error occurred ({exc_type}). Check daemon logs for details.", "INTERNAL_ERROR"

    async def _handle_chat_message(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle chat_message using a persistent ClaudeSDKClient-backed ChatSession.

        Sessions are keyed by conversation_id (stable across reconnections).
        Each session maintains full multi-turn context including tool calls.

        Message format:
        {
            "type": "chat_message",
            "content": "user message",
            "message_id": "client-generated-id",
            "conversation_id": "optional-stable-id",
            "model": "optional-model-override",
            "request_id": "client-uuid-for-stream-correlation"
        }

        Response format (streamed):
        {
            "type": "chat_stream",
            "message_id": "assistant-uuid",
            "conversation_id": "stable-id",
            "request_id": "echoed-client-uuid",
            "content": "chunk of text",
            "done": false
        }

        Tool status format:
        {
            "type": "tool_status",
            "message_id": "assistant-uuid",
            "conversation_id": "stable-id",
            "tool_call_id": "unique-id",
            "status": "calling" | "completed" | "error",
            "tool_name": "mcp__gobby-tasks__create_task",
            "server_name": "gobby-tasks",
            "arguments": {...},
            "result": {...},
            "error": "..."
        }

        Args:
            websocket: Client WebSocket connection
            data: Parsed chat message
        """
        content: str | list[dict[str, Any]] = data.get("content", "")
        content_blocks = data.get("content_blocks")
        conversation_id = data.get("conversation_id") or str(uuid4())
        model = data.get("model")
        request_id = data.get("request_id", "")
        project_id = data.get("project_id")

        # Use content_blocks (multimodal) if provided, otherwise plain text
        if content_blocks and isinstance(content_blocks, list):
            content = content_blocks
        elif not content or not isinstance(content, str) or not content.strip():
            await self._send_error(websocket, "Missing or invalid 'content' field")
            return

        client_info = self.clients.get(websocket)
        if not client_info:
            logger.warning("Chat message from unregistered client")
            return

        # Track which conversation this client is in (for scoped broadcasts)
        client_info["conversation_id"] = conversation_id

        # Extract inject_context for tool result injection into LLM conversation
        inject_context = data.get("inject_context")

        # Cancel any active stream for this conversation
        await self._cancel_active_chat(conversation_id)

        # Run streaming as a cancellable task
        task = asyncio.create_task(
            self._stream_chat_response(
                websocket,
                conversation_id,
                content,
                model,
                request_id,
                project_id,
                inject_context=inject_context,
            )
        )
        task.add_done_callback(self._on_chat_task_done)
        self._active_chat_tasks[conversation_id] = task

    def _on_chat_task_done(self, task: asyncio.Task[None]) -> None:
        """Log unhandled exceptions from chat tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Unhandled exception in chat task", exc_info=exc)

    async def _stream_chat_response(
        self,
        websocket: Any,
        conversation_id: str,
        content: str | list[dict[str, Any]],
        model: str | None,
        request_id: str = "",
        project_id: str | None = None,
        inject_context: str | None = None,
    ) -> None:
        """Stream a ChatSession response to the client. Runs as a cancellable task."""
        from gobby.llm.claude_models import (
            DoneEvent,
            TextChunk,
            ThinkingEvent,
            ToolCallEvent,
            ToolResultEvent,
        )

        assistant_message_id = f"assistant-{uuid4().hex[:12]}"
        accumulated_text = ""
        after_tool_call = False  # Track tool→text transitions to prevent sentence collisions
        has_sent_text = False  # Survives accumulated_text flushes for separator injection
        ws_connected = True

        def _base_msg(**fields: Any) -> dict[str, Any]:
            """Build a response dict, always including request_id for stream correlation."""
            msg: dict[str, Any] = fields
            msg["request_id"] = request_id
            return msg

        async def _safe_send(msg: dict[str, Any]) -> bool:
            """Send a message to the WebSocket, returning False if disconnected."""
            nonlocal ws_connected
            if not ws_connected:
                return False
            try:
                await websocket.send(json.dumps(msg))
                return True
            except (ConnectionClosed, ConnectionClosedError):
                ws_connected = False
                logger.debug("Client disconnected during chat stream for %s", conversation_id[:8])
                return False

        def _session_ref() -> str | None:
            """Get the session ref (#N) for the current conversation."""
            s = self._chat_sessions.get(conversation_id)
            if s and getattr(s, "seq_num", None):
                return f"#{s.seq_num}"
            return None

        async def _persist_message(session: Any, role: str, text: str) -> None:
            """Persist a chat message to the database (best-effort)."""
            message_manager = getattr(self, "message_manager", None)
            db_sid = getattr(session, "db_session_id", None)
            if not message_manager or not db_sid or not text:
                return
            try:
                idx = session.message_index
                session.message_index = idx + 1
                msg = ParsedMessage(
                    index=idx,
                    role=role,
                    content=text,
                    content_type="text",
                    tool_name=None,
                    tool_input=None,
                    tool_result=None,
                    timestamp=datetime.now(UTC),
                    raw_json={},
                )
                await message_manager.store_messages(db_sid, [msg])
            except Exception as e:
                logger.error(f"Failed to persist {role} message for {conversation_id[:8]}: {e}")
                try:
                    await _safe_send(
                        {
                            "type": "chat_warning",
                            "conversation_id": conversation_id,
                            "warning": "Message may not be saved to history",
                            "code": "PERSIST_FAILED",
                        }
                    )
                except Exception:
                    pass

        async def _emit_pending_approval(tool_name: str, arguments: dict[str, Any]) -> None:
            """Emit pending_approval tool_status to the client."""
            await _safe_send(
                _base_msg(
                    type="tool_status",
                    message_id=assistant_message_id,
                    conversation_id=conversation_id,
                    tool_call_id=f"approval-{uuid4().hex[:8]}",
                    status="pending_approval",
                    tool_name=tool_name,
                    arguments=arguments,
                )
            )

        # Track pending tool calls so we can persist tool_name + arguments
        # when ToolResultEvent arrives (it only has tool_call_id)
        pending_tool_calls: dict[str, dict[str, Any]] = {}

        async def _persist_tool_call(
            tool_call_id: str,
            tool_name: str,
            tool_input: dict[str, Any] | None,
            tool_result: Any | None,
            is_error: bool = False,
        ) -> None:
            """Persist a tool_use + tool_result pair as session messages."""
            if session is None:
                return
            message_manager = getattr(self, "message_manager", None)
            db_sid = getattr(session, "db_session_id", None)
            if not message_manager or not db_sid:
                return
            try:
                idx = session.message_index
                session.message_index = idx + 1
                tool_use_msg = ParsedMessage(
                    index=idx,
                    role="assistant",
                    content=tool_name,
                    content_type="tool_use",
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_result=None,
                    timestamp=datetime.now(UTC),
                    raw_json={},
                )
                idx2 = session.message_index
                session.message_index = idx2 + 1
                result_content = ""
                if tool_result is not None:
                    result_content = (
                        json.dumps(tool_result) if not isinstance(tool_result, str) else tool_result
                    )
                if is_error:
                    result_content = f"Error: {result_content}"
                tool_result_msg = ParsedMessage(
                    index=idx2,
                    role="tool",
                    content=result_content,
                    content_type="tool_result",
                    tool_name=tool_name,
                    tool_input=None,
                    tool_result=tool_result if not is_error else None,
                    timestamp=datetime.now(UTC),
                    raw_json={},
                )
                await message_manager.store_messages(db_sid, [tool_use_msg, tool_result_msg])
            except Exception as e:
                logger.error(f"Failed to persist tool call for {conversation_id[:8]}: {e}")
                try:
                    await _safe_send(
                        {
                            "type": "chat_warning",
                            "conversation_id": conversation_id,
                            "warning": "Tool call may not be saved to history",
                            "code": "PERSIST_FAILED",
                        }
                    )
                except Exception:
                    pass

        gen: AsyncIterator[Any] | None = None
        try:
            # Get or create ChatSession for this conversation
            session = self._chat_sessions.get(conversation_id)
            if session is None:
                try:
                    session = await self._create_chat_session(
                        conversation_id, model=model, project_id=project_id
                    )
                    # Notify client of session identity + branch context
                    ref = _session_ref()
                    session_info_msg = _base_msg(
                        type="session_info",
                        conversation_id=conversation_id,
                    )
                    # Include DB session ID so frontend can call session APIs
                    # (e.g. synthesize-title) without waiting for sessions list poll
                    db_sid = getattr(session, "db_session_id", None)
                    if db_sid:
                        session_info_msg["db_session_id"] = db_sid
                    if ref:
                        session_info_msg["session_ref"] = ref
                    branch, wt_path = await _resolve_git_branch(
                        getattr(session, "project_path", None)
                    )
                    if branch:
                        session_info_msg["current_branch"] = branch
                    if wt_path:
                        session_info_msg["worktree_path"] = wt_path
                    # Include active agent name so frontend can display it
                    agent_name = getattr(session, "_pending_agent_name", None) or "default"
                    session_info_msg["agent_name"] = agent_name
                    await websocket.send(json.dumps(session_info_msg))
                except Exception as e:
                    logger.error(f"Failed to start chat session: {e}")
                    await websocket.send(
                        json.dumps(
                            _base_msg(
                                type="chat_error",
                                message_id=assistant_message_id,
                                conversation_id=conversation_id,
                                error="Failed to start chat session. Please try again.",
                            )
                        )
                    )
                    return

            elif model and session.model and model != session.model:
                # Mid-conversation model switch
                old_model = session.model
                try:
                    await session.switch_model(model)
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "model_switched",
                                "conversation_id": conversation_id,
                                "old_model": old_model,
                                "new_model": model,
                            }
                        )
                    )
                except Exception as e:
                    logger.warning(f"Failed to switch model to {model}: {e}")
                    await websocket.send(
                        json.dumps(
                            _base_msg(
                                type="chat_error",
                                message_id=assistant_message_id,
                                conversation_id=conversation_id,
                                error="Failed to switch model. The previous model is still active.",
                            )
                        )
                    )

            # Wire tool approval callback for this request
            session._tool_approval_callback = _emit_pending_approval

            # Persist user message to database
            user_text = content if isinstance(content, str) else json.dumps(content)
            await _persist_message(session, "user", user_text)

            # Mark session as active while streaming
            db_sid = getattr(session, "db_session_id", None)
            if db_sid:
                _sm = getattr(self, "session_manager", None)
                if _sm:
                    try:
                        await asyncio.to_thread(_sm.update, db_sid, status="active")
                        await self.broadcast_session_event("updated", db_sid)
                    except Exception:
                        logger.debug("Failed to set session status to active", exc_info=True)

            # Enrich content with inject_context for SDK (invisible to chat UI)
            sdk_content = content
            if inject_context and isinstance(inject_context, str):
                if isinstance(sdk_content, str):
                    sdk_content = (
                        f"{sdk_content}\n\n<skill-context>\n{inject_context}\n</skill-context>"
                    )
                elif isinstance(sdk_content, list):
                    # For content blocks, append context as an additional text block
                    sdk_content = sdk_content + [
                        {
                            "type": "text",
                            "text": f"\n\n<skill-context>\n{inject_context}\n</skill-context>",
                        }
                    ]

            # Stream events from ChatSession.
            # Hold a reference to the generator so we can explicitly aclose()
            # it in the finally block — this prevents Python's GC from
            # finalizing it in a different asyncio task (which triggers
            # RuntimeError from anyio cancel scope mismatch).
            gen = session.send_message(sdk_content)
            async for event in gen:
                if isinstance(event, ThinkingEvent):
                    if not await _safe_send(
                        _base_msg(
                            type="chat_thinking",
                            message_id=assistant_message_id,
                            conversation_id=conversation_id,
                            content=event.content,
                        )
                    ):
                        break
                elif isinstance(event, TextChunk):
                    # Plan approval boundary — start a fresh message so
                    # post-approval text doesn't concatenate with pre-approval text.
                    content = event.content
                    session_obj = self._chat_sessions.get(conversation_id)
                    if session_obj and getattr(session_obj, "_plan_approval_completed", False):
                        session_obj._plan_approval_completed = False
                        if accumulated_text.strip():
                            await _persist_message(session, "assistant", accumulated_text)
                            accumulated_text = ""
                        assistant_message_id = f"assistant-{uuid4().hex[:12]}"
                        after_tool_call = False
                        has_sent_text = False
                    elif after_tool_call:
                        # Prevent sentence collisions after tool calls by injecting
                        # a separator when the model resumes text output.
                        # Without this: "What do you think?Ok, let me do that."
                        # With this:    "What do you think?\n\nOk, let me do that."
                        after_tool_call = False
                        if has_sent_text:
                            content = "\n\n" + content
                    if content.strip():
                        has_sent_text = True
                    accumulated_text += content
                    if not await _safe_send(
                        _base_msg(
                            type="chat_stream",
                            message_id=assistant_message_id,
                            conversation_id=conversation_id,
                            content=content,
                            done=False,
                        )
                    ):
                        break
                elif isinstance(event, ToolCallEvent):
                    # Flush accumulated text as a separate message before tool calls.
                    # This prevents text segments from merging across tool boundaries
                    # (e.g., "Want me to test it?Good call." running together).
                    if accumulated_text.strip():
                        await _persist_message(session, "assistant", accumulated_text)
                        accumulated_text = ""
                    # Track pending tool call for persistence on result
                    pending_tool_calls[event.tool_call_id] = {
                        "tool_name": event.tool_name,
                        "arguments": event.arguments,
                    }
                    if not await _safe_send(
                        _base_msg(
                            type="tool_status",
                            message_id=assistant_message_id,
                            conversation_id=conversation_id,
                            tool_call_id=event.tool_call_id,
                            status="calling",
                            tool_name=event.tool_name,
                            server_name=event.server_name,
                            arguments=event.arguments,
                        )
                    ):
                        break
                elif isinstance(event, ToolResultEvent):
                    after_tool_call = True
                    # Persist tool_use + tool_result pair to DB
                    pending = pending_tool_calls.pop(event.tool_call_id, {})
                    if not pending:
                        logger.warning(
                            "ToolResultEvent for %s arrived before ToolCallEvent "
                            "(tool_name will be 'unknown')",
                            event.tool_call_id,
                        )
                    await _persist_tool_call(
                        tool_call_id=event.tool_call_id,
                        tool_name=pending.get("tool_name", "unknown"),
                        tool_input=pending.get("arguments"),
                        tool_result=event.result if event.success else event.error,
                        is_error=not event.success,
                    )
                    if not await _safe_send(
                        _base_msg(
                            type="tool_status",
                            message_id=assistant_message_id,
                            conversation_id=conversation_id,
                            tool_call_id=event.tool_call_id,
                            status="completed" if event.success else "error",
                            result=event.result,
                            error=event.error,
                        )
                    ):
                        break
                elif isinstance(event, DoneEvent):
                    # Persist remaining assistant text (after last tool call, if any)
                    if accumulated_text.strip():
                        await _persist_message(session, "assistant", accumulated_text)

                    done_msg = _base_msg(
                        type="chat_stream",
                        message_id=assistant_message_id,
                        conversation_id=conversation_id,
                        content="",
                        done=True,
                        tool_calls_count=event.tool_calls_count,
                    )
                    ref = _session_ref()
                    if ref:
                        done_msg["session_ref"] = ref
                    # Include usage data if available.
                    # total_input_tokens = uncached + cache_read + cache_creation
                    # (the real context size; input_tokens alone is tiny with caching)
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

                    # Adopt SDK session_id as external_id (replaces temp frontend UUID)
                    sdk_sid = event.sdk_session_id
                    if sdk_sid:
                        done_msg["sdk_session_id"] = sdk_sid
                    if sdk_sid and sdk_sid != conversation_id:
                        # Update DB external_id
                        db_sid = getattr(session, "db_session_id", None)
                        session_mgr = getattr(self, "session_manager", None)
                        if db_sid and session_mgr:
                            try:
                                await asyncio.to_thread(
                                    session_mgr.update, db_sid, external_id=sdk_sid
                                )
                            except Exception:
                                logger.debug(
                                    "Failed to update external_id to SDK session_id for %s",
                                    db_sid,
                                    exc_info=True,
                                )
                        # Re-key in-memory dicts
                        self._chat_sessions[sdk_sid] = self._chat_sessions.pop(
                            conversation_id, session
                        )
                        if conversation_id in self._active_chat_tasks:
                            self._active_chat_tasks[sdk_sid] = self._active_chat_tasks.pop(
                                conversation_id
                            )
                        logger.info(
                            "Re-keyed web chat session %s → %s",
                            conversation_id[:8],
                            sdk_sid[:8],
                        )
                        conversation_id = sdk_sid

                    await _safe_send(done_msg)

                    # Persist usage to DB (best-effort)
                    db_sid = getattr(session, "db_session_id", None)
                    session_manager = getattr(self, "session_manager", None)
                    if db_sid and session_manager:
                        has_usage = (
                            event.total_input_tokens is not None or event.output_tokens is not None
                        )
                        if has_usage:
                            try:
                                prev_output = getattr(session, "_accumulated_output_tokens", 0)
                                new_output = prev_output + (event.output_tokens or 0)
                                session._accumulated_output_tokens = new_output

                                prev_cost = getattr(session, "_accumulated_cost_usd", 0.0)
                                new_cost = prev_cost + (event.cost_usd or 0.0)
                                session._accumulated_cost_usd = new_cost

                                await asyncio.to_thread(
                                    session_manager.update_usage,
                                    db_sid,
                                    input_tokens=event.total_input_tokens or 0,
                                    output_tokens=new_output,
                                    cache_creation_tokens=event.cache_creation_input_tokens or 0,
                                    cache_read_tokens=event.cache_read_input_tokens or 0,
                                    total_cost_usd=new_cost,
                                    context_window=event.context_window,
                                    model=getattr(session, "_last_model", None),
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to persist usage for %s", db_sid, exc_info=True
                                )
                        else:
                            # No token data — still persist context_window + model
                            try:
                                updates: dict[str, Any] = {}
                                if event.context_window is not None:
                                    updates["context_window"] = event.context_window
                                last_model = getattr(session, "_last_model", None)
                                if last_model:
                                    updates["model"] = last_model
                                if updates:
                                    updates["updated_at"] = datetime.now(UTC).isoformat()
                                    await asyncio.to_thread(
                                        session_manager.db.safe_update,
                                        "sessions",
                                        updates,
                                        "id = ?",
                                        (db_sid,),
                                    )
                            except Exception:
                                logger.debug(
                                    "Failed to persist context_window for %s",
                                    db_sid,
                                    exc_info=True,
                                )

                    # Mark session as paused now that streaming is done
                    if db_sid and session_manager:
                        try:
                            await asyncio.to_thread(session_manager.update, db_sid, status="paused")
                            await self.broadcast_session_event("updated", db_sid)
                        except Exception:
                            logger.debug("Failed to set session status to paused", exc_info=True)

        except asyncio.CancelledError:
            # Stream was interrupted (stop button or new message replacing old)
            try:
                await websocket.send(
                    json.dumps(
                        _base_msg(
                            type="chat_stream",
                            message_id=assistant_message_id,
                            conversation_id=conversation_id,
                            content="",
                            done=True,
                            interrupted=True,
                        )
                    )
                )
            except (ConnectionClosed, ConnectionClosedError):
                pass

        except (ConnectionClosed, ConnectionClosedError):
            # Client disconnected mid-stream — not an error
            logger.debug(f"Client disconnected during chat stream for {conversation_id}")

        except Exception as exc:
            logger.exception(f"Chat error for conversation {conversation_id}")
            error_msg, error_code = self._classify_chat_error(exc)
            try:
                await websocket.send(
                    json.dumps(
                        _base_msg(
                            type="chat_error",
                            message_id=assistant_message_id,
                            conversation_id=conversation_id,
                            error=error_msg,
                            code=error_code,
                        )
                    )
                )
            except (ConnectionClosed, ConnectionClosedError):
                pass

        finally:
            # Explicitly close the async generator in THIS task to prevent
            # Python's GC from finalizing it in a different asyncio task
            # (which causes RuntimeError from anyio cancel scope mismatch).
            if gen is not None:
                _aclose = getattr(gen, "aclose", None)
                if _aclose is not None:
                    try:
                        await _aclose()
                    except BaseException:
                        pass
            self._active_chat_tasks.pop(conversation_id, None)

    async def _handle_ask_user_response(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle ask_user_response message from the web UI.

        Looks up the ChatSession by conversation_id and forwards the user's
        answers to unblock the pending AskUserQuestion callback.
        """
        conversation_id = data.get("conversation_id")
        answers = data.get("answers", {})

        session = self._chat_sessions.get(conversation_id) if conversation_id else None
        if session is None:
            logger.warning(f"ask_user_response for unknown conversation: {conversation_id}")
            return

        if not session.has_pending_question:
            logger.warning(f"ask_user_response but no pending question for {conversation_id}")
            return

        session.provide_answer(answers)

    async def _handle_tool_approval_response(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle tool_approval_response message from the web UI.

        Looks up the ChatSession by conversation_id and forwards the user's
        approval decision to unblock the pending tool approval callback.
        """
        conversation_id = data.get("conversation_id")
        decision = data.get("decision", "reject")
        if decision not in ("approve", "reject", "approve_always"):
            decision = "reject"

        session = self._chat_sessions.get(conversation_id) if conversation_id else None
        if session is None:
            logger.warning(f"tool_approval_response for unknown conversation: {conversation_id}")
            return

        if not session.has_pending_approval:
            logger.warning(f"tool_approval_response but no pending approval for {conversation_id}")
            return

        session.provide_approval(decision)
