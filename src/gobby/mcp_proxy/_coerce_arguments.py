"""Shared helper for coercing string arguments to dicts.

Agents frequently send call_tool arguments as JSON strings — sometimes with
literal backslash-escaped quotes (``{\\"title\\": \\"foo\\"}``) instead of
proper JSON.  This module provides a single function that all call_tool entry
points use so the fallback logic lives in one place.
"""

from __future__ import annotations

import json
import unicodedata
from string import hexdigits
from typing import Any


def _decode_legacy_escapes(value: str) -> str | None:
    """Decode legacy escapes, retaining unknown ones and three-digit octal values."""
    # The former codec interpreted UTF-8 bytes as Latin-1 characters. Keep that
    # observable behavior, including for unescaped non-ASCII input.
    try:
        raw = value.encode("utf-8").decode("latin-1")
    except UnicodeEncodeError:
        return None
    decoded: list[str] = []
    index = 0
    simple = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    while index < len(raw):
        char = raw[index]
        index += 1
        if char != "\\":
            decoded.append(char)
            continue
        if index == len(raw):
            return None
        escaped = raw[index]
        index += 1
        if escaped == "\n":
            continue
        if escaped in "\\'\"":
            decoded.append(escaped)
        elif escaped in simple:
            decoded.append(simple[escaped])
        elif escaped in "01234567":
            start = index - 1
            while index < min(start + 3, len(raw)) and raw[index] in "01234567":
                index += 1
            decoded.append(chr(int(raw[start:index], 8)))
        elif escaped in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[escaped]
            digits = raw[index : index + width]
            if len(digits) != width or any(digit not in hexdigits for digit in digits):
                return None
            codepoint = int(digits, 16)
            if codepoint > 0x10FFFF:
                return None
            decoded.append(chr(codepoint))
            index += width
        elif escaped == "N":
            if index == len(raw) or raw[index] != "{":
                return None
            end = raw.find("}", index + 1)
            if end == -1:
                return None
            try:
                named = unicodedata.lookup(raw[index + 1 : end])
            except KeyError:
                return None
            if len(named) != 1:
                return None
            decoded.append(named)
            index = end + 1
        else:
            decoded.extend(("\\", escaped))
    return "".join(decoded)


def coerce_string_arguments(value: str) -> dict[str, Any] | None:
    """Try to parse *value* as a JSON dict.

    1. Attempt a straight ``json.loads``.
    2. If that fails and the string contains literal ``\\"`` sequences, strip
       them to plain ``"`` and retry.
    3. Fall back to explicit legacy escape decoding for double-encoded input.

    Returns the parsed dict, or ``None`` if every attempt fails (caller
    decides what error to surface).
    """
    # Fast path — well-formed JSON
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass

    # Fallback — literal backslash-escaped quotes
    if '\\"' in value:
        candidates: list[str] = []

        replaced = value.replace('\\"', '"')
        if replaced != value:
            candidates.append(replaced)

        decoded = _decode_legacy_escapes(value)
        if decoded is not None and decoded != value:
            candidates.append(decoded)

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                continue

    return None
