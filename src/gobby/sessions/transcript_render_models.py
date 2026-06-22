from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from gobby.sessions.transcripts.base import TokenUsage

ToolResultKind = Literal["text", "json", "image", "error"]


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
