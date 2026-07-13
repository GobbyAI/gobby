"""Heuristic session-title extraction helpers."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from gobby.memory.synthetic_prompts import synthetic_body_reason

logger = logging.getLogger(__name__)

LIFECYCLE_CMDS = ("/clear", "/exit", "/compact")
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
_TITLE_COMMAND_PREFIXES = ("/", "$")
_TITLE_ROUTER_COMMANDS = {"/gobby", "$gobby"}
_TITLE_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_TITLE_ORCHESTRATION_BOILERPLATE_RE = re.compile(
    r"^a previous agent produced the plan below\b",
    re.IGNORECASE,
)
_TITLE_CONTROL_MARKER_RE = re.compile(
    r"^\[\s*request\s+interrupted\s+by\s+user(?:\s+[^\]]*)?\s*\]$",
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

# Matches known unsubstituted template variables wrapped in angle brackets that
# some CLIs (e.g. Grok) send as the prompt value instead of the actual user input.
_ANGLE_BRACKET_PLACEHOLDER_RE = re.compile(r"^<([a-z_][a-z0-9_]*)>$", re.IGNORECASE)
_ANGLE_BRACKET_PLACEHOLDERS = frozenset({"user_query", "user_prompt", "prompt", "input"})

# Droid's default session-start title placeholder.
_NATIVE_PLACEHOLDER_TITLES = frozenset({"new session"})

# Reject native titles longer than this before truncation — if it's this long,
# it's a response dump, not a title.
_NATIVE_TITLE_MAX_RAW_LENGTH = 200

# XML/tool tag titles that Claude can emit as ai-title values before real content lands.
_NATIVE_TOOL_TAG_TITLE_RE = re.compile(
    r"^</?(?:function_calls|invoke|parameter|local-command(?:[-_a-z0-9]*)?)(?:\s+[^>]*)?>$",
    re.IGNORECASE,
)

# XML-like block markers that indicate a raw response dump, not a title.
_NATIVE_TITLE_REJECT_MARKERS = ("<function_calls>", "<invoke", "<parameter", "<local-command")

__all__ = [
    "LIFECYCLE_CMDS",
    "build_heuristic_title",
    "heuristic_title_from_transcript",
    "is_template_placeholder",
    "normalize_native_title",
    "normalize_title_candidate",
]


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
        if not candidate or _has_command_prefix(candidate):
            return None
        return _truncate_title(candidate)
    return None


def _has_command_prefix(value: str) -> bool:
    """Return whether a value starts with user-facing command syntax."""
    return value.startswith(_TITLE_COMMAND_PREFIXES)


def _strip_command_prefix(candidate: str) -> str:
    """Strip leading slash/dollar command tokens from a title candidate.

    Command-prefixed first prompts (``/gobby plan ...``, ``$gobby coderabbit ...``,
    ``/loop check ...``) would otherwise yield empty or garbage titles. Strip the
    command token so the user's actual intent surfaces. ``/gobby`` and ``$gobby``
    are router prefixes, so when either leads, the next short lowercase token is
    treated as the skill/subcommand and also gets stripped, leaving the args (if
    any) as the title source. A bare ``$gobby coderabbit`` therefore strips to
    empty, falling back to digest synthesis later.
    """
    parts = candidate.split()
    if not parts or not _has_command_prefix(parts[0]):
        return candidate
    leading = parts.pop(0)
    if leading.lower() in _TITLE_ROUTER_COMMANDS and parts and _TITLE_SUBCOMMAND_RE.match(parts[0]):
        parts.pop(0)
    while parts and _has_command_prefix(parts[0]):
        parts.pop(0)
    return " ".join(parts)


def _is_control_marker(value: str) -> bool:
    """Return True for provider control records that are not user intent."""
    normalized = re.sub(r"\s+", " ", value.strip())
    return bool(_TITLE_CONTROL_MARKER_RE.fullmatch(normalized))


def _is_angle_bracket_placeholder(value: str) -> bool:
    """Return True for unsubstituted template variables like ``<user_query>``."""
    match = _ANGLE_BRACKET_PLACEHOLDER_RE.fullmatch(value.strip())
    return bool(match and match.group(1).lower() in _ANGLE_BRACKET_PLACEHOLDERS)


def _is_native_tool_tag_title(value: str) -> bool:
    """Return True when the entire native title is a raw provider/tool XML tag."""
    return bool(_NATIVE_TOOL_TAG_TITLE_RE.fullmatch(value.strip()))


def build_heuristic_title(prompt_text: Any) -> str | None:
    """Derive a cheap bootstrap title from the first meaningful user prompt."""
    raw_text = _coerce_prompt_text(prompt_text)
    if not raw_text.strip():
        return None
    if _is_control_marker(raw_text):
        return None
    if synthetic_body_reason(raw_text):
        return None
    if _is_angle_bracket_placeholder(raw_text):
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
        if line and not _is_control_marker(line):
            lines.append(line)

    if not lines:
        return None

    candidate = re.sub(r"\s+", " ", lines[0]).strip()
    if not candidate:
        return None
    if _has_command_prefix(candidate):
        candidate = _strip_command_prefix(candidate)
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


async def heuristic_title_from_transcript(
    transcript_path: str | None,
    source: str | None,
    *,
    max_lines: int = 600,
) -> str | None:
    """Derive a heuristic title from the transcript's opening user prompt.

    Parses the transcript from the **beginning** via the provider parser
    (:meth:`TranscriptParser.parse_lines`) and feeds the first meaningful user
    text message — ``role == "user"`` and ``content_type == "text"``, skipping
    lifecycle commands and tool-result/non-text records — to
    :func:`build_heuristic_title`. LLM-free; the provider-agnostic backstop for
    sessions whose per-turn title paths never landed. Returns ``None`` when the
    transcript is missing, empty, or yields no usable opening prompt.

    The scan runs from the first line forward and stops after ``max_lines``:
    a session's first user prompt is always near the start, so this bounds work
    on long transcripts without changing the result.
    """
    if not transcript_path or not Path(transcript_path).exists():
        return None
    if not str(source or "").strip():
        logger.warning(
            "Skipping heuristic title for transcript %s: session source is missing",
            transcript_path,
        )
        return None
    try:
        from gobby.sessions.transcript_normalization import normalize_transcript_records
        from gobby.sessions.transcripts import ParsedMessage, get_parser

        try:
            parser = get_parser(source)
        except ValueError as exc:
            logger.warning("Skipping heuristic title for transcript %s: %s", transcript_path, exc)
            return None

        def _read_lines() -> list[str]:
            lines: list[str] = []
            with open(transcript_path, encoding="utf-8") as f:
                for line in f:
                    lines.append(line)
                    if len(lines) >= max_lines:
                        break
            return lines

        lines = await asyncio.to_thread(_read_lines)
        if not lines:
            return None

        for record in normalize_transcript_records(parser.parse_lines(lines), source):
            if not isinstance(record, ParsedMessage):
                continue
            if record.role != "user" or record.content_type != "text":
                continue
            content = (
                record.content if isinstance(record.content, str) else str(record.content or "")
            )
            stripped = content.strip().lower()
            if (
                not stripped
                or any(stripped == cmd or stripped.startswith(cmd + " ") for cmd in LIFECYCLE_CMDS)
                or _is_control_marker(content)
            ):
                continue
            title = build_heuristic_title(content)
            if title:
                return title
        return None
    except Exception as e:
        logger.debug("Heuristic title from transcript %s failed: %s", transcript_path, e)
        return None


def normalize_title_candidate(value: Any) -> str | None:
    """Validate and normalize an LLM-proposed title candidate."""
    if not isinstance(value, str):
        return None
    title = value.strip().strip('"').strip("'")
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


def _normalize_claude_native_title(title: str) -> str:
    """Normalize Claude ai-title slugs into readable titles."""
    return re.sub(r"\s+", " ", title.replace("-", " ")).strip()


def normalize_native_title(value: Any, *, source: str | None = None) -> str | None:
    """Validate and normalize a CLI-native session title (Claude ai-title, Droid sessionTitle).

    Native titles are AI-synthesized by the CLI itself, but quality varies:
    Droid's ``sessionTitle`` is sometimes the entire first assistant response
    (hundreds of chars, markdown, raw ``<function_calls>`` blocks) rather than a
    concise title. This function rejects garbage and returns a clean truncated
    title, or ``None`` when the value is not title-like.
    """
    if not isinstance(value, str):
        return None
    title = value.strip()
    if not title:
        return None
    if title.lower() in _NATIVE_PLACEHOLDER_TITLES:
        return None
    if "\n" in title:
        return None
    if len(title) > _NATIVE_TITLE_MAX_RAW_LENGTH:
        return None
    if _is_native_tool_tag_title(title):
        return None
    lowered_title = title.lower()
    if any(marker in lowered_title for marker in _NATIVE_TITLE_REJECT_MARKERS):
        return None
    if is_template_placeholder(title):
        return None
    if source == "claude":
        title = _normalize_claude_native_title(title)
        if not title:
            return None
    return _truncate_title(title)


def is_template_placeholder(value: str) -> bool:
    """Return True for prompt-template placeholders echoed by the LLM."""
    stripped = value.strip()
    return bool(
        _TEMPLATE_PLACEHOLDER_RE.fullmatch(stripped) or _is_angle_bracket_placeholder(stripped)
    )
