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


def _encode_component(value: str) -> str:
    """Encode a key component with an explicit length prefix."""
    return f"{len(value)}:{value}"


def entity_key(project_id: str | None, name: str) -> str:
    """Build a stable entity key from scope plus normalized name."""
    scope_kind = "g" if project_id is None else "p"
    scope_value = _GLOBAL_SCOPE if project_id is None else project_id
    normalized_name = normalize_entity_name(name)
    return f"{scope_kind}:{_encode_component(scope_value)}|n:{_encode_component(normalized_name)}"
