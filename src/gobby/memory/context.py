from __future__ import annotations

import re

from gobby.storage.memories import Memory

# Pattern to match common bullet markers at start of string
_BULLET_PATTERN = re.compile(r"^[\s]*[-*•]\s*")


def _strip_leading_bullet(content: str) -> str:
    """
    Strip leading bullet points and whitespace from content.

    Handles common bullet markers: -, *, •
    Also strips any leading/trailing whitespace.

    Returns empty string if content is empty or only whitespace/bullets.
    """
    # Strip outer whitespace first
    content = content.strip()
    if not content:
        return ""

    # Remove leading bullet marker if present
    result = _BULLET_PATTERN.sub("", content)
    return result.strip()


def format_memory_metadata_suffix(
    memory_id: str | None,
    *,
    rationale: str | None = None,
    score: int | float | None = None,
    via: str | None = None,
) -> str:
    """Format prompt-visible metadata for rendered project memories.

    Args:
        memory_id: Memory identifier to include when present.
        rationale: Optional writer's claim, rendered as `why:` after memory_id.
        score: Optional relevance score; numeric values are rendered to 4 decimals.
        via: Optional source path for how the memory was selected.

    Returns:
        Parenthesized metadata suffix, or an empty string when no fields are present.
    """
    fields = []
    if memory_id:
        fields.append(f"memory_id: {memory_id}")
    if rationale:
        fields.append(f"why: {rationale}")
    if score is not None:
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            fields.append(f"score: {score:.4f}")
    if via:
        fields.append(f"via: {via}")
    if not fields:
        return ""
    return f" ({', '.join(fields)})"


def build_memory_context(memories: list[Memory]) -> str:
    """
    Build a formatted markdown context string from memories.

    Args:
        memories: List of Memory objects to include

    Returns:
        Formatted markdown string wrapped in <project-memory> tags
    """
    if not memories:
        return ""

    parts = ["<project-memory>"]

    # Group memories by type
    context_memories = [m for m in memories if m.memory_type == "context"]
    pref_memories = [m for m in memories if m.memory_type == "preference"]
    pattern_memories = [m for m in memories if m.memory_type == "pattern"]
    fact_memories = [m for m in memories if m.memory_type == "fact"]

    # 1. Project Context
    if context_memories:
        parts.append("## Project Context")
        for mem in context_memories:
            parts.append(
                f"{mem.content}{format_memory_metadata_suffix(mem.id, rationale=mem.rationale)}"
            )
        parts.append("")

    # 2. Preferences
    if pref_memories:
        parts.append("## Preferences")
        for mem in pref_memories:
            content = _strip_leading_bullet(mem.content)
            if content:  # Skip empty content
                parts.append(
                    f"- {content}{format_memory_metadata_suffix(mem.id, rationale=mem.rationale)}"
                )
        parts.append("")

    # 3. Patterns
    if pattern_memories:
        parts.append("## Patterns")
        for mem in pattern_memories:
            content = _strip_leading_bullet(mem.content)
            if content:  # Skip empty content
                parts.append(
                    f"- {content}{format_memory_metadata_suffix(mem.id, rationale=mem.rationale)}"
                )
        parts.append("")

    # 4. Facts/Other
    if fact_memories:
        parts.append("## Facts")
        for mem in fact_memories:
            content = _strip_leading_bullet(mem.content)
            if content:  # Skip empty content
                parts.append(
                    f"- {content}{format_memory_metadata_suffix(mem.id, rationale=mem.rationale)}"
                )
        parts.append("")

    parts.append("</project-memory>")

    return "\n".join(parts)
