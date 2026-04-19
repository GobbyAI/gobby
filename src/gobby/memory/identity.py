"""Stable identity helpers for memory knowledge-graph entities."""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")
_GLOBAL_SCOPE = "__global__"


def normalize_entity_name(name: str) -> str:
    """Normalize an entity name for stable identity comparison."""
    normalized = unicodedata.normalize("NFKC", name)
    normalized = normalized.strip()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    return normalized.casefold()


def entity_key(project_id: str | None, name: str) -> str:
    """Build a stable entity key from scope plus normalized name."""
    scope = project_id if project_id is not None else _GLOBAL_SCOPE
    return f"{scope}::{normalize_entity_name(name)}"
