from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, TypeGuard

from gobby.sessions.transcripts.base import TokenUsage

ToolResultKind = Literal["text", "json", "image", "error"]

# How many resolved tool calls a RenderState remembers so it can still recognise a
# duplicate tool_result. A duplicate is a streaming re-emit and arrives within a
# turn or two of the record it repeats, so a few hundred covers every one that
# happens while keeping this state a fixed size whatever the session's length --
# which is the whole point, since the daemon deep-copies it on the event loop once
# per transcript batch (#20859). Remembering them all is what put a copy growing to
# 47ms and beyond on that loop.
MAX_REMEMBERED_RESOLVED_CALLS = 512


@dataclass
class ToolResult:
    """Result of a tool execution.

    `kind` discriminates how `content` should be rendered: text bodies are
    plain strings, json bodies are dict/list payloads, image/error bodies
    follow tool-specific shapes. The frontend dispatches on `kind` rather
    than sniffing `typeof content`.
    """

    content: Any
    kind: ToolResultKind
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
class ContentBlock:
    """A block of content within a message."""

    type: str  # text, thinking, tool_chain, tool_reference, image, document, web_search_result, compaction_summary, unknown
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
    # Map of tool_use_id -> RenderedToolCall, genuinely awaiting a result. Entries
    # leave on resolution: the daemon deep-copies this state on its event loop once
    # per transcript batch, so anything held for the life of a session becomes a
    # growing tax on loop scheduling (#20859).
    pending_tool_calls: dict[str, RenderedToolCall] = field(default_factory=dict)
    # Map of tool_use_id -> message containing the call, for late result
    # re-broadcasts. Released with its call for the same reason.
    tool_call_messages: dict[str, RenderedMessage] = field(default_factory=dict)
    # Ids of recently resolved calls -- all a resolved call leaves behind. Its only
    # remaining job is to make a duplicate tool_result be suppressed rather than
    # rendered as an orphan block. The payload it was carrying is not lost: the
    # same RenderedToolCall object is reachable through its message's content
    # blocks, which is where it belongs. A dict rather than a set because the
    # insertion order is what makes the bound below evictable.
    resolved_tool_call_ids: dict[str, None] = field(default_factory=dict)
    # Track seen content hashes to deduplicate Claude Code streaming duplicates
    seen_content: set[int] = field(default_factory=set)

    def remember_resolved_tool_call(self, tool_use_id: str) -> None:
        """Record a resolved call's id, dropping the oldest once past the bound."""
        self.resolved_tool_call_ids.pop(tool_use_id, None)
        self.resolved_tool_call_ids[tool_use_id] = None
        while len(self.resolved_tool_call_ids) > MAX_REMEMBERED_RESOLVED_CALLS:
            del self.resolved_tool_call_ids[next(iter(self.resolved_tool_call_ids))]

    def knows_tool_call(self, tool_use_id: str | None) -> TypeGuard[str]:
        """Whether a result for this id belongs to a call rather than to nothing.

        A ``TypeGuard`` rather than a ``bool`` so the caller can index the maps
        afterwards: saying yes already means the id was not ``None``. It stays a
        ``TypeGuard`` rather than a ``TypeIs`` because the negative branch proves
        nothing -- an unrecognised id is still a perfectly good string.
        """
        if tool_use_id is None:
            return False
        return tool_use_id in self.pending_tool_calls or tool_use_id in self.resolved_tool_call_ids
