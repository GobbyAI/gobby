"""Shared identifier grammar for plan sections, items, and coverage labels."""

from __future__ import annotations

import re

DOTTED_ID_PATTERN = (
    r"(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?)"
    r"(?:\.(?:\d+[a-z]?|[A-Z]+[0-9]+[a-z]?))*"
)
DOTTED_ID_REGEX: re.Pattern[str] = re.compile(rf"^{DOTTED_ID_PATTERN}$")


def is_dotted_id(value: str) -> bool:
    return DOTTED_ID_REGEX.fullmatch(value) is not None


__all__ = ["DOTTED_ID_PATTERN", "DOTTED_ID_REGEX", "is_dotted_id"]
