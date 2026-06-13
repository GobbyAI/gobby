"""Shared utilities for Claude Agent SDK integration.

Functions extracted from claude_streaming.py, chat_session.py, and
chat_session_helpers.py to eliminate duplication across SDK consumers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping


def sanitize_error(e: Exception) -> str:
    """Return a user-facing error message, hiding internal library details."""
    msg = str(e)
    if "model isn't mapped" in msg or "custom_llm_provider" in msg:
        return "An internal error occurred. Please try again."
    return msg


def parse_server_name(full_tool_name: str) -> str:
    """Extract server name from mcp__{server}__{tool} format."""
    if full_tool_name.startswith("mcp__"):
        parts = full_tool_name.split("__")
        if len(parts) >= 2:
            return parts[1]
    return "builtin"


def format_exception_group(eg: ExceptionGroup) -> str:
    """Format an ExceptionGroup into a semicolon-separated error string."""
    errors = [sanitize_error(exc) for exc in eg.exceptions]
    return "; ".join(errors)


# Claude Code / Agent SDK hard-truncates additionalContext at 10K chars.
# We cap slightly below to avoid the ugly "... [output truncated]" suffix.
ADDITIONAL_CONTEXT_LIMIT = 9_950

# Budget for a single large handoff/summary contributor injected inline via
# additionalContext. Kept well below ADDITIONAL_CONTEXT_LIMIT so the other
# contributors (task context, user profile, metadata, system message) and the
# breadcrumb still fit under the SDK's 10K aggregate ceiling. The full,
# untruncated summary stays available on demand via the get_handoff_context
# MCP tool, so this only bounds the inline copy — it never drops content.
HANDOFF_SUMMARY_INJECT_BUDGET: int = 6_500


def head_with_breadcrumb(text: str, *, budget: int, breadcrumb: str) -> str:
    """Return ``text`` bounded to ``budget``, appending ``breadcrumb`` when cut.

    Truncates at a clean boundary — the last blank-line break, else the last
    newline, before ``budget`` — so the injected head never ends mid-sentence.
    When ``text`` already fits within ``budget`` it is returned verbatim with no
    breadcrumb. The breadcrumb should tell the reader how to retrieve the full
    text (e.g. via the get_handoff_context MCP tool).
    """
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text

    suffix = f"\n\n{breadcrumb}" if breadcrumb else ""
    head_budget = budget - len(suffix)
    if head_budget <= 0:
        return (breadcrumb or text)[:budget]

    # Require clean cuts to keep at least half of the head budget. This prevents
    # the breadcrumb from crowding out content and falls back to a hard cut when
    # paragraph/newline boundaries are too close to the start.
    min_clean_cut = head_budget // 2
    # Prefer a paragraph boundary; this is the cleanest place to cut pasted
    # handoff/summary prose.
    cut = text.rfind("\n\n", 0, head_budget)
    if cut == -1:
        # Fall back to a single newline only when it still leaves a meaningful
        # head. ``-1`` means no newline was found inside the budget.
        newline = text.rfind("\n", 0, head_budget)
        cut = newline if newline > min_clean_cut else head_budget
    elif cut < min_clean_cut:
        # A paragraph break too close to the start would waste most of the
        # budget, so try the last line break before hard cutting.
        newline = text.rfind("\n", 0, head_budget)
        cut = newline if newline > min_clean_cut else head_budget
    head = text[:cut].rstrip()
    if not head:
        return (breadcrumb or text)[:budget]
    return f"{head}{suffix}"


def _split_contributors(text: str, contributor_sizes: Mapping[str, int]) -> list[str] | None:
    parts: list[str] = []
    cursor = 0
    for size in contributor_sizes.values():
        if size < 0:
            return None
        parts.append(text[cursor : cursor + size])
        cursor += size
        if cursor < len(text):
            if text[cursor : cursor + 2] != "\n\n":
                return None
            cursor += 2
    return parts if cursor == len(text) else None


def _truncate_contributors(text: str, contributor_sizes: Mapping[str, int]) -> str | None:
    parts = _split_contributors(text, contributor_sizes)
    if not parts:
        return None
    marker = "\n... [truncated]"
    separator_budget = 2 * (len(parts) - 1)
    content_budget = ADDITIONAL_CONTEXT_LIMIT - len(marker) - separator_budget
    if content_budget < 0:
        return None
    allocations = [len(part) for part in parts]
    allocated = sum(allocations)
    while allocated > content_budget:
        index = max(range(len(allocations)), key=allocations.__getitem__)
        reduction = min(allocations[index], allocated - content_budget)
        allocations[index] -= reduction
        allocated -= reduction
    return (
        "\n\n".join(part[:budget] for part, budget in zip(parts, allocations, strict=True)) + marker
    )


def truncate_additional_context(
    text: str,
    *,
    contributor_sizes: Mapping[str, int] | None = None,
    logger: logging.Logger | None = None,
) -> str:
    """Truncate text to fit within the SDK's additionalContext limit.

    Truncation only — no compression, no mutation. Contributors (skills,
    memory, inject_context effects, metadata lines) are expected to emit
    payloads that fit within the aggregate limit; this function is the
    final safety net.
    """
    if len(text) <= ADDITIONAL_CONTEXT_LIMIT:
        return text
    if logger:
        logger.warning(
            "additionalContext truncated aggregate_len=%d limit=%d contributors=%s",
            len(text),
            ADDITIONAL_CONTEXT_LIMIT,
            dict(contributor_sizes or {}),
        )
    if contributor_sizes:
        truncated = _truncate_contributors(text, contributor_sizes)
        if truncated is not None:
            return truncated
    return text[: ADDITIONAL_CONTEXT_LIMIT - 16] + "\n... [truncated]"
