"""Validation helpers for digest-generated session titles."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

LIFECYCLE_CMDS = ("/clear", "/exit", "/compact")
_MAX_SESSION_TITLE_LENGTH = 80
_TITLE_COMMAND_PREFIXES = ("/", "$")
_TITLE_ROUTER_COMMANDS = {"/gobby", "$gobby"}
_TITLE_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_TEMPLATE_PLACEHOLDER_RE = re.compile(
    r"^\[?\s*(?:"
    r"\d+\s*-\s*\d+\s+word\s+session\s+title(?:\s+reflecting\s+current\s+work)?|"
    r"accurate\s+summary\s+of\s+the\s+full\s+turn\s+with\s+user\s+request\s*\+\s*"
    r"agent\s+response"
    r")\s*\]?$",
    re.IGNORECASE,
)
_ANGLE_BRACKET_PLACEHOLDER_RE = re.compile(r"^<([a-z_][a-z0-9_]*)>$", re.IGNORECASE)
_ANGLE_BRACKET_PLACEHOLDERS = frozenset({"user_query", "user_prompt", "prompt", "input"})
_UNSUITABLE_TITLE_PREFIX_RE = re.compile(
    r"^(?:#\d+\b|\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\d{1,2}:\d{2}(?::\d{2})?\b)"
)
_DECORATIVE_TITLE_GLYPHS = frozenset("•◦▪▫‣⁃")

__all__ = [
    "LIFECYCLE_CMDS",
    "is_template_placeholder",
    "normalize_title_candidate",
]


def _truncate_title(title: str, limit: int = _MAX_SESSION_TITLE_LENGTH) -> str:
    """Clamp a title without cutting through a word when possible."""
    title = title.strip()
    if len(title) <= limit:
        return title

    words = title.split()
    truncated_words: list[str] = []
    current_length = 0
    for word in words:
        next_length = len(word) if not truncated_words else current_length + 1 + len(word)
        if next_length > limit:
            break
        truncated_words.append(word)
        current_length = next_length

    if truncated_words:
        return " ".join(truncated_words)
    return title[:limit].rstrip()


def _has_command_prefix(value: str) -> bool:
    """Return whether a value starts with user-facing command syntax."""
    return value.startswith(_TITLE_COMMAND_PREFIXES)


def _strip_command_prefix(candidate: str) -> str:
    """Strip leading command/router tokens from a digest title candidate."""
    parts = candidate.split()
    if not parts or not _has_command_prefix(parts[0]):
        return candidate
    leading = parts.pop(0)
    if leading.lower() in _TITLE_ROUTER_COMMANDS and parts and _TITLE_SUBCOMMAND_RE.match(parts[0]):
        parts.pop(0)
    while parts and _has_command_prefix(parts[0]):
        parts.pop(0)
    return " ".join(parts)


def _is_angle_bracket_placeholder(value: str) -> bool:
    """Return whether a value is an unsubstituted prompt variable."""
    match = _ANGLE_BRACKET_PLACEHOLDER_RE.fullmatch(value.strip())
    return bool(match and match.group(1).lower() in _ANGLE_BRACKET_PLACEHOLDERS)


def normalize_title_candidate(value: Any) -> str | None:
    """Validate and normalize an LLM-proposed title candidate."""
    if not isinstance(value, str):
        return None
    title = unicodedata.normalize("NFKC", value).strip().strip('"').strip("'")
    title = " ".join(title.split())
    if _UNSUITABLE_TITLE_PREFIX_RE.match(title):
        return None
    if any(
        "\u2500" <= char <= "\u257f"
        or char in _DECORATIVE_TITLE_GLYPHS
        or unicodedata.category(char) in {"Cc", "Cf", "Cs", "So"}
        for char in title
    ):
        return None
    command_prefixed = _has_command_prefix(title)
    if command_prefixed:
        title = _strip_command_prefix(title)
    title = _truncate_title(title)
    if is_template_placeholder(title):
        return None
    if not title:
        return None
    if command_prefixed:
        return title[0].upper() + title[1:]
    return title


def is_template_placeholder(value: str) -> bool:
    """Return whether the LLM echoed a prompt-template placeholder."""
    stripped = value.strip()
    return bool(
        _TEMPLATE_PLACEHOLDER_RE.fullmatch(stripped) or _is_angle_bracket_placeholder(stripped)
    )
