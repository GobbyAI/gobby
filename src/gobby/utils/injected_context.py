"""Helpers for fencing and stripping Gobby-injected context."""

from __future__ import annotations

import re

INJECTED_CONTEXT_BEGIN = "<!-- gobby:injected-context:begin -->"
INJECTED_CONTEXT_END = "<!-- gobby:injected-context:end -->"

_LEGACY_MARKER_RE = re.compile(
    r"^[ \t]*\*Injected by Gobby\b[^\n]*\*[ \t]*$",
    re.MULTILINE,
)
_LEGACY_SECTION_END_RE = re.compile(
    r"(?:\n[ \t]*\n(?=^[ \t]*#{1,6}[ \t]+\S)|(?=^[ \t]*#{3}[ \t]+Turn\b)|"
    r"^(?:[-*_]{3,}[ \t]*)$)",
    re.MULTILINE,
)
_LEGACY_INJECTED_HEADING_RE = re.compile(
    r"(?:^|\n)[ \t]*#{1,6}[ \t]+(?:Previous Session Context|Continuation Context)[ \t]*\n"
    r"(?:[ \t]*\n)*\Z",
)


def strip_injected_context(text: str) -> str:
    """Remove Gobby-injected context blocks from transcript or summary text."""
    if (
        INJECTED_CONTEXT_BEGIN not in text
        and INJECTED_CONTEXT_END not in text
        and "Injected by Gobby" not in text
    ):
        return text

    stripped = _strip_sentinel_blocks(text)
    stripped = _strip_legacy_marker_blocks(stripped)
    return _normalize_stripped_text(stripped)


def _strip_sentinel_blocks(text: str) -> str:
    parts: list[str] = []
    cursor = 0

    while cursor < len(text):
        begin_index = text.find(INJECTED_CONTEXT_BEGIN, cursor)
        end_index = text.find(INJECTED_CONTEXT_END, cursor)

        if begin_index == -1 and end_index == -1:
            parts.append(text[cursor:])
            break

        if end_index != -1 and (begin_index == -1 or end_index < begin_index):
            parts.clear()
            cursor = end_index + len(INJECTED_CONTEXT_END)
            continue

        parts.append(text[cursor:begin_index])
        matching_end = text.find(INJECTED_CONTEXT_END, begin_index + len(INJECTED_CONTEXT_BEGIN))
        if matching_end == -1:
            break
        cursor = matching_end + len(INJECTED_CONTEXT_END)
        if (
            begin_index > 0
            and text[begin_index - 1] == "\n"
            and cursor < len(text)
            and text[cursor] == "\n"
        ):
            cursor += 1

    return "".join(parts)


def _strip_legacy_marker_blocks(text: str) -> str:
    while True:
        marker_match = _LEGACY_MARKER_RE.search(text)
        if not marker_match:
            return text

        start = _legacy_block_start(text, marker_match.start())
        end_match = _LEGACY_SECTION_END_RE.search(text, marker_match.end())
        end = end_match.start() if end_match else len(text)
        text = text[:start] + text[end:]


def _legacy_block_start(text: str, marker_start: int) -> int:
    prefix = text[:marker_start]
    heading_match = _LEGACY_INJECTED_HEADING_RE.search(prefix)
    if heading_match:
        return heading_match.start() + (1 if heading_match.group(0).startswith("\n") else 0)
    return marker_start


def _normalize_stripped_text(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
