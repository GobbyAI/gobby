"""Compact one-line rendering of surfaced memories."""

from __future__ import annotations

from typing import Any

# Enough of each field to judge a hit without reading the memory; the agent
# fetches the full text with get_memory before acting on one.
LEAD_CHARS = 160

_INDEX_INSTRUCTION = (
    'Fetch full text before acting on a match: gobby-memory:get_memory(memory_id="<id>")'
)


def _lead(value: Any) -> str:
    """Collapse one field onto one line and truncate only between words."""
    text = " ".join(str(value or "").split())
    if len(text) <= LEAD_CHARS:
        return text
    prefix = text[: LEAD_CHARS - 1]
    boundary = prefix.rfind(" ")
    if boundary <= 0:
        return "…"
    prefix = prefix[:boundary]
    return f"{prefix}…"


def format_memory_index(trigger: str, memories: list[dict[str, Any]]) -> str:
    """Render surfaced memories as a ranked one-line-per-hit index.

    Scores are deliberately absent: the live corpus band is too narrow for an
    agent to read one usefully, so the line carries provenance instead — which
    searches found the hit, when it was last updated, and when it applies.
    """
    if not memories:
        return ""
    lines = [f'<memory-index trigger="{trigger}">']
    for rank, memory in enumerate(memories, start=1):
        memory_id = str(memory.get("id") or "")[:8]
        memory_type = memory.get("type") or "memory"
        searches = str(memory.get("search_via") or "unknown").replace("|", "+")
        updated = str(memory.get("updated_at") or "")[:10] or "unknown"
        line = (
            f"{rank}. {memory_id} [{memory_type}; {searches}; updated {updated}] "
            f"{_lead(memory.get('content'))}"
        )
        rationale = memory.get("rationale")
        if rationale:
            line = f"{line} | when: {_lead(rationale)}"
        lines.append(line)
    lines.append(_INDEX_INSTRUCTION)
    lines.append("</memory-index>")
    return "\n".join(lines)
