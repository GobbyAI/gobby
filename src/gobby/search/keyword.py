"""Keyword search backends for hub storage."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

SearchMode = Literal["keyword", "semantic"]


@dataclass(frozen=True)
class SearchHit:
    """Single keyword search result."""

    id: str
    score: float
    snippet: str | None = None


class KeywordSearchBackend(Protocol):
    """Single-source keyword search. Returns a ranked list per call."""

    def search(
        self,
        query: str,
        limit: int,
        *,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Return keyword hits ranked best first."""
        ...

    def get_stats(self) -> dict[str, Any]:
        """Return backend statistics."""
        ...

    def clear(self) -> None:
        """Clear the keyword index when the backend supports it."""
        ...


@dataclass(frozen=True)
class _TableConfig:
    table: str
    aliases: tuple[str, ...] = ()
    postgres_columns: tuple[str, ...] = ()
    filters: Mapping[str, str] | None = None


_TABLE_CONFIGS: dict[str, _TableConfig] = {
    "tasks": _TableConfig(
        table="tasks",
        aliases=("tasks_fts",),
        postgres_columns=("title", "description"),
        filters={
            "project_id": "project_id",
            "task_type": "task_type",
            "priority": "priority",
            "parent_task_id": "parent_task_id",
            "category": "category",
        },
    ),
    "memories": _TableConfig(
        table="memories",
        aliases=("memories_fts",),
        postgres_columns=("content", "tags_text"),
        filters={"project_id": "project_id"},
    ),
    "skills": _TableConfig(
        table="skills",
        aliases=("skills_fts",),
        postgres_columns=("name", "description", "content"),
    ),
    "code_symbols": _TableConfig(
        table="code_symbols",
        aliases=("code_symbols_fts",),
        postgres_columns=("name", "qualified_name", "signature", "docstring", "summary"),
        filters={"project_id": "project_id", "kind": "kind", "file_path": "file_path"},
    ),
    "code_content": _TableConfig(
        table="code_content_chunks",
        aliases=("code_content_fts",),
        postgres_columns=("content",),
        filters={"project_id": "project_id", "file_path": "file_path"},
    ),
}

_TABLE_ALIAS_TO_TABLE = {
    alias: name for name, config in _TABLE_CONFIGS.items() for alias in config.aliases
}


def keyword_table_for_fts_table(fts_table: str) -> str:
    """Return the backend table key for an existing keyword table alias."""
    try:
        return _TABLE_ALIAS_TO_TABLE[fts_table]
    except KeyError as exc:
        raise ValueError(f"unsupported keyword table alias: {fts_table}") from exc


def pick_search_backend(
    hub: Any,
    table: str,
    mode: SearchMode = "keyword",
) -> KeywordSearchBackend:
    """Pick the runtime keyword search backend for a hub database."""
    if mode == "semantic":
        raise NotImplementedError(
            "Semantic search is a follow-up workstream; use mode='keyword' today."
        )
    config = _table_config(table)
    return BM25SearchBackend(hub, config)


class BM25SearchBackend:
    """PostgreSQL pg_search BM25 keyword backend."""

    def __init__(self, hub: Any, table: str | _TableConfig) -> None:
        self._hub = hub
        self._config = _table_config(table) if isinstance(table, str) else table

    def search(
        self,
        query: str,
        limit: int,
        *,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        bm25_query = sanitize_pg_search_query(query)
        if not bm25_query:
            return []

        params: list[Any] = []
        search_clauses = []
        for column in self._config.postgres_columns:
            placeholder = _add_param(self._hub, params, bm25_query)
            search_clauses.append(f"{column} @@@ {placeholder}")

        where = [f"({' OR '.join(search_clauses)})"]
        if filters:
            where.extend(
                _filter_clauses(self._hub, params, self._config.table, self._config, filters)
            )

        limit_placeholder = _add_param(self._hub, params, limit)
        sql = f"""
            SELECT id, pdb.score(id) AS score
              FROM {self._config.table}
             WHERE {" AND ".join(where)}
             ORDER BY score DESC, id ASC
             LIMIT {limit_placeholder}
        """

        try:
            rows = fetch_all(self._hub, sql, params)
        except Exception as exc:
            logger.debug("pg_search BM25 search failed on %s: %s", self._config.table, exc)
            return []

        raw_scores = [float(row_value(row, "score")) for row in rows]
        normalized = normalize_positive_scores(raw_scores)
        return [
            SearchHit(id=str(row_value(row, "id")), score=score)
            for row, score in zip(rows, normalized, strict=False)
        ]

    def get_stats(self) -> dict[str, Any]:
        try:
            row = fetch_one(self._hub, f"SELECT count(*) AS cnt FROM {self._config.table}", [])
            count = int(row_value(row, "cnt")) if row else 0
        except Exception:
            count = 0
        return {
            "backend_type": "pg_search_bm25",
            "table": self._config.table,
            "document_count": count,
            "fitted": True,
        }

    def clear(self) -> None:
        return None


class KeywordAsyncSearchBackend:
    """Async adapter for UnifiedSearcher keyword mode."""

    def __init__(self, hub: Any, table: str) -> None:
        self._hub = hub
        self._table = table
        self._backend = pick_search_backend(hub, table)
        self._fitted_items: list[tuple[str, str]] | None = None

    async def fit_async(self, items: list[tuple[str, str]]) -> None:
        fit = getattr(self._backend, "fit", None)
        if callable(fit):
            fit(items)
            self._fitted_items = None
        else:
            self._fitted_items = items.copy()
        return None

    async def search_async(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        return self.search(query, top_k)

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        hits = [(hit.id, hit.score) for hit in self._backend.search(query, top_k)]
        if hits or self._fitted_items is None:
            return hits
        return _search_fitted_items(query, self._fitted_items, top_k)

    def needs_refit(self) -> bool:
        return False

    def get_stats(self) -> dict[str, Any]:
        get_stats = getattr(self._backend, "get_stats", None)
        if callable(get_stats):
            return dict(get_stats())
        return {"backend_type": "keyword", "table": self._table, "fitted": True}

    def clear(self) -> None:
        clear = getattr(self._backend, "clear", None)
        if callable(clear):
            clear()
        self._fitted_items = None


def _search_fitted_items(
    query: str,
    items: Sequence[tuple[str, str]],
    top_k: int,
) -> list[tuple[str, float]]:
    query_terms = set(_tokenize(query))
    if not query_terms:
        return []

    scored: list[tuple[str, float]] = []
    denominator = len(query_terms)
    for item_id, content in items:
        content_terms = set(_tokenize(content))
        if not content_terms:
            continue
        matched = len(query_terms & content_terms)
        if matched:
            scored.append((item_id, matched / denominator))

    scored.sort(key=lambda row: (-row[1], row[0]))
    return scored[:top_k]


def _tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def fetch_all(hub: Any, sql: str, params: Sequence[Any]) -> list[Any]:
    """Fetch rows using direct DB methods or HubDatabase transactions."""
    if hasattr(hub, "fetchall"):
        return list(hub.fetchall(sql, tuple(params)))
    with hub.transaction() as txn:
        return list(txn.execute(sql, params).fetchall())


def fetch_one(hub: Any, sql: str, params: Sequence[Any]) -> Any | None:
    """Fetch one row using direct DB methods or HubDatabase transactions."""
    if hasattr(hub, "fetchone"):
        return hub.fetchone(sql, tuple(params))
    with hub.transaction() as txn:
        return txn.execute(sql, params).fetchone()


def row_value(row: Any, key: str) -> Any:
    """Read a column value from mapping or tuple-like rows."""
    if isinstance(row, Mapping):
        return row[key]
    return row[key]


def placeholder(hub: Any, index: int) -> str:
    """Return the placeholder token for the active execution surface."""
    return "%s"


def _add_param(hub: Any, params: list[Any], value: Any) -> str:
    params.append(value)
    return placeholder(hub, len(params))


def _filter_clauses(
    hub: Any,
    params: list[Any],
    alias: str,
    config: _TableConfig,
    filters: Mapping[str, Any],
) -> list[str]:
    clauses: list[str] = []
    columns = config.filters or {}
    for filter_name, value in filters.items():
        if value is None or filter_name not in columns:
            continue
        placeholder_token = _add_param(hub, params, value)
        clauses.append(f"{alias}.{columns[filter_name]} = {placeholder_token}")
    return clauses


def _table_config(table: str) -> _TableConfig:
    try:
        return _TABLE_CONFIGS[table]
    except KeyError as exc:
        raise ValueError(f"unsupported keyword search table: {table}") from exc


def sanitize_pg_search_query(query: str) -> str:
    """Sanitize user input for pg_search's BM25 query DSL."""
    cleaned = "".join(ch if ch.isalnum() or ch in (" ", "_", "-") else " " for ch in query)
    return " ".join(token for token in cleaned.split() if token)


def normalize_positive_scores(raw_scores: list[float]) -> list[float]:
    """Normalize positive scores to the 0..1 range while preserving rank order."""
    if not raw_scores:
        return []
    max_score = max(raw_scores) if raw_scores else 1.0
    if max_score <= 0:
        return [0.0] * len(raw_scores)
    return [score / max_score for score in raw_scores]
