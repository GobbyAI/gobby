"""Codex app-server turn streaming helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol, cast

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.codex_impl.item_normalization import (
    DynamicExecCorrelator,
    build_pre_tool_lifecycle_payload,
)
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ToolCallEvent,
    ToolResultEvent,
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

logger = logging.getLogger(__name__)

_CODEX_TURN_TIMEOUT_SECONDS = 600.0

BeforeToolDedupKeyExtractor = Callable[[dict[str, Any]], str | None]


class CodexTurnSession(Protocol):
    conversation_id: str
    sdk_session_id: str | None
    _model: str | None
    _thread_id: str | None
    _turn_id: str | None
    reasoning_effort: str | None

    async def _get_transcript_offset(self) -> int: ...

    async def _get_transcript_assistant_text_since(self, offset: int) -> str: ...

    async def _get_transcript_records_since(
        self,
        offset: int,
    ) -> list[ParsedMessage | ParsedToolEvent]: ...

    def _reset_before_tool_state(self) -> None: ...

    async def _dispatch_before_tool_once(
        self,
        dedup_key: str | None,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    async def _apply_post_tool_lifecycle(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_response: Any,
        *,
        is_error: bool | None = None,
    ) -> dict[str, Any] | None: ...

    def _resolve_context_window(self) -> int | None: ...


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


async def stream_codex_turn(
    *,
    client: CodexAppServerClient,
    session: CodexTurnSession,
    prompt: str,
    context_prefix: str | None,
    extract_before_tool_dedup_key: BeforeToolDedupKeyExtractor,
    request_parameters: Mapping[str, object] | None = None,
) -> AsyncIterator[ChatEvent]:
    thread_id = session._thread_id
    if thread_id is None:
        raise RuntimeError("Codex session missing threadId")

    event_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    turn_completed = asyncio.Event()
    saw_text_output = False
    tool_calls_count = 0
    started_tool_call_ids: set[str] = set()
    completed_tool_call_ids: set[str] = set()
    lifecycle_completed_tool_call_ids: set[str] = set()
    latest_transcript_usage: dict[str, int | None] | None = None
    latest_transcript_context_window: int | None = None
    dynamic_exec_correlator = DynamicExecCorrelator()
    transcript_offset = await session._get_transcript_offset()
    session._reset_before_tool_state()
    turn_parameters = cast(dict[str, Any], dict(request_parameters or {}))

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
        if isinstance(thread_id, str) and session._thread_id and thread_id != session._thread_id:
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
        "turn/failed",
        "thread/closed",
        "agent/messageDelta",
        "item/agentMessage/delta",
        "item/started",
        "item/completed",
        "response_item",
        "event_msg",
    ]
    for method in event_methods:
        client.add_notification_handler(method, _enqueue)

    try:
        turn = await client.start_turn(
            thread_id,
            prompt,
            context_prefix=context_prefix,
            model=session._model,
            effort=session.reasoning_effort,
            **turn_parameters,
        )
        session._turn_id = turn.id or session._turn_id
        turn_deadline = asyncio.get_running_loop().time() + _CODEX_TURN_TIMEOUT_SECONDS

        while not turn_completed.is_set():
            if not client.is_connected:
                raise RuntimeError("Codex app-server disconnected before turn completed")
            poll_deadline = min(turn_deadline, asyncio.get_running_loop().time() + 0.1)
            try:
                async with asyncio.timeout_at(poll_deadline):
                    method, params = await event_queue.get()
            except TimeoutError:
                if asyncio.get_running_loop().time() >= turn_deadline:
                    raise RuntimeError(
                        f"Codex turn timed out after {_CODEX_TURN_TIMEOUT_SECONDS:g} seconds"
                    ) from None
                continue

            if method in {"agent/messageDelta", "item/agentMessage/delta"}:
                delta = _extract_codex_delta(params)
                if delta:
                    saw_text_output = True
                    yield TextChunk(content=delta)
                continue

            if method == "item/completed":
                tool_event_data = codex_tool_event_data(
                    params,
                    dynamic_exec_correlator=dynamic_exec_correlator,
                )
                if tool_event_data is not None:
                    tool_call_id = str(tool_event_data["tool_call_id"])
                    if not tool_call_id or tool_call_id not in lifecycle_completed_tool_call_ids:
                        await session._apply_post_tool_lifecycle(
                            str(tool_event_data["tool_name"]),
                            tool_event_data["arguments"],
                            tool_event_data["lifecycle_response"],
                            is_error=tool_event_data["is_error"],
                        )
                        if tool_call_id:
                            lifecycle_completed_tool_call_ids.add(tool_call_id)

                    if tool_call_id in completed_tool_call_ids:
                        continue
                    completed_tool_call_ids.add(tool_call_id)

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
                        extract_before_tool_dedup_key(params),
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
                if turn_completed.is_set():
                    continue
                raise RuntimeError("Codex thread closed before turn completed")

            if method == "turn/failed":
                error = params.get("error")
                message = str(error) if error else "Codex turn failed"
                raise RuntimeError(message)

            if method == "turn/completed":
                turn_completed.set()
                usage = params.get("usage", {})
                if not isinstance(usage, dict):
                    usage = {}
                transcript_records = await session._get_transcript_records_since(transcript_offset)
                transcript_assistant_text: list[str] = []
                for record in transcript_records:
                    for event in _events_from_transcript_record(record):
                        yield event
                    if (
                        isinstance(record, ParsedMessage)
                        and record.role == "assistant"
                        and isinstance(record.content, str)
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
                continue
    except asyncio.CancelledError as cancel_exc:
        active_turn_id = session._turn_id
        if active_turn_id:
            try:
                await asyncio.wait_for(
                    client.interrupt_turn(thread_id, active_turn_id), timeout=1.0
                )
            except (TimeoutError, OSError, RuntimeError) as exc:
                if isinstance(exc, RuntimeError) and "expected active turn id" in str(exc):
                    # The turn already advanced; the one we meant to kill is
                    # gone, which is the desired end state.
                    logger.debug(
                        "Codex turn %s already finished before interrupt for session %s: %s",
                        active_turn_id,
                        session.conversation_id,
                        exc,
                    )
                else:
                    logger.warning(
                        "Failed to interrupt cancelled Codex turn %s for session %s: %s",
                        active_turn_id,
                        session.conversation_id,
                        exc,
                    )
        raise cancel_exc
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
            client.remove_notification_handler(method, _enqueue)
