from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from gobby.sessions.transcript_render_models import (
    RenderedToolCall,
    ToolResult,
    ToolResultKind,
)
from gobby.sessions.transcript_tool_metadata import _PROTOCOL_TOOL_NAME


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
_PROTOCOL_CODE_SPAN_RE = re.compile(r"```(?:.*?```|.*\Z)|`[^`\n]*(?:`|$)", re.DOTALL)

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


def _find_protocol_protected_ranges(content: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _PROTOCOL_CODE_SPAN_RE.finditer(content)]


def _is_protected_protocol_index(index: int, ranges: list[tuple[int, int]]) -> bool:
    # Assumes ranges are sorted by start (re.finditer yields matches in order).
    for start, end in ranges:
        if index < start:
            return False
        if index < end:
            return True

    return False


def _find_matching_protocol_close(
    content: str,
    start_index: int,
    normalized_tag: str,
    protected_ranges: list[tuple[int, int]],
) -> re.Match[str] | None:
    depth = 1
    for match in _PROTOCOL_TAG_RE.finditer(content, start_index):
        if _is_protected_protocol_index(match.start(), protected_ranges):
            continue

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
    protected_ranges = _find_protocol_protected_ranges(content)
    index = 0
    while match := _PROTOCOL_TAG_RE.search(content, index):
        if _is_protected_protocol_index(match.start(), protected_ranges):
            index = match.end()
            continue

        if match.group("closing"):
            index = match.end()
            continue

        close_match = _find_matching_protocol_close(
            content, match.end(), match.group("tag").lower(), protected_ranges
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
    result_kind: ToolResultKind = "json" if isinstance(result_content, (dict, list)) else "text"

    return RenderedToolCall(
        id=f"protocol-{source_index}-{ordinal}",
        tool_name=_PROTOCOL_TOOL_NAME,
        server_name="builtin",
        tool_type="protocol",
        arguments=arguments,
        result=ToolResult(
            content=result_content,
            kind=result_kind,
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


def _append_visible_protocol_segment(
    segments: list[_ProtocolContentSegment],
    text: str,
    source_index: int,
    ordinal: int,
) -> int:
    if not text.strip():
        return ordinal

    if _looks_like_system_bootstrap_text(text):
        ordinal += 1
        segments.append(
            _ProtocolContentSegment(
                kind="protocol_tool",
                tool_call=_make_protocol_tool_call(
                    "system_instructions",
                    text.strip(),
                    "",
                    source_index,
                    ordinal,
                ),
            )
        )
        return ordinal

    segments.append(_ProtocolContentSegment(kind="text", text=text))
    return ordinal


def _extract_protocol_content_segments(
    content: str, source_index: int
) -> list[_ProtocolContentSegment]:
    if "<" not in content and not _looks_like_system_bootstrap_text(content):
        return [_ProtocolContentSegment(kind="text", text=content)] if content else []

    segments: list[_ProtocolContentSegment] = []
    last_end = 0
    ordinal = 0

    for match in _iter_protocol_tool_matches(content):
        visible_text = _sanitize_visible_protocol_text(content[last_end : match.start]).rstrip()
        ordinal = _append_visible_protocol_segment(
            segments,
            visible_text,
            source_index,
            ordinal,
        )

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
    ordinal = _append_visible_protocol_segment(
        segments,
        trailing_text,
        source_index,
        ordinal,
    )

    if segments:
        return segments

    sanitized = _sanitize_visible_protocol_text(content)
    fallback_segments: list[_ProtocolContentSegment] = []
    _append_visible_protocol_segment(fallback_segments, sanitized, source_index, 0)
    return fallback_segments


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
