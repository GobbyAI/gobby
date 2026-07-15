"""Normalization helpers for Claude transcript metadata records."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage, _unknown_block_message

ClaudeRole = Literal["assistant", "user"]


@dataclass(frozen=True)
class ClaudeMessageBuilder:
    """Build messages that share one Claude transcript record's metadata."""

    index: int
    timestamp: datetime
    data: dict[str, Any]
    usage: TokenUsage | None
    model: str | None
    message_id: str | None

    def make(
        self,
        *,
        role: str,
        content: str | dict[str, Any],
        content_type: str = "text",
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_result: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
    ) -> ParsedMessage:
        return ParsedMessage(
            index=self.index,
            role=role,
            content=content,
            content_type=content_type,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            timestamp=self.timestamp,
            raw_json=self.data,
            usage=self.usage,
            tool_use_id=tool_use_id,
            model=self.model,
            message_id=self.message_id,
        )

    def unknown(self, *, role: ClaudeRole, block_type: str, raw: dict[str, Any]) -> ParsedMessage:
        return _unknown_block_message(
            index=self.index,
            block_type=block_type,
            raw=raw,
            role=role,
            timestamp=self.timestamp,
            message_id=self.message_id,
            model=self.model,
            usage=self.usage,
        )


def _media_source(block: dict[str, Any], block_type: str) -> dict[str, Any]:
    source = block.get("source")
    normalized = dict(source) if isinstance(source, dict) else {"data": str(source or "")}
    title = block.get("title")
    if block_type == "document" and isinstance(title, str) and title:
        normalized.setdefault("name", title)
    return normalized


def extract_tool_result_content(block: dict[str, Any]) -> str:
    """Extract string content from a tool_result block."""
    content = block.get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else str(content)
    return content if isinstance(content, str) else str(content)


def expand_message_content(
    builder: ClaudeMessageBuilder,
    content: Any,
    *,
    role: ClaudeRole,
) -> list[ParsedMessage]:
    """Normalize one Claude message's content blocks into rendered messages."""
    if not isinstance(content, list):
        return [builder.make(role=role, content=str(content))]

    results: list[ParsedMessage] = []
    text_parts: list[str] = []
    thinking_parts: list[str] = []

    def flush_text() -> None:
        if text_parts:
            results.append(builder.make(role=role, content=" ".join(text_parts)))
            text_parts.clear()

    def flush_thinking() -> None:
        if thinking_parts:
            results.append(
                builder.make(
                    role="assistant",
                    content="\n".join(thinking_parts),
                    content_type="thinking",
                )
            )
            thinking_parts.clear()

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type in ("image", "document"):
            flush_text()
            results.append(
                builder.make(
                    role=role,
                    content=_media_source(block, block_type),
                    content_type=block_type,
                )
            )
        elif role == "user" and block_type == "tool_result":
            flush_text()
            result_content = extract_tool_result_content(block)
            results.append(
                builder.make(
                    role="user",
                    content=result_content,
                    content_type="tool_result",
                    tool_result={
                        "content": result_content,
                        "is_error": block.get("is_error", False),
                    },
                    tool_use_id=block.get("tool_use_id"),
                )
            )
        elif role == "assistant" and block_type == "tool_use":
            flush_text()
            results.append(
                builder.make(
                    role="assistant",
                    content="",
                    content_type="tool_use",
                    tool_name=block.get("name"),
                    tool_input=block.get("input"),
                    tool_use_id=block.get("id"),
                )
            )
        elif role == "assistant" and block_type == "thinking":
            value = block.get("thinking") or ""
            if value.strip():
                thinking_parts.append(value)
        elif role == "assistant" and block_type == "fallback":
            results.append(builder.make(role="system", content=fallback_content(block)))
        else:
            flush_text()
            flush_thinking()
            results.append(
                builder.unknown(
                    role=role,
                    block_type=str(block_type or "<missing>"),
                    raw=block,
                )
            )

    flush_text()
    flush_thinking()
    return results


def fallback_content(block: dict[str, Any]) -> str:
    """Describe an assistant fallback block."""
    source = block.get("from")
    target = block.get("to")
    source_model = source.get("model") if isinstance(source, dict) else None
    target_model = target.get("model") if isinstance(target, dict) else None
    return f"Model fallback: {source_model or 'unknown'} -> {target_model or 'unknown'}"


def system_event_content(data: dict[str, Any]) -> str | None:
    """Return display text for counted Claude system events."""
    subtype = data.get("subtype")
    if subtype == "api_error":
        error = data.get("error")
        if isinstance(error, dict):
            detail = error.get("formatted") or error.get("message")
        else:
            detail = error
        return str(detail or "API error")
    if subtype == "model_refusal_fallback":
        return str(data.get("content") or "Model refusal fallback")
    return None
