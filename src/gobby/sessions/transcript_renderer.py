from __future__ import annotations

from collections.abc import Iterable

from gobby.sessions.observation_tracker import ObservationTracker
from gobby.sessions.transcript_render_blocks import (
    _INTERNAL_CONTENT_TYPES,
    _classify_render_role,
    _process_message_block,
)
from gobby.sessions.transcript_render_models import (
    ContentBlock,
    RenderedMessage,
    RenderedToolCall,
    RenderState,
    ToolResult,
    ToolResultKind,
)
from gobby.sessions.transcript_tool_metadata import classify_tool, extract_result_metadata
from gobby.sessions.transcripts.base import (
    UNMODELED_RECORD_CONTENT_TYPE,
    ParsedMessage,
    TranscriptParserErrorLog,
)

__all__ = [
    "ContentBlock",
    "RenderState",
    "RenderedMessage",
    "RenderedToolCall",
    "ToolResult",
    "ToolResultKind",
    "classify_tool",
    "extract_result_metadata",
    "render_incremental",
    "render_transcript",
]


def render_transcript(
    parsed_messages: Iterable[ParsedMessage],
    session_id: str | None = None,
    cli_name: str = "claude",
    error_log: TranscriptParserErrorLog | None = None,
    source: str | None = None,
    observation_tracker: ObservationTracker | None = None,
) -> list[RenderedMessage]:
    """
    Render a full transcript from a stream of parsed messages.

    Args:
        parsed_messages: Stream of flat ParsedMessage objects.
        session_id: Optional session identifier.
        cli_name: CLI name to use for error logging if error_log is not provided.
        error_log: Optional error log instance to use.

    Returns:
        List of grouped RenderedMessage objects.
    """
    if not error_log:
        error_log = TranscriptParserErrorLog(cli_name)
    if observation_tracker is None:
        observation_tracker = ObservationTracker()

    state = RenderState()
    rendered_messages: list[RenderedMessage] = []
    rendered_message_objects: set[int] = set()

    for msg in parsed_messages:
        completed, state = render_incremental(
            [msg],
            state,
            session_id=session_id,
            error_log=error_log,
            source=source or cli_name,
            observation_tracker=observation_tracker,
        )
        for message in completed:
            if id(message) not in rendered_message_objects:
                rendered_messages.append(message)
                rendered_message_objects.add(id(message))

    if state.current_message and id(state.current_message) not in rendered_message_objects:
        rendered_messages.append(state.current_message)

    return rendered_messages


def render_incremental(
    new_messages: list[ParsedMessage],
    pending_state: RenderState,
    session_id: str | None = None,
    error_log: TranscriptParserErrorLog | None = None,
    source: str | None = None,
    observation_tracker: ObservationTracker | None = None,
) -> tuple[list[RenderedMessage], RenderState]:
    """
    Process new messages and return completed turns.

    Args:
        new_messages: Batch of new ParsedMessage objects.
        pending_state: Current accumulation state.
        session_id: Optional session identifier.
        error_log: Optional error log instance.

    Returns:
        Tuple of (newly completed messages, updated state).
    """
    completed_messages: list[RenderedMessage] = []
    completed_message_ids: set[str] = set()

    def append_completed(message: RenderedMessage) -> None:
        if message.id not in completed_message_ids:
            completed_messages.append(message)
            completed_message_ids.add(message.id)

    state = pending_state
    if observation_tracker is None:
        observation_tracker = ObservationTracker()

    for msg in new_messages:
        if msg.content_type == UNMODELED_RECORD_CONTENT_TYPE:
            # Genuinely-unknown record-level type: route to the T2 observation
            # worklist (telemetry) then skip — no card, no group. The real
            # envelope type rides in msg.content. observation_tracker is non-None
            # here (defaulted above).
            observation_tracker.observe_block_type(
                msg,
                session_id=session_id,
                source=msg.source or source,
                block_type=(msg.content if isinstance(msg.content, str) else "<missing>"),
            )
            continue
        if msg.content_type in _INTERNAL_CONTENT_TYPES:
            continue

        # 1. Classify role and detect hook/protocol/bootstrap feedback
        role = _classify_render_role(msg)

        # 2. Tool result pairing (can bypass turn logic if paired)
        is_tool_result = msg.content_type in ["tool_result", "mcp_tool_result"]
        if is_tool_result and state.knows_tool_call(msg.tool_use_id):
            owner = state.tool_call_messages.get(msg.tool_use_id)
            _process_message_block(
                msg,
                state,
                session_id=session_id,
                error_log=error_log,
                source=source,
                observation_tracker=observation_tracker,
            )
            if owner is not None and owner is not state.current_message:
                append_completed(owner)
            continue

        # 3. Detect turn boundary
        is_new_turn = False
        if not state.current_message:
            is_new_turn = True
        elif state.current_message.role != role:
            is_new_turn = True
        elif role in ["user", "system"]:
            is_new_turn = True

        if is_new_turn and state.current_message:
            append_completed(state.current_message)
            state.current_message = None
            state.seen_content.clear()

        # 4. Initialize new message if needed
        if not state.current_message:
            state.current_message = RenderedMessage(
                id=f"{session_id or 'no-session'}-{role}-{msg.timestamp.timestamp()}-{msg.index}",
                role=role,
                content="",
                timestamp=msg.timestamp,
                model=msg.model,
                usage=msg.usage,
            )

        # 5. Process block
        _process_message_block(
            msg,
            state,
            session_id=session_id,
            error_log=error_log,
            source=source,
            observation_tracker=observation_tracker,
        )

    return completed_messages, state
