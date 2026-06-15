"""Shared search helpers for code-index storage mixins."""

from __future__ import annotations

from typing import Any

from gobby.search import keyword
from gobby.storage.hub.protocol import HubDatabase


def rows_by_ids(db: HubDatabase, table: str, ids: list[str]) -> list[Any]:
    """Fetch rows by IDs while preserving backend-specific placeholder behavior."""
    if not ids:
        return []
    params = list(ids)
    placeholders = ", ".join(keyword.placeholder(db, index) for index in range(1, len(ids) + 1))
    return keyword.fetch_all(db, f"SELECT * FROM {table} WHERE id IN ({placeholders})", params)


def make_snippet(content: str, query: str) -> str:
    """Return a small content snippet around the first query token match."""
    lowered = content.lower()
    tokens = [token.lower() for token in query.split() if token.strip()]
    match_at = -1
    for token in tokens:
        match_at = lowered.find(token)
        if match_at >= 0:
            break
    if match_at < 0:
        match_at = 0
    start = max(0, match_at - 60)
    end = min(len(content), match_at + 120)
    return content[start:end]
