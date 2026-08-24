from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Literal, TypeGuard

from gobby.sessions.transcripts.base import TokenUsage

ToolResultKind = Literal["text", "json", "image", "error"]

# RenderState fields its __deepcopy__ shares with the clone instead of copying.
# Only a grow-only record whose entries can never change how a record renders
# belongs here -- see RenderState.__deepcopy__ for why the resolved-id set
# qualifies. Everything not named is deep-copied, so a field added later is
# rollback-safe by default rather than silently reset (#20875).
_SHARED_RENDER_STATE_FIELDS = frozenset({"resolved_tool_call_ids"})


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
    # Ids of resolved calls -- all a resolved call leaves behind. Its only remaining
    # job is to make a duplicate tool_result be suppressed rather than rendered as
    # an orphan block, so it keeps that unconditionally, for any distance between
    # the record and its repeat. The payload it was carrying is not lost: the same
    # RenderedToolCall object is reachable through its message's content blocks,
    # which is where it belongs. Shared rather than copied on deepcopy -- see
    # __deepcopy__ -- so remembering every id costs the event loop nothing.
    resolved_tool_call_ids: set[str] = field(default_factory=set)
    # Track seen content hashes to deduplicate Claude Code streaming duplicates
    seen_content: set[int] = field(default_factory=set)

    def __deepcopy__(self, memo: dict[int, Any]) -> RenderState:
        """Copy everything a rollback can undo, and share what it cannot.

        The daemon deep-copies this state on its event loop once per transcript
        batch to have something to roll back to, so every field it copies is a
        per-batch cost (#20859). ``resolved_tool_call_ids`` is the one field a
        rollback has no reason to undo: it only ever grows, and an id in it can
        only suppress a duplicate tool_result, never change how a record renders.
        Re-feeding a rolled-back batch puts its calls back in
        ``pending_tool_calls``, which is checked first, so a stale id cannot
        shadow a genuine pairing either -- the ordering is documented where it
        lives, at the pairing checks in ``transcript_render_blocks``. Sharing it
        is what lets the suppression be unconditional -- remembering every id
        for the life of the session -- without the copy growing with the
        session.

        The field list comes from ``fields(self)`` with the explicit share list
        ``_SHARED_RENDER_STATE_FIELDS`` rather than a hand enumeration, so a
        field added later is deep-copied by default instead of silently
        resetting on rollback (#20875).
        """
        cls = type(self)
        clone = cls.__new__(cls)
        memo[id(self)] = clone
        for spec in fields(self):
            value = getattr(self, spec.name)
            if spec.name in _SHARED_RENDER_STATE_FIELDS:
                setattr(clone, spec.name, value)
            else:
                setattr(clone, spec.name, deepcopy(value, memo))
        return clone

    def remember_resolved_tool_call(self, tool_use_id: str) -> None:
        """Record that this call has been paired, so a repeat is not an orphan."""
        self.resolved_tool_call_ids.add(tool_use_id)

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
