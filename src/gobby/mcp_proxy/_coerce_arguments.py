"""Shared helper for coercing string arguments to dicts.

Agents frequently send call_tool arguments as JSON strings — sometimes with
literal backslash-escaped quotes (``{\\"title\\": \\"foo\\"}``) instead of
proper JSON.  This module provides a single function that all call_tool entry
points use so the fallback logic lives in one place.
"""

from __future__ import annotations

import json
from typing import Any


def coerce_string_arguments(value: str) -> dict[str, Any] | None:
    """Try to parse *value* as a JSON dict.

    1. Attempt a straight ``json.loads``.
    2. If that fails and the string contains literal ``\\"`` sequences, strip
       them to plain ``"`` and retry.

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
        try:
            parsed = json.loads(value.replace('\\"', '"'))
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass

    return None
