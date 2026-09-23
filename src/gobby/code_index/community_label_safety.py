"""Validation for model-generated import-community labels."""

from __future__ import annotations

import re
from collections.abc import Collection

_OPENING_FENCE = re.compile(r"\A```[^\n]*\n")
_CLOSING_FENCE = re.compile(r"\n?```\s*\Z")
_LABEL_CHARSET = re.compile(r"[A-Za-z0-9 /&+.-]+")
_SOURCE_EXTENSION = re.compile(r"\.(py|pyi|ts|tsx|js|mjs|cjs|css|rs|sh)\b", re.IGNORECASE)


def sanitize_community_label(text: str, *, members: Collection[str]) -> str | None:
    """Return a safe 2-5 word community name, or None when the text is unusable."""
    label = _CLOSING_FENCE.sub("", _OPENING_FENCE.sub("", text.strip()))
    label = " ".join(label.strip().strip("\"'`").split())
    if not 2 <= len(label.split()) <= 5 or len(label) > 40:
        return None
    if not _LABEL_CHARSET.fullmatch(label) or _SOURCE_EXTENSION.search(label):
        return None
    if label in members:
        return None
    return label
