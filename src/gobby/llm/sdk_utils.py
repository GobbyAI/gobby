"""Shared utilities for Claude Agent SDK integration.

Functions extracted from claude_streaming.py, chat_session.py, and
chat_session_helpers.py to eliminate duplication across SDK consumers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace


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
# Live overrides live in hooks.additional_context_limit / additional_context_limits.
ADDITIONAL_CONTEXT_LIMIT = 9_950

# Reserved under the ship limit for overflow instruction + adapter metadata.
INLINE_CONTEXT_HEADROOM = 450

# Reserved under the ship limit for first-prompt preamble, task/wiki/skill
# companions, metadata, and the handoff breadcrumb.
HANDOFF_COMPANION_RESERVE = 5_450

# Budget for a single large handoff/summary contributor injected inline via
# additionalContext. Derived from the default ship limit minus companion reserve.
# The full summary stays available on demand via get_handoff_context.
HANDOFF_SUMMARY_INJECT_BUDGET: int = ADDITIONAL_CONTEXT_LIMIT - HANDOFF_COMPANION_RESERVE


@dataclass(frozen=True, slots=True)
class MarkdownSection:
    """One preamble or level-two Markdown section."""

    title: str
    display_title: str
    order: int
    heading: str
    body: str
    trimmed: bool = False

    @property
    def text(self) -> str:
        """Render this section with its original heading."""
        return f"{self.heading}{self.body}"


@dataclass(frozen=True, slots=True)
class BudgetResult:
    """Section allocation result in original document order."""

    sections: tuple[MarkdownSection, ...]
    omitted_titles: tuple[str, ...]

    @property
    def text(self) -> str:
        """Render kept sections in original document order."""
        return "".join(section.text for section in self.sections)


_SECTION_HEADING_RE = re.compile(r"^##(?:[ \t]+(?P<title>.*?)[ \t]*|[ \t]*)\r?\n?$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
MANDATORY_HANDOFF_SECTION_TITLES = ("next steps", "current state")
_SECTION_TRIM_MARKER = "\n\n[section trimmed]\n"
_PREAMBLE_PRIORITY = 25


def split_markdown_sections(text: str) -> list[MarkdownSection]:
    """Split text into a preamble and fence-aware level-two sections."""
    sections: list[MarkdownSection] = []
    heading = ""
    display_title = "Preamble"
    title = ""
    body: list[str] = []
    order = 0
    fence: str | None = None

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" \t")
        if fence is not None:
            body.append(line)
            if stripped.startswith(fence):
                fence = None
            continue

        fence_match = _FENCE_RE.match(line)
        if fence_match is not None:
            body.append(line)
            fence = fence_match.group(1)
            continue

        heading_match = _SECTION_HEADING_RE.match(line)
        if heading_match is None:
            body.append(line)
            continue

        sections.append(
            MarkdownSection(
                title=title,
                display_title=display_title,
                order=order,
                heading=heading,
                body="".join(body),
            )
        )
        order += 1
        heading = line
        display_title = (heading_match.group("title") or "").strip()
        title = display_title.casefold()
        body = []

    sections.append(
        MarkdownSection(
            title=title,
            display_title=display_title,
            order=order,
            heading=heading,
            body="".join(body),
        )
    )
    return sections


def _clean_cut(text: str, budget: int) -> str:
    """Cut text at the last useful paragraph or newline boundary."""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text

    min_clean_cut = budget // 2
    cut = text.rfind("\n\n", 0, budget)
    if cut == -1:
        newline = text.rfind("\n", 0, budget)
        cut = newline if newline > min_clean_cut else budget
    elif cut < min_clean_cut:
        newline = text.rfind("\n", 0, budget)
        cut = newline if newline > min_clean_cut else budget
    return text[:cut].rstrip()


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

    head = _clean_cut(text, head_budget)
    if not head:
        return (breadcrumb or text)[:budget]
    return f"{head}{suffix}"


def _mandatory_section_owners(sections: list[MarkdownSection]) -> list[MarkdownSection]:
    owners: list[MarkdownSection] = []
    seen: set[str] = set()
    for section in sections:
        if section.title in MANDATORY_HANDOFF_SECTION_TITLES and section.title not in seen:
            owners.append(section)
            seen.add(section.title)
    return owners


def _allocate_mandatory_body_budgets(
    sections: list[MarkdownSection],
    budget: int,
    min_section_chars: int,
) -> list[int]:
    lengths = [len(section.body) for section in sections]
    minimums = [min(length, min_section_chars) for length in lengths]
    if sum(minimums) > budget:
        total_length = sum(lengths)
        allocations: list[int] = []
        remaining = budget
        for index, length in enumerate(lengths):
            share = remaining if index == len(lengths) - 1 else budget * length // total_length
            allocation = min(length, share)
            allocations.append(allocation)
            remaining -= allocation
        return allocations

    allocations = minimums.copy()
    remaining = budget - sum(allocations)
    while remaining > 0:
        active = [index for index, length in enumerate(lengths) if allocations[index] < length]
        if not active:
            break
        total_weight = sum(lengths[index] for index in active)
        allocated_this_round = 0
        for index in active:
            capacity = lengths[index] - allocations[index]
            share = max(1, remaining * lengths[index] // total_weight)
            addition = min(capacity, share, remaining - allocated_this_round)
            allocations[index] += addition
            allocated_this_round += addition
            if allocated_this_round == remaining:
                break
        remaining -= allocated_this_round
    return allocations


def _trim_section(
    section: MarkdownSection,
    budget: int,
    *,
    min_body_chars: int = 0,
) -> MarkdownSection | None:
    body_budget = budget - len(section.heading) - len(_SECTION_TRIM_MARKER)
    if body_budget < 0:
        return None

    body = _clean_cut(section.body, body_budget)
    required_body_chars = min(len(section.body), min_body_chars, body_budget)
    if len(body) < required_body_chars:
        body = section.body[:required_body_chars]
    stripped_body = body.rstrip()
    if len(stripped_body) >= required_body_chars:
        body = stripped_body
    return replace(
        section,
        body=f"{body}{_SECTION_TRIM_MARKER}",
        trimmed=True,
    )


def _section_priority(
    section: MarkdownSection,
    priorities: Mapping[str, int],
    mandatory_owner_orders: set[int],
    unknown_priority: int,
) -> int:
    if section.title == "":
        return _PREAMBLE_PRIORITY
        if (
            section.title in MANDATORY_HANDOFF_SECTION_TITLES
            and section.order not in mandatory_owner_orders
        ):
            return unknown_priority
    return priorities.get(section.title, unknown_priority)


def _optional_sections_by_priority(
    sections: list[MarkdownSection],
    priorities: Mapping[str, int],
    mandatory_owner_orders: set[int],
    unknown_priority: int,
) -> Iterator[MarkdownSection]:
    buckets: dict[int, list[MarkdownSection]] = {}
    for section in sections:
        if section.order in mandatory_owner_orders:
            continue
        priority = _section_priority(
            section,
            priorities,
            mandatory_owner_orders,
            unknown_priority,
        )
        buckets.setdefault(priority, []).append(section)
    for priority in sorted(buckets):
        yield from buckets[priority]


def _budget_result(
    sections: list[MarkdownSection],
    kept: list[MarkdownSection],
) -> BudgetResult:
    kept_by_order = {section.order: section for section in kept}
    ordered_kept = tuple(kept_by_order[order] for order in sorted(kept_by_order))
    omitted_titles = tuple(
        section.display_title
        for section in sections
        if section.text
        and (section.order not in kept_by_order or kept_by_order[section.order].trimmed)
    )
    return BudgetResult(sections=ordered_kept, omitted_titles=omitted_titles)


def allocate_section_budget(
    sections: list[MarkdownSection],
    priorities: Mapping[str, int],
    budget: int,
    *,
    min_section_chars: int = 200,
    unknown_priority: int = 60,
) -> BudgetResult:
    """Allocate a character budget while preserving mandatory handoff sections."""
    if budget <= 0:
        return _budget_result(sections, [])

    mandatory = _mandatory_section_owners(sections)
    mandatory_orders = {section.order for section in mandatory}
    marker_reserve = sum(len(section.heading) + len(_SECTION_TRIM_MARKER) for section in mandatory)
    if marker_reserve > budget:
        return _budget_result(sections, [])

    mandatory_body_budget = budget - marker_reserve
    mandatory_bodies_fit = sum(len(section.body) for section in mandatory) <= (
        mandatory_body_budget
    )
    if not mandatory_bodies_fit:
        body_budgets = _allocate_mandatory_body_budgets(
            mandatory,
            mandatory_body_budget,
            min_section_chars,
        )
        trimmed_mandatory = [
            trimmed
            for section, body_budget in zip(mandatory, body_budgets, strict=True)
            if (
                trimmed := _trim_section(
                    section,
                    len(section.heading) + len(_SECTION_TRIM_MARKER) + body_budget,
                    min_body_chars=min(len(section.body), min_section_chars),
                )
            )
            is not None
        ]
        return _budget_result(sections, trimmed_mandatory)

    kept = mandatory.copy()
    remaining = budget - sum(len(section.text) for section in kept)
    for section in _optional_sections_by_priority(
        sections,
        priorities,
        mandatory_orders,
        unknown_priority,
    ):
        if len(section.text) <= remaining:
            kept.append(section)
            remaining -= len(section.text)
            continue
        if remaining >= min_section_chars:
            trimmed = _trim_section(section, remaining)
            if trimmed is not None:
                kept.append(trimmed)
        break

    return _budget_result(sections, kept)


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


def _truncate_contributors(
    text: str,
    contributor_sizes: Mapping[str, int],
    *,
    limit: int,
) -> str | None:
    parts = _split_contributors(text, contributor_sizes)
    if not parts:
        return None
    marker = "\n... [truncated]"
    separator_budget = 2 * (len(parts) - 1)
    content_budget = limit - len(marker) - separator_budget
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
    limit: int | None = None,
) -> str:
    """Truncate text to fit within the additionalContext ship limit.

    Truncation only — no compression, no mutation. Contributors (skills,
    memory, inject_context effects, metadata lines) are expected to emit
    payloads that fit within the aggregate limit; this function is the
    final safety net.
    """
    ship_limit = ADDITIONAL_CONTEXT_LIMIT if limit is None else limit
    if len(text) <= ship_limit:
        return text
    if logger:
        logger.warning(
            "additionalContext truncated aggregate_len=%d limit=%d contributors=%s",
            len(text),
            ship_limit,
            dict(contributor_sizes or {}),
        )
    if contributor_sizes:
        truncated = _truncate_contributors(text, contributor_sizes, limit=ship_limit)
        if truncated is not None:
            return truncated
    marker = "\n... [truncated]"
    keep = max(0, ship_limit - len(marker))
    return text[:keep] + marker
