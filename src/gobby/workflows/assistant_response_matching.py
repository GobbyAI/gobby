"""Find listed phrases in an assistant response, ignoring cited spans."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS",
    "ASSISTANT_RESPONSE_SCAN_LIMIT",
    "assistant_response_text",
    "first_response_match",
]

logger = logging.getLogger(__name__)

ASSISTANT_RESPONSE_SCAN_LIMIT = 8000
ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS = (
    r"\bnot\b\s+[^.!?\n,;:]{1,80}?,\s*\bnot\b\s+[^.!?\n,;:]{1,80}?,\s*\bbut\b\s+[^.!?\n]{1,120}",
    r"\bnot\b\s+[^.!?\n,;:]{1,120}?,\s*\bbut\b\s+[^.!?\n]{1,120}",
    r"\b(?!not\b)[A-Z0-9][^.!?\n,;:]{1,120}?,\s*\bnot\b\s+[^.!?\n]{1,120}",
)
_ASSISTANT_RESPONSE_REGEX_ALLOWLIST = frozenset(ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS)
# Fenced blocks, inline code, and double-quoted spans cite text instead of
# asserting it; masking keeps offsets so matches still slice the original.
_ASSISTANT_RESPONSE_CITED_SPAN = re.compile(
    r"```.*?(?:```|\Z)|`[^`\n]+`|\"[^\"\n]*\"|“[^”\n]*”", re.DOTALL
)
_ASSISTANT_RESPONSE_TEXT_KEYS = (
    "response",
    "assistant_response",
    "assistant_response_text",
    "prompt_response",
    "last_assistant_message",
    "last_assistant_content",
    "output",
    "message",
    "content",
    "log",
)
_NESTED_TEXT_KEYS = (
    "text",
    "content",
    "message",
    "response",
    "output",
)


def _coerce_response_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return "\n".join(text for item in value if (text := _coerce_response_text(item).strip()))
    if isinstance(value, dict):
        parts = [
            text
            for key in _NESTED_TEXT_KEYS
            if (text := _coerce_response_text(value.get(key)).strip())
        ]
        return "\n".join(parts)
    return ""


def assistant_response_text(data: Mapping[str, Any], variables: Mapping[str, Any]) -> str:
    """Return the assistant response from event data, else from session variables."""
    for key in _ASSISTANT_RESPONSE_TEXT_KEYS:
        if text := _coerce_response_text(data.get(key)).strip():
            return text
    for key in ("last_assistant_response", "last_assistant_message"):
        if text := _coerce_response_text(variables.get(key)).strip():
            return text
    return ""


def _mask_cited_spans(text: str) -> str:
    """Blank cited spans with same-length filler so offsets map to ``text``."""
    return _ASSISTANT_RESPONSE_CITED_SPAN.sub(
        lambda match: re.sub(r"[^\n]", "x", match.group(0)), text
    )


def first_response_match(text: str, patterns: Sequence[Any], regex: bool = False) -> str | None:
    """Return the first fragment of ``text`` that a pattern matches outside cited spans.

    Plain patterns match case-insensitively as substrings. Regex patterns must be
    bundled contrastive patterns; any other regex is skipped.
    """
    if not text:
        return None
    haystack = text[:ASSISTANT_RESPONSE_SCAN_LIMIT]
    masked = _mask_cited_spans(haystack)

    if not regex:
        lowered = masked.lower()
        for pattern in patterns:
            if not isinstance(pattern, str) or pattern == "":
                continue
            index = lowered.find(pattern.lower())
            if index >= 0:
                return haystack[index : index + len(pattern)]
        return None

    for pattern in patterns:
        if not isinstance(pattern, str) or pattern not in _ASSISTANT_RESPONSE_REGEX_ALLOWLIST:
            continue
        try:
            match = re.search(pattern, masked, re.IGNORECASE)
        except re.error:
            logger.warning("Invalid bundled assistant response regex skipped: %s", pattern)
            continue
        if match:
            return haystack[match.start() : match.end()]
    return None
