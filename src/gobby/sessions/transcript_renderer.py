from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from gobby.hooks.normalization import is_shell_tool
from gobby.sessions.transcripts.base import (
    ParsedMessage,
    TokenUsage,
    TranscriptParserErrorLog,
)

logger = logging.getLogger(__name__)

_INTERNAL_CONTENT_TYPES: frozenset[str] = frozenset({"hook_prompt"})


@dataclass
class ToolResult:
    """Result of a tool execution."""

    content: Any
    content_type: str  # text/json/image/error
    truncated: bool = False
    metadata: dict[str, Any] | None = None


@dataclass
class RenderedToolCall:
    """A tool call and its result."""

    id: str
    tool_name: str
    server_name: str
    tool_type: str
    arguments: dict[str, Any]
    result: ToolResult | None = None
    status: str = "pending"
    error: str | None = None


TOOL_TYPE_MAP = {
    "Bash": "bash",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Grep": "grep",
    "Glob": "glob",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
    "AskUserQuestion": "ask_user",
    "Agent": "agent",
    "NotebookEdit": "notebook",
}


def classify_tool(tool_name: str | None) -> tuple[str, str | None]:
    """Returns (tool_type, server_name). Extracts server from mcp__server__tool naming."""
    if not tool_name:
        return "unknown", None

    if is_shell_tool(tool_name):
        return "bash", None

    if tool_name in TOOL_TYPE_MAP:
        return TOOL_TYPE_MAP[tool_name], None

    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3:
            return "mcp", parts[1]
        return "mcp", "unknown"

    return "unknown", None


def extract_result_metadata(
    tool_type: str, result_content: Any, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Extract tool-specific metadata from result for rich frontend rendering."""
    metadata: dict[str, Any] = {}
    if result_content is None:
        return metadata

    match tool_type:
        case "bash":
            if isinstance(result_content, dict):
                metadata["exit_code"] = result_content.get("exit_code")
                stdout = result_content.get("stdout", "")
                stderr = result_content.get("stderr", "")
                if isinstance(stdout, str):
                    metadata["stdout_lines"] = len(stdout.splitlines())
                if isinstance(stderr, str):
                    metadata["stderr_lines"] = len(stderr.splitlines())
        case "read":
            if isinstance(result_content, str):
                metadata["line_count"] = len(result_content.splitlines())
            if arguments:
                metadata["file_path"] = arguments.get("file_path") or arguments.get("path")
        case "edit":
            if arguments:
                metadata["file_path"] = arguments.get("file_path") or arguments.get("path")
        case "grep":
            if isinstance(result_content, dict):
                metadata["files_matched"] = result_content.get("files_matched")
                metadata["total_matches"] = result_content.get("total_matches")
        case "glob":
            if isinstance(result_content, list):
                metadata["files_found"] = len(result_content)

    return metadata


@dataclass
class ContentBlock:
    """A block of content within a message."""

    type: str  # text, thinking, tool_chain, tool_reference, image, document, web_search_result, unknown
    content: Any | None = None  # content can be Any for pass-through types
    tool_calls: list[RenderedToolCall] | None = None
    tool_name: str | None = None  # For tool_reference
    server_name: str | None = None  # For tool_reference
    source: dict[str, Any] | None = None  # For image/document
    raw: dict[str, Any] | None = None
    source_line: int | None = None
    block_type: str | None = None  # Original type for 'unknown' blocks


@dataclass
class RenderedMessage:
    """A grouped message ready for rendering."""

    id: str
    role: str
    content: str  # plain text summary
    timestamp: datetime
    content_blocks: list[ContentBlock] = field(default_factory=list)
    model: str | None = None
    usage: TokenUsage | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "content_blocks": [asdict(b) for b in self.content_blocks],
            "model": self.model,
            "usage": asdict(self.usage) if self.usage else None,
        }


@dataclass
class RenderState:
    """Accumulator for in-progress message turns."""

    current_message: RenderedMessage | None = None
    # Map of tool_use_id -> RenderedToolCall
    pending_tool_calls: dict[str, RenderedToolCall] = field(default_factory=dict)
    # Track seen content hashes to deduplicate Claude Code streaming duplicates
    seen_content: set[int] = field(default_factory=set)


def render_transcript(
    parsed_messages: Iterable[ParsedMessage],
    session_id: str | None = None,
    cli_name: str = "claude",
    error_log: TranscriptParserErrorLog | None = None,
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

    state = RenderState()
    rendered_messages = []

    for msg in parsed_messages:
        completed, state = render_incremental(
            [msg], state, session_id=session_id, error_log=error_log
        )
        rendered_messages.extend(completed)

    if state.current_message:
        rendered_messages.append(state.current_message)

    return rendered_messages


def render_incremental(
    new_messages: list[ParsedMessage],
    pending_state: RenderState,
    session_id: str | None = None,
    error_log: TranscriptParserErrorLog | None = None,
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
    completed_messages = []
    state = pending_state

    for msg in new_messages:
        if msg.content_type in _INTERNAL_CONTENT_TYPES:
            continue

        # 1. Classify role and detect hook feedback
        role = msg.role
        if _is_hook_feedback(msg):
            role = "system"

        # 2. Tool result pairing (can bypass turn logic if paired)
        is_tool_result = msg.content_type in ["tool_result", "mcp_tool_result"]
        if is_tool_result and msg.tool_use_id in state.pending_tool_calls:
            _process_message_block(msg, state, session_id=session_id, error_log=error_log)
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
            completed_messages.append(state.current_message)
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
        _process_message_block(msg, state, session_id=session_id, error_log=error_log)

    return completed_messages, state


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


_FULLY_STRIPPED_PROTOCOL_TAGS: tuple[str, ...] = (
    "system-reminder",
    "task-notification",
    "local-command-caveat",
    "local-command-stdout",
    "command-name",
    "command-args",
    "command-message",
    "hook_context",
    "hook-context",
    "antml_thinking",
    "antml_function_calls",
    "antml_invoke",
    "environment_context",
    "skill",
)

_UNCLOSED_PROTOCOL_TAGS: tuple[str, ...] = (
    "system-reminder",
    "task-notification",
    "local-command-caveat",
    "hook_context",
    "environment_context",
    "skill",
)

_WRAPPER_STRIPPED_PROTOCOL_TAGS: tuple[str, ...] = (
    "proposed_plan",
    "proposed_implementation",
    "search_quality_reflection",
    "permissions instructions",
    "permission instructions",
    "collaboration_mode",
    "turn_aborted",
    "instructions",
    "skills_instructions",
)


def _tag_pattern(tags: tuple[str, ...]) -> str:
    return "|".join(re.escape(tag) for tag in tags)


_FULLY_STRIPPED_PROTOCOL_TAG_PATTERN = _tag_pattern(_FULLY_STRIPPED_PROTOCOL_TAGS)
_UNCLOSED_PROTOCOL_TAG_PATTERN = _tag_pattern(_UNCLOSED_PROTOCOL_TAGS)
_WRAPPER_STRIPPED_PROTOCOL_TAG_PATTERN = _tag_pattern(_WRAPPER_STRIPPED_PROTOCOL_TAGS)

# Protocol XML tags that should be stripped from rendered content
_PROTOCOL_TAG_RE = re.compile(
    rf"<(?:{_FULLY_STRIPPED_PROTOCOL_TAG_PATTERN})(?=[\s>])[^>]*>"
    rf".*?"
    rf"</(?:{_FULLY_STRIPPED_PROTOCOL_TAG_PATTERN})\s*>",
    re.DOTALL | re.IGNORECASE,
)

# Unclosed protocol tags (content extends to end of string)
_PROTOCOL_TAG_UNCLOSED_RE = re.compile(
    rf"<(?:{_UNCLOSED_PROTOCOL_TAG_PATTERN})(?=[\s>])[^>]*>.*$",
    re.DOTALL | re.IGNORECASE,
)

# Tags where only the wrapper should be stripped (content preserved)
_TAG_ONLY_STRIP_RE = re.compile(
    rf"</?(?:{_WRAPPER_STRIPPED_PROTOCOL_TAG_PATTERN})(?=[\s>])[^>]*>",
    re.IGNORECASE,
)


def _strip_protocol_tags(content: str) -> str:
    """Remove protocol XML tags from message content.

    Strips environment_context, skill payloads, local-command-*,
    hook_context, and other infrastructure tags that shouldn't be
    displayed in the session viewer. Also strips tag wrappers (but
    preserves content) for proposed_plan, turn_aborted,
    instructions, and similar prompt metadata tags.
    """
    if "<" not in content:
        return content
    result = _PROTOCOL_TAG_RE.sub("", content)
    result = _PROTOCOL_TAG_UNCLOSED_RE.sub("", result)
    result = _TAG_ONLY_STRIP_RE.sub("", result)
    # Clean up leftover whitespace
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def _process_message_block(
    msg: ParsedMessage,
    state: RenderState,
    session_id: str | None = None,
    error_log: TranscriptParserErrorLog | None = None,
) -> None:
    """Integrate a ParsedMessage into the current RenderedMessage or pair as tool result."""

    # Tool Result Pairing
    if msg.content_type in ["tool_result", "mcp_tool_result"]:
        if msg.tool_use_id and msg.tool_use_id in state.pending_tool_calls:
            tool_call = state.pending_tool_calls[msg.tool_use_id]
            content = msg.tool_result or msg.content
            tool_call.result = ToolResult(
                content=content,
                content_type="json" if msg.tool_result else "text",
                metadata=extract_result_metadata(tool_call.tool_type, content, tool_call.arguments),
            )
            tool_call.status = "completed"
            return

    if not state.current_message:
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

    # Strip protocol tags from all message content
    block_text: Any = msg.content
    if isinstance(block_text, str):
        block_text = _strip_protocol_tags(block_text)

    # Block Type Mapping
    original_type = msg.content_type
    block_type = original_type
    block_content: Any = block_text

    if block_type in ["tool_use", "mcp_tool_use"]:
        block_type = "tool_chain"
    elif block_type == "web_search_tool_result":
        block_type = "web_search_result"
        block_content = msg.tool_result or block_text
    elif block_type in ["text", "thinking", "tool_reference", "image", "document"]:
        pass  # Use as-is
    else:
        # Fallback for unknown types
        block_type = "unknown"
        if error_log:
            error_log.log_unknown_block(
                line_num=msg.index,
                session_id=session_id,
                block_type=original_type,
                raw=msg.raw_json,
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
                t_type, s_name = classify_tool(t_name)
                block.tool_name = t_name
                block.server_name = s_name or "builtin"

        if block_type == "unknown":
            block.block_type = original_type
            block.raw = msg.raw_json

        if block_type == "tool_chain":
            tool_type, server_name = classify_tool(msg.tool_name)
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

        state.current_message.content_blocks.append(block)

    # Update summary content
    if msg.content_type == "text":
        if isinstance(block_content, str):
            state.current_message.content += block_content
