"""Heuristic session-title extraction helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LIFECYCLE_CMDS = ("/clear", "/exit", "/compact")
_MAX_SESSION_TITLE_LENGTH = 80
_TITLE_LINE_CLEANUP_RE = re.compile(r"^\s*(?:[-*+>#]+|\d+[.)])\s*")
_TITLE_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_TITLE_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_TITLE_LEADING_PHRASE_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|"
    r"help\s+me\s+(?:to\s+)?|i(?:'d| would)?\s+like\s+to\s+|"
    r"i\s+want\s+to\s+|need\s+to\s+|we\s+need\s+to\s+|"
    r"let'?s\s+|can\s+we\s+|could\s+we\s+)",
    re.IGNORECASE,
)
_TITLE_BREAK_RE = re.compile(r"(?<=[.!?])\s+|[:;]\s+|\s+[/-]\s+")
_TITLE_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_TITLE_ORCHESTRATION_BOILERPLATE_RE = re.compile(
    r"^a previous agent produced the plan below\b",
    re.IGNORECASE,
)
# Matches literal prompt placeholders that models sometimes echo instead of
# replacing; those values must not become persisted titles or digest turns.
_TEMPLATE_PLACEHOLDER_RE = re.compile(
    r"^\[?\s*(?:"
    r"\d+\s*-\s*\d+\s+word\s+session\s+title(?:\s+reflecting\s+current\s+work)?|"
    r"accurate\s+summary\s+of\s+the\s+full\s+turn\s+with\s+user\s+request\s*\+\s*"
    r"agent\s+response"
    r")\s*\]?$",
    re.IGNORECASE,
)


def _coerce_prompt_text(prompt_text: Any) -> str:
    """Normalize prompt text from string or multimodal blocks into plain text."""
    if isinstance(prompt_text, str):
        return prompt_text
    if not isinstance(prompt_text, list):
        return str(prompt_text or "")

    parts: list[str] = []
    for block in prompt_text:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
            continue
        if isinstance(block.get("content"), str):
            parts.append(block["content"])
    return "\n".join(part for part in parts if part)


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


def _extract_markdown_h1_title(text: str) -> str | None:
    """Extract a markdown H1 title from wrapper prompts when present."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("# ") or line.startswith("##"):
            continue
        candidate = _TITLE_LINK_RE.sub(r"\1", line[2:]).replace("`", " ")
        candidate = re.sub(r"\s+", " ", candidate).strip(" \t\r\n.,:;!?-")
        if not candidate or candidate.startswith("/"):
            return None
        return _truncate_title(candidate)
    return None


def _strip_slash_command_prefix(candidate: str) -> str:
    """Strip leading slash-command tokens from a title candidate.

    Slash-prefixed first prompts (``/gobby plan ...``, ``/loop check ...``)
    would otherwise yield empty or garbage titles. Strip the command token so
    the user's actual intent surfaces. ``/gobby`` is the namespace prefix used
    by every Gobby slash command, so when it leads, the next short lowercase
    token is the subcommand and also gets stripped, leaving the args (if any)
    as the title source. A bare ``/gobby plan`` therefore strips to empty,
    falling back to the digest path later.
    """
    parts = candidate.split()
    if not parts or not parts[0].startswith("/"):
        return candidate
    leading = parts.pop(0)
    if leading.lower() == "/gobby" and parts and _TITLE_SUBCOMMAND_RE.match(parts[0]):
        parts.pop(0)
    while parts and parts[0].startswith("/"):
        parts.pop(0)
    return " ".join(parts)


def _build_heuristic_title(prompt_text: Any) -> str | None:
    """Derive a cheap bootstrap title from the first meaningful user prompt."""
    raw_text = _coerce_prompt_text(prompt_text)
    if not raw_text.strip():
        return None

    cleaned = _TITLE_CODE_BLOCK_RE.sub(" ", raw_text)
    cleaned = _TITLE_LINK_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("`", " ")

    first_nonempty = next((line.strip() for line in cleaned.splitlines() if line.strip()), "")
    if _TITLE_ORCHESTRATION_BOILERPLATE_RE.match(first_nonempty):
        h1_title = _extract_markdown_h1_title(cleaned)
        return h1_title[0].upper() + h1_title[1:] if h1_title else None

    lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = _TITLE_LINE_CLEANUP_RE.sub("", raw_line).strip()
        if line:
            lines.append(line)

    if not lines:
        return None

    candidate = re.sub(r"\s+", " ", lines[0]).strip()
    if not candidate:
        return None
    if candidate.startswith("/"):
        candidate = _strip_slash_command_prefix(candidate)
        if not candidate:
            return None

    candidate = _TITLE_LEADING_PHRASE_RE.sub("", candidate)
    candidate = _TITLE_BREAK_RE.split(candidate, maxsplit=1)[0]
    candidate = candidate.strip(" \t\r\n.,:;!?-")
    if not candidate:
        return None

    words = candidate.split()
    if len(words) > 7:
        candidate = " ".join(words[:7])

    candidate = _truncate_title(candidate)
    if not candidate or len(candidate) < 2:
        return None

    return candidate[0].upper() + candidate[1:]


async def _heuristic_title_from_transcript(
    transcript_path: str | None,
    source: str | None,
    *,
    max_turns: int = 200,
) -> str | None:
    """Derive a heuristic title from the first meaningful user prompt.

    Reads the transcript's current segment (since the last ``/clear``) via the
    shared parser registry and feeds the opening non-lifecycle user prompt to
    :func:`_build_heuristic_title`. LLM-free; the resilient backstop for sessions
    whose per-turn title paths never landed (notably interactive Claude sessions
    whose stops are perpetually blocked). Returns ``None`` when the transcript is
    missing, empty, or yields no usable prompt.
    """
    if not transcript_path or not Path(transcript_path).exists():
        return None
    try:
        from gobby.sessions.transcripts import get_parser

        parser = get_parser(source or "")

        def _read_lines() -> list[str]:
            with open(transcript_path, encoding="utf-8") as f:
                return f.readlines()

        turns: list[dict[str, Any]] = []
        for line in await asyncio.to_thread(_read_lines):
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        segment = parser.extract_turns_since_clear(turns, max_turns=max_turns) if turns else []
        if not segment:
            return None
        # Large num_pairs keeps every message, so the first user one is the opener.
        for msg in parser.extract_last_messages(segment, num_pairs=len(segment) + 1):
            if msg.get("role") != "user":
                continue
            content = str(msg.get("content") or "")
            stripped = content.strip().lower()
            if not stripped or any(
                stripped == c or stripped.startswith(c + " ") for c in _LIFECYCLE_CMDS
            ):
                continue
            title = _build_heuristic_title(content)
            if title:
                return title
        return None
    except Exception as e:
        logger.debug("Heuristic title from transcript %s failed: %s", transcript_path, e)
        return None


def _normalize_title_candidate(value: Any) -> str | None:
    """Validate and normalize an LLM-proposed title candidate."""
    if not isinstance(value, str):
        return None
    title = value.strip().strip('"').strip("'")
    title = _truncate_title(title)
    if _is_template_placeholder(title):
        return None
    return title or None


def _is_template_placeholder(value: str) -> bool:
    """Return True for prompt-template placeholders echoed by the LLM."""
    return bool(_TEMPLATE_PLACEHOLDER_RE.fullmatch(value.strip()))
