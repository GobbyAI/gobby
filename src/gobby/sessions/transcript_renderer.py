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
_PROTOCOL_TOOL_NAME = "protocol_context"


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


@dataclass
class _ProtocolContentSegment:
    kind: str
    text: str | None = None
    tool_call: RenderedToolCall | None = None


@dataclass(frozen=True)
class _ProtocolToolMatch:
    start: int
    end: int
    tag: str
    attrs: str
    body: str


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

    if tool_name.lower() == _PROTOCOL_TOOL_NAME:
        return "protocol", None

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

        # 1. Classify role and detect hook/protocol/bootstrap feedback
        role = _classify_render_role(msg)

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


_PROTOCOL_TOOL_TAGS: tuple[str, ...] = (
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
    "permissions instructions",
    "permission instructions",
    "collaboration_mode",
    "turn_aborted",
    "system_instructions",
    "instructions",
    "skills_instructions",
)

_INLINE_WRAPPER_PROTOCOL_TAGS: tuple[str, ...] = (
    "proposed_plan",
    "proposed_implementation",
    "search_quality_reflection",
)


def _tag_pattern(tags: tuple[str, ...]) -> str:
    return "|".join(re.escape(tag) for tag in tags)


_PROTOCOL_TOOL_TAG_PATTERN = _tag_pattern(_PROTOCOL_TOOL_TAGS)
_INLINE_WRAPPER_PROTOCOL_TAG_PATTERN = _tag_pattern(_INLINE_WRAPPER_PROTOCOL_TAGS)

_PROTOCOL_TAG_RE = re.compile(
    rf"<(?P<closing>/)?(?P<tag>{_PROTOCOL_TOOL_TAG_PATTERN})(?=[\s>])(?P<attrs>[^>]*)>",
    re.IGNORECASE,
)

_INLINE_WRAPPER_PROTOCOL_TAG_RE = re.compile(
    rf"</?(?:{_INLINE_WRAPPER_PROTOCOL_TAG_PATTERN})(?=[\s>])[^>]*>",
    re.IGNORECASE,
)

_PROTOCOL_CHILD_RE = re.compile(
    r"\s*<(?P<tag>[\w:-]+)>(?P<body>.*?)</(?P=tag)\s*>",
    re.DOTALL,
)
_MAX_PROTOCOL_PARSE_DEPTH = 50

_PROTOCOL_ATTR_RE = re.compile(r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_SYSTEM_BOOTSTRAP_PREFIX_RE = re.compile(
    r"^\s*(?:#\s*)?(?:AGENTS\.md instructions for\b|System instructions\b|Gobby Session ID:)",
    re.IGNORECASE,
)
_SYSTEM_BOOTSTRAP_HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s+)?(?P<heading>[^:#]+):?\s*$")
_SYSTEM_BOOTSTRAP_HEADINGS: frozenset[str] = frozenset(
    {
        "platform context",
        "capabilities",
        "lifecycle model",
        "behavior",
        "role",
        "personality",
        "values",
        "interaction style",
        "general",
        "tools",
        "working with the user",
        "formatting rules",
        "final answer instructions",
        "intermediary updates",
    }
)
_HIGH_SIGNAL_SYSTEM_BOOTSTRAP_HEADINGS: frozenset[str] = frozenset(
    {
        "platform context",
        "capabilities",
        "lifecycle model",
        "personality",
        "interaction style",
        "final answer instructions",
        "intermediary updates",
    }
)


def _sanitize_visible_protocol_text(content: str) -> str:
    """Strip inline wrapper tags while preserving the visible content."""
    if "<" not in content:
        return content
    content = _INLINE_WRAPPER_PROTOCOL_TAG_RE.sub("", content)
    return re.sub(r"\n{3,}", "\n\n", content)


def _count_bootstrap_heading_matches(content: str) -> tuple[int, int]:
    matched_headings: set[str] = set()
    matched_high_signal_headings: set[str] = set()

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading_match = _SYSTEM_BOOTSTRAP_HEADING_RE.match(line)
        if not heading_match:
            continue

        normalized_heading = heading_match.group("heading").strip().lower()
        if normalized_heading not in _SYSTEM_BOOTSTRAP_HEADINGS:
            continue

        matched_headings.add(normalized_heading)
        if normalized_heading in _HIGH_SIGNAL_SYSTEM_BOOTSTRAP_HEADINGS:
            matched_high_signal_headings.add(normalized_heading)

    return len(matched_headings), len(matched_high_signal_headings)


def _looks_like_system_bootstrap_text(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False

    if _SYSTEM_BOOTSTRAP_PREFIX_RE.match(stripped):
        return True

    heading_count, high_signal_heading_count = _count_bootstrap_heading_matches(stripped)
    return heading_count >= 3 or (heading_count >= 2 and high_signal_heading_count >= 1)


def _parse_protocol_attributes(attr_text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _PROTOCOL_ATTR_RE.finditer(attr_text):
        key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attrs[key] = value or ""
    return attrs


def _parse_protocol_payload(content: str, depth: int = 0) -> Any:
    content = content.strip()
    if not content or "<" not in content or depth >= _MAX_PROTOCOL_PARSE_DEPTH:
        return content

    parsed_children: dict[str, Any] = {}
    index = 0
    matched_child = False

    while index < len(content):
        match = _PROTOCOL_CHILD_RE.match(content, index)
        if not match:
            break

        matched_child = True
        child_tag = match.group("tag")
        child_value = _parse_protocol_payload(match.group("body"), depth + 1)
        existing = parsed_children.get(child_tag)
        if existing is None:
            parsed_children[child_tag] = child_value
        elif isinstance(existing, list):
            existing.append(child_value)
        else:
            parsed_children[child_tag] = [existing, child_value]
        index = match.end()

    if matched_child and not content[index:].strip():
        return parsed_children

    return content


def _find_matching_protocol_close(
    content: str, start_index: int, normalized_tag: str
) -> re.Match[str] | None:
    depth = 1
    for match in _PROTOCOL_TAG_RE.finditer(content, start_index):
        if match.group("tag").lower() != normalized_tag:
            continue

        if match.group("closing"):
            depth -= 1
            if depth == 0:
                return match
        else:
            depth += 1

    return None


def _iter_protocol_tool_matches(content: str) -> Iterable[_ProtocolToolMatch]:
    index = 0
    while match := _PROTOCOL_TAG_RE.search(content, index):
        if match.group("closing"):
            index = match.end()
            continue

        close_match = _find_matching_protocol_close(
            content, match.end(), match.group("tag").lower()
        )
        if close_match is None:
            index = match.end()
            continue

        yield _ProtocolToolMatch(
            start=match.start(),
            end=close_match.end(),
            tag=match.group("tag"),
            attrs=match.group("attrs"),
            body=content[match.end() : close_match.start()],
        )
        index = close_match.end()


def _make_protocol_tool_call(
    tag: str,
    body: str,
    attrs: str,
    source_index: int,
    ordinal: int,
) -> RenderedToolCall:
    normalized_tag = tag.lower()
    arguments: dict[str, Any] = {"tag": normalized_tag}
    parsed_attrs = _parse_protocol_attributes(attrs)
    if parsed_attrs:
        arguments["attributes"] = parsed_attrs

    result_content = _parse_protocol_payload(body)
    result_type = "json" if isinstance(result_content, (dict, list)) else "text"

    return RenderedToolCall(
        id=f"protocol-{source_index}-{ordinal}",
        tool_name=_PROTOCOL_TOOL_NAME,
        server_name="builtin",
        tool_type="protocol",
        arguments=arguments,
        result=ToolResult(
            content=result_content,
            content_type=result_type,
            metadata={"protocol_tag": normalized_tag},
        ),
        status="completed",
    )


def _make_plain_text_protocol_tool_call(
    tag: str,
    body: str,
    source_index: int,
) -> RenderedToolCall:
    return _make_protocol_tool_call(tag, body, "", source_index, 0)


def _extract_protocol_content_segments(
    content: str, source_index: int
) -> list[_ProtocolContentSegment]:
    if "<" not in content:
        return [_ProtocolContentSegment(kind="text", text=content)] if content else []

    segments: list[_ProtocolContentSegment] = []
    last_end = 0
    ordinal = 0

    for match in _iter_protocol_tool_matches(content):
        visible_text = _sanitize_visible_protocol_text(content[last_end : match.start]).rstrip()
        if visible_text.strip():
            segments.append(_ProtocolContentSegment(kind="text", text=visible_text))

        ordinal += 1
        segments.append(
            _ProtocolContentSegment(
                kind="protocol_tool",
                tool_call=_make_protocol_tool_call(
                    match.tag,
                    match.body,
                    match.attrs,
                    source_index,
                    ordinal,
                ),
            )
        )
        last_end = match.end

    trailing_text = _sanitize_visible_protocol_text(content[last_end:]).lstrip()
    if trailing_text.strip():
        segments.append(_ProtocolContentSegment(kind="text", text=trailing_text))

    if segments:
        return segments

    sanitized = _sanitize_visible_protocol_text(content)
    return [_ProtocolContentSegment(kind="text", text=sanitized)] if sanitized.strip() else []


def _is_protocol_only_text(content: str) -> bool:
    segments = _extract_protocol_content_segments(content, source_index=0)
    has_protocol_segment = False

    for segment in segments:
        if segment.kind == "protocol_tool":
            has_protocol_segment = True
            continue

        if segment.text and segment.text.strip():
            return False

    return has_protocol_segment


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
