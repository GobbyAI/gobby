from __future__ import annotations

from typing import Any

from gobby.sessions.observation_tracker import ObservationTracker
from gobby.sessions.transcript_protocol import (
    _extract_protocol_content_segments,
    _is_protocol_only_text,
    _looks_like_system_bootstrap_text,
    _make_plain_text_protocol_tool_call,
    _sanitize_visible_protocol_text,
)
from gobby.sessions.transcript_render_models import (
    ContentBlock,
    RenderedToolCall,
    RenderState,
    ToolResult,
)
from gobby.sessions.transcript_tool_metadata import classify_tool, extract_result_metadata
from gobby.sessions.transcripts.base import (
    RENDER_SKIP_CONTENT_TYPES,
    ParsedMessage,
    TranscriptParserErrorLog,
)

# Plain-skipped metadata content types (no card, no telemetry). Sourced from
# base so the parser, stats, index, and reader stay in lockstep on the set.
_INTERNAL_CONTENT_TYPES: frozenset[str] = RENDER_SKIP_CONTENT_TYPES


def _is_hook_feedback(msg: ParsedMessage) -> bool:
    """Identify hook feedback messages that should be role='system'."""
    if not isinstance(msg.content, str):
        return False
    prefixes = [
        "Stop hook feedback:",
        "PreToolUse hook",
        "PostToolUse hook",
        "UserPromptSubmit hook",
    ]
    return any(msg.content.startswith(p) for p in prefixes)


def _classify_render_role(msg: ParsedMessage) -> str:
    if _is_hook_feedback(msg):
        return "system"

    if (
        msg.role == "user"
        and msg.content_type == "text"
        and isinstance(msg.content, str)
        and (_is_protocol_only_text(msg.content) or _looks_like_system_bootstrap_text(msg.content))
    ):
        return "system"

    return msg.role


def _append_text_content_block(
    state: RenderState,
    text: str,
    source_line: int,
) -> None:
    if state.current_message is None:
        return
    if not state.current_message.content_blocks:
        state.current_message.content_blocks = []

    last_block = (
        state.current_message.content_blocks[-1] if state.current_message.content_blocks else None
    )

    if last_block and last_block.type == "text" and isinstance(last_block.content, str):
        last_block.content += text
        previous_block = None
    else:
        state.current_message.content_blocks.append(
            ContentBlock(type="text", content=text, source_line=source_line)
        )
        previous_block = (
            state.current_message.content_blocks[-2]
            if len(state.current_message.content_blocks) >= 2
            else None
        )

    if (
        previous_block
        and previous_block.type == "tool_chain"
        and state.current_message.content
        and text
        and not state.current_message.content[-1].isspace()
        and not text[0].isspace()
    ):
        state.current_message.content += " "

    state.current_message.content += text


def _append_protocol_tool_call_block(
    state: RenderState,
    tool_call: RenderedToolCall,
    source_line: int,
) -> None:
    if state.current_message is None:
        return
    if not state.current_message.content_blocks:
        state.current_message.content_blocks = []

    last_block = (
        state.current_message.content_blocks[-1] if state.current_message.content_blocks else None
    )
    if (
        last_block
        and last_block.type == "tool_chain"
        and last_block.tool_calls
        and all(call.tool_type == "protocol" for call in last_block.tool_calls)
    ):
        last_block.tool_calls.append(tool_call)
        return

    state.current_message.content_blocks.append(
        ContentBlock(type="tool_chain", tool_calls=[tool_call], source_line=source_line)
    )


def _append_orphan_tool_result_block(msg: ParsedMessage, state: RenderState) -> None:
    if state.current_message is None:
        return

    tool_name = msg.tool_name or "unknown_result"
    tool_type, server_name = classify_tool(tool_name)
    content = msg.tool_result if msg.tool_result is not None else msg.content
    arguments = msg.tool_input or {}
    tool_call = RenderedToolCall(
        id=msg.tool_use_id or f"orphan-result-{msg.index}",
        tool_name=tool_name,
        server_name=server_name or "unknown",
        tool_type=tool_type,
        arguments=arguments,
        result=ToolResult(
            content=content,
            kind="json" if msg.tool_result is not None else "text",
            metadata=extract_result_metadata(tool_type, content, arguments),
        ),
        status="completed",
    )
    state.current_message.content_blocks.append(
        ContentBlock(type="tool_chain", tool_calls=[tool_call], source_line=msg.index)
    )


def _process_message_block(
    msg: ParsedMessage,
    state: RenderState,
    session_id: str | None = None,
    error_log: TranscriptParserErrorLog | None = None,
    source: str | None = None,
    observation_tracker: ObservationTracker | None = None,
) -> None:
    """Integrate a ParsedMessage into the current RenderedMessage or pair as tool result."""

    # Tool Result Pairing
    if msg.content_type in ["tool_result", "mcp_tool_result"]:
        if msg.tool_use_id and msg.tool_use_id in state.pending_tool_calls:
            tool_call = state.pending_tool_calls.pop(msg.tool_use_id)
            content = msg.tool_result if msg.tool_result is not None else msg.content
            tool_call.result = ToolResult(
                content=content,
                kind="json" if msg.tool_result is not None else "text",
                metadata=extract_result_metadata(tool_call.tool_type, content, tool_call.arguments),
            )
            tool_call.status = "completed"
            # The call is answered, so drop the two entries that were holding its
            # arguments, its result and its whole owner message alive for the rest
            # of the session, and keep only the id a duplicate needs (#20859).
            # Popping the index entry does not touch the payload: the object just
            # mutated is the same one the owner message's content block holds.
            state.tool_call_messages.pop(msg.tool_use_id, None)
            state.resolved_tool_call_ids.add(msg.tool_use_id)
            return
        if msg.tool_use_id and msg.tool_use_id in state.resolved_tool_call_ids:
            # A second result for a call already paired. Suppressed rather than
            # appended, exactly as it was while resolved calls stayed pending.
            return

    if not state.current_message:
        return

    if msg.content_type in ["tool_result", "mcp_tool_result"]:
        _append_orphan_tool_result_block(msg, state)
        return

    # Update metadata
    if msg.model and not state.current_message.model:
        state.current_message.model = msg.model
    if msg.usage and not state.current_message.usage:
        state.current_message.usage = msg.usage

    # Content Deduplication
    try:
        content_key = (msg.content_type, msg.content, msg.tool_use_id, msg.tool_name)
        content_hash = hash(content_key)
        if content_hash in state.seen_content:
            return
        state.seen_content.add(content_hash)
    except TypeError:
        # Non-hashable content (e.g. dict) — skip deduplication
        pass

    # Convert protocol/context tags in text content into synthetic tool calls.
    block_text: Any = msg.content

    # Block Type Mapping
    original_type = msg.content_type
    block_type = original_type
    block_content: Any = block_text

    if block_type == "text" and isinstance(block_text, str):
        if _looks_like_system_bootstrap_text(block_text):
            state.current_message.role = "system"
            _append_protocol_tool_call_block(
                state,
                _make_plain_text_protocol_tool_call(
                    "system_instructions",
                    block_text.strip(),
                    msg.index,
                ),
                msg.index,
            )
            return

        is_protocol_only = _is_protocol_only_text(block_text)
        for segment in _extract_protocol_content_segments(block_text, msg.index):
            if segment.kind == "text" and segment.text is not None:
                _append_text_content_block(state, segment.text, msg.index)
            elif segment.kind == "protocol_tool" and segment.tool_call is not None:
                if is_protocol_only:
                    state.current_message.role = "system"
                _append_protocol_tool_call_block(state, segment.tool_call, msg.index)
        return

    if isinstance(block_text, str):
        block_text = _sanitize_visible_protocol_text(block_text)
        block_content = block_text

    if block_type in ["tool_use", "mcp_tool_use"]:
        block_type = "tool_chain"
    elif block_type == "web_search_tool_result":
        block_type = "web_search_result"
        block_content = msg.tool_result if msg.tool_result is not None else block_text
    elif block_type in [
        "text",
        "thinking",
        "tool_reference",
        "image",
        "document",
        "compaction_summary",
    ]:
        pass  # Use as-is
    else:
        # Fallback for unknown types
        block_type = "unknown"
        if observation_tracker:
            observation_tracker.observe_block_type(
                msg,
                session_id=session_id,
                source=msg.source or source,
                block_type=original_type,
            )

    # Skip empty thinking blocks (defense in depth)
    if block_type == "thinking" and (not block_content or not str(block_content).strip()):
        return

    # Merge consecutive blocks of same type if appropriate
    if (
        state.current_message.content_blocks
        and state.current_message.content_blocks[-1].type == block_type
        and block_type in ["text", "thinking"]
    ):
        last_block = state.current_message.content_blocks[-1]
        if isinstance(last_block.content, str) and isinstance(block_content, str):
            last_block.content += block_content
    else:
        # Create new block
        block = ContentBlock(
            type=block_type,
            content=block_content
            if block_type not in ["tool_chain", "image", "document", "tool_reference"]
            else None,
            source_line=msg.index,
        )

        if block_type == "image" or block_type == "document":
            block.source = (
                block_content if isinstance(block_content, dict) else {"data": str(block_content)}
            )

        if block_type == "tool_reference":
            # For tool_reference, we might have tool_name in msg or content
            t_name = msg.tool_name or (block_content if isinstance(block_content, str) else None)
            if t_name:
                _, s_name = classify_tool(t_name)
                block.tool_name = t_name
                block.server_name = s_name or "builtin"

        if block_type == "unknown":
            block.block_type = original_type
            block.raw = msg.raw_json

        if block_type == "tool_chain":
            tool_type, server_name = classify_tool(msg.tool_name, msg.tool_input)
            if observation_tracker and (tool_type == "unknown" or server_name == "unknown"):
                observation_tracker.observe_tool_name(
                    msg,
                    session_id=session_id,
                    source=msg.source or source,
                    tool_name=msg.tool_name or "unknown",
                    server_name=server_name or "unknown",
                    tool_type=tool_type,
                )
            tool_call = RenderedToolCall(
                id=msg.tool_use_id or f"call-{msg.index}",
                tool_name=msg.tool_name or "unknown",
                server_name=server_name or "unknown",
                tool_type=tool_type,
                arguments=msg.tool_input or {},
            )
            block.tool_calls = [tool_call]
            if msg.tool_use_id:
                state.pending_tool_calls[msg.tool_use_id] = tool_call
                state.tool_call_messages[msg.tool_use_id] = state.current_message

        state.current_message.content_blocks.append(block)

    # Update summary content
    if msg.content_type == "text":
        if isinstance(block_content, str):
            state.current_message.content += block_content
