"""JSON extraction utilities for parsing LLM responses.

This module provides robust JSON extraction from text that may contain
markdown code blocks, preamble text, or other non-JSON content.

Also provides typed JSON decoding using msgspec for structured LLM responses.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import msgspec

logger = logging.getLogger(__name__)


def extract_json_from_text(text: str | None) -> str | None:
    """
    Extract JSON from text, handling markdown code blocks and mixed content.

    Uses json.JSONDecoder.raw_decode() which properly handles all JSON
    edge cases (nested strings, escapes, backticks in strings, etc.)
    rather than brittle regex patterns.

    Args:
        text: Raw text that may contain JSON, possibly wrapped in markdown
              code blocks or with preamble/postamble text.

    Returns:
        Extracted JSON string, or None if no valid JSON found.

    Examples:
        >>> extract_json_from_text('{"key": "value"}')
        '{"key": "value"}'

        >>> extract_json_from_text('Here is the result:\\n```json\\n{"key": "value"}\\n```')
        '{"key": "value"}'

        >>> extract_json_from_text('No JSON here')
        None
    """
    if not text:
        return None

    decoder = json.JSONDecoder()

    # Build list of positions to try, prioritizing code block content
    positions_to_try: list[int] = []

    # Look for ```json marker first (most specific)
    code_block_idx = text.find("```json")
    if code_block_idx != -1:
        brace_pos = text.find("{", code_block_idx + 7)
        if brace_pos != -1:
            positions_to_try.append(brace_pos)

    # Then try plain ``` marker
    if not positions_to_try:
        code_block_idx = text.find("```")
        if code_block_idx != -1:
            brace_pos = text.find("{", code_block_idx + 3)
            if brace_pos != -1:
                positions_to_try.append(brace_pos)

    # Finally try raw JSON (first { in text)
    first_brace = text.find("{")
    if first_brace != -1 and first_brace not in positions_to_try:
        positions_to_try.append(first_brace)

    # Try each position until we find valid JSON
    logger.debug(
        "extract_json_from_text: trying %d positions in %d-char text",
        len(positions_to_try),
        len(text),
    )
    for pos in positions_to_try:
        try:
            # raw_decode returns (obj, end_idx) where end_idx is absolute position
            _, end_idx = decoder.raw_decode(text, pos)
            return text[pos:end_idx]
        except json.JSONDecodeError:
            continue

    logger.debug(
        "extract_json_from_text: no valid JSON found in text (length=%d, redacted)",
        len(text),
    )
    return None


def extract_json_object(text: str | None) -> dict[str, Any] | None:
    """
    Extract and parse a JSON object from text.

    Convenience wrapper that extracts JSON string and parses it.

    Args:
        text: Raw text that may contain JSON.

    Returns:
        Parsed JSON dict, or None if no valid JSON found.
    """
    json_str = extract_json_from_text(text)
    if json_str is None:
        return None

    try:
        result = json.loads(json_str)
        if isinstance(result, dict):
            return result
        logger.warning(f"Extracted JSON is not an object: {type(result)}")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse extracted JSON: {e}")
        return None


def json_equal(left: Any, right: Any) -> bool:
    """Compare JSON payloads semantically across strings and decoded values."""
    left_value: object = _decode_json_if_text(left)
    right_value: object = _decode_json_if_text(right)
    return left_value == right_value


def _decode_json_if_text(value: Any) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def decode_llm_response[T](
    text: str | None,
    response_type: type[T],
    *,
    strict: bool = True,
) -> T | None:
    """
    Extract JSON from LLM response and decode to a typed struct.

    Uses msgspec for efficient, type-safe JSON decoding with clear error messages.
    Combines extract_json_from_text() with msgspec.json.decode().

    Args:
        text: Raw LLM response text (may contain markdown code blocks, preamble, etc.)
        response_type: The msgspec.Struct or other type to decode to
        strict: If True (default), type mismatches raise errors.
                If False, allows coercion (e.g., "5" -> 5 for int fields).
                Configure via llm_providers.json_strict in gobby-config,
                or override per-workflow with llm_json_strict variable.

    Returns:
        Decoded response of type T, or None if extraction/decoding fails.

    Examples:
        >>> class TaskResult(msgspec.Struct):
        ...     status: str
        ...     count: int
        >>> result = decode_llm_response('{"status": "ok", "count": 5}', TaskResult)
        >>> result.status
        'ok'

        >>> # With strict=False, string "5" coerces to int 5
        >>> result = decode_llm_response('{"status": "ok", "count": "5"}', TaskResult, strict=False)
        >>> result.count
        5
    """
    json_str = extract_json_from_text(text)
    if json_str is None:
        logger.debug("No JSON found in LLM response")
        return None

    try:
        # msgspec.json.decode returns Any at runtime when using TypeVar
        return msgspec.json.decode(json_str.encode(), type=response_type, strict=strict)
    except msgspec.ValidationError as e:
        logger.warning(f"Invalid LLM response structure: {e}")
        return None
    except msgspec.DecodeError as e:
        logger.warning(f"Failed to decode LLM response JSON: {e}")
        return None
