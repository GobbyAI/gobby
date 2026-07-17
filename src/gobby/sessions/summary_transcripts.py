"""Transcript and digest helpers for session summary generation."""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiofiles

from gobby.utils.injected_context import strip_injected_context

logger = logging.getLogger("gobby.sessions.summarize")

TURN_PATTERN = re.compile(r"^### Turn \d+", re.MULTILINE)
TRANSCRIPT_FALLBACK_MAX_TURNS = 80
TRANSCRIPT_FALLBACK_MAX_CHARS = 24_000
DIGEST_FALLBACK_MAX_CHARS = 24_000


async def _read_transcript(
    path: Path,
    source: str = "claude",
    max_turns: int | None = None,
) -> list[dict[str, Any]]:
    """Read and parse a transcript file in its native format.

    Claude, Codex, and Droid use JSONL (one JSON object per line).
    Qwen stores sessions as a single JSON object with a ``messages`` array.
    The returned dicts are in the source's native format - callers that need
    to iterate content blocks should use format-aware helpers.

    Args:
        path: Path to the transcript file.
        source: Session source (``"claude"``, ``"qwen"``, ``"codex"``,
            ``"droid"``).
    """
    # Typed JSON session files are a single JSON object, not JSONL. Legacy
    # registrations may have source="unknown" even when the file shape is typed.
    if path.suffix == ".json" and source in {"qwen", "unknown"}:
        typed_turns = await _read_typed_json_transcript(path)
        return typed_turns[-max_turns:] if max_turns is not None else typed_turns

    # JSONL format (Claude, Codex, default)
    turns: list[dict[str, Any]] | deque[dict[str, Any]] = (
        deque(maxlen=max_turns) if max_turns is not None else []
    )
    async with aiofiles.open(path, encoding="utf-8") as f:
        async for idx, line in async_enumerate(f):
            if line.strip():
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        turns.append(obj)
                    else:
                        logger.warning(
                            "Skipping non-dict JSONL value",
                            extra={"line": idx + 1, "path": str(path)},
                        )
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed JSONL line",
                        extra={"line": idx + 1, "path": str(path)},
                    )
    return list(turns)


def _summary_source_text(value: str | None) -> str:
    """Normalize optional markdown fields for summary context decisions."""
    if not isinstance(value, str) or not value.strip():
        return ""
    return strip_injected_context(value).strip()


def _digest_markdown_for_summary(session: Any) -> str:
    """Return digest context with the latest completed turn when digest lags."""
    digest_markdown = strip_injected_context(
        _summary_source_text(getattr(session, "digest_markdown", None))
    )
    pending_turns = [
        strip_injected_context(_summary_source_text(getattr(session, "last_turn_markdown", None))),
        strip_injected_context(
            _summary_source_text(getattr(session, "last_assistant_content", None))
        ),
    ]

    summary_parts = [digest_markdown] if digest_markdown else []
    next_turn = len(TURN_PATTERN.findall(digest_markdown)) + 1
    for turn_markdown in pending_turns:
        if not turn_markdown:
            continue
        joined_summary = "\n\n".join(summary_parts)
        if turn_markdown in joined_summary:
            continue
        summary_parts.append(f"### Turn {next_turn}\n{turn_markdown}")
        next_turn += 1

    return "\n\n".join(summary_parts)


def _strip_injected_context_from_value(value: Any) -> Any:
    if isinstance(value, str):
        return strip_injected_context(value)
    if isinstance(value, list):
        return [_strip_injected_context_from_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_injected_context_from_value(item) for key, item in value.items()}
    return value


def _truncate_markdown(value: str, max_chars: int) -> str:
    """Bound prompt context without splitting through the fallback plumbing."""
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()}\n..."


def _format_transcript_fallback_summary(
    turns: list[dict[str, Any]],
    formatter: Any,
) -> str:
    """Format a bounded transcript fallback for sessions without digest markdown."""
    bounded_turns = turns[-TRANSCRIPT_FALLBACK_MAX_TURNS:]
    formatted = formatter(bounded_turns)
    return _truncate_markdown(formatted, TRANSCRIPT_FALLBACK_MAX_CHARS)


def _format_deterministic_summary(handoff_ctx: Any, digest_markdown: str) -> str:
    """Build deterministic markdown when provider generation is unavailable."""
    from gobby.sessions.formatting import format_handoff_as_markdown

    base_markdown = format_handoff_as_markdown(handoff_ctx)
    current_state_parts: list[str] = []
    if digest_markdown:
        digest_section = _truncate_markdown(digest_markdown, DIGEST_FALLBACK_MAX_CHARS)
        current_state_parts.append(f"### Session Digest\n\n{digest_section}")
    if base_markdown:
        current_state_parts.append(base_markdown)
    if not current_state_parts:
        return ""

    current_state = "\n\n".join(current_state_parts)
    return (
        f"## Current State\n\n{current_state}\n\n"
        "## Next Steps\n\nContinue from the captured session state."
    )


async def _read_typed_json_transcript(path: Path) -> list[dict[str, Any]]:
    """Read a typed-JSON session file and return its native message dicts.

    Typed-JSON session files have the structure::

        {"sessionId": "...", "messages": [{...}, ...], "kind": "main"}

    We return the ``messages`` array as-is so callers get native dicts.
    """
    async with aiofiles.open(path, encoding="utf-8") as f:
        raw = await f.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(
            "Invalid JSON in typed-JSON transcript",
            extra={"path": str(path), "error": str(e)},
        )
        return []

    if not isinstance(data, dict):
        logger.error(
            "Expected JSON object in typed-JSON transcript",
            extra={"path": str(path), "actual_type": type(data).__name__},
        )
        return []

    messages = data.get("messages", [])
    return [m for m in messages if isinstance(m, dict)]


async def async_enumerate[T](
    aiter: AsyncIterator[T], start: int = 0
) -> AsyncIterator[tuple[int, T]]:
    """Async version of enumerate."""
    idx = start
    async for item in aiter:
        yield idx, item
        idx += 1


def _extract_digest_turns(digest_markdown: str | None) -> tuple[str, str]:
    """Extract first and last digest turns from rolling digest markdown.

    Args:
        digest_markdown: The session's rolling digest_markdown field.

    Returns:
        Tuple of (first_turn_text, recent_turns_text). Empty strings if unavailable.
    """
    if not digest_markdown:
        return "", ""

    # Split on ### Turn N headings
    parts = TURN_PATTERN.split(digest_markdown)
    headings = TURN_PATTERN.findall(digest_markdown)

    if not headings:
        # No turn structure - return first 500 chars as first turn
        return digest_markdown[:500].strip(), ""

    # parts[0] is content before first heading (preamble), parts[1:] are turn contents
    # Pair headings with their content
    turns: list[str] = []
    for i, heading in enumerate(headings):
        content = parts[i + 1] if (i + 1) < len(parts) else ""
        turns.append(f"{heading}\n{content.strip()}")

    first_turn = turns[0] if turns else ""
    # Last 2 turns for recent context
    recent = turns[-2:] if len(turns) >= 2 else turns
    recent_turns = "\n\n".join(recent)

    # Truncate to avoid blowing up the prompt
    if len(first_turn) > 800:
        first_turn = first_turn[:800] + "\n..."
    if len(recent_turns) > 1500:
        recent_turns = recent_turns[:1500] + "\n..."

    return first_turn, recent_turns
