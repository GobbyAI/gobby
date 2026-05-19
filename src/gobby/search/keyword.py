"""Dialect-aware keyword search backends for hub storage."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from gobby.search.fts5 import sanitize_fts_query

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
    sqlite_fts_table: str
    sqlite_content_table: str | None
    sqlite_id_column: str = "id"
    sqlite_weights: tuple[float, ...] | None = None
    postgres_columns: tuple[str, ...] = ()
    filters: Mapping[str, str] | None = None


_TABLE_CONFIGS: dict[str, _TableConfig] = {
    "tasks": _TableConfig(
        table="tasks",
        sqlite_fts_table="tasks_fts",
        sqlite_content_table="tasks",
        sqlite_weights=(10.0, 5.0, 2.0, 1.0, 2.0),
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
        sqlite_fts_table="memories_fts",
        sqlite_content_table="memories",
        sqlite_weights=(10.0, 5.0, 1.0, 1.0),
        postgres_columns=("content", "tags_text"),
        filters={"project_id": "project_id"},
    ),
    "skills": _TableConfig(
        table="skills",
        sqlite_fts_table="skills_fts",
        sqlite_content_table=None,
        sqlite_weights=(10.0, 5.0, 2.0, 2.0),
        postgres_columns=("name", "description", "content"),
    ),
    "code_symbols": _TableConfig(
        table="code_symbols",
        sqlite_fts_table="code_symbols_fts",
        sqlite_content_table="code_symbols",
        postgres_columns=("name", "qualified_name", "signature", "docstring", "summary"),
        filters={"project_id": "project_id", "kind": "kind", "file_path": "file_path"},
    ),
    "code_content": _TableConfig(
        table="code_content_chunks",
        sqlite_fts_table="code_content_fts",
        sqlite_content_table="code_content_chunks",
        postgres_columns=("content",),
        filters={"project_id": "project_id", "file_path": "file_path"},
    ),
}

_FTS_TABLE_TO_TABLE = {config.sqlite_fts_table: name for name, config in _TABLE_CONFIGS.items()}


def keyword_table_for_fts_table(fts_table: str) -> str:
    """Return the backend-neutral table key for an FTS5 table."""
    try:
        return _FTS_TABLE_TO_TABLE[fts_table]
    except KeyError as exc:
        raise ValueError(f"unsupported FTS table: {fts_table}") from exc


def pick_search_backend(
    hub: Any,
    table: str,
    mode: SearchMode = "keyword",
) -> KeywordSearchBackend:
    """Pick the dialect-specific keyword search backend for a hub database."""
    if mode == "semantic":
        raise NotImplementedError(
            "Semantic search is a follow-up workstream; use mode='keyword' today."
        )
    config = _table_config(table)
    if _dialect(hub) == "sqlite":
        return FTS5SearchBackend(hub, config)
    return BM25SearchBackend(hub, config)


class FTS5SearchBackend:
    """SQLite FTS5 keyword backend."""

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
        fts_query = sanitize_fts_query(query)
        if not fts_query:
            return []

        params: list[Any] = []
        where: list[str] = []
        match_placeholder = _add_param(self._hub, params, fts_query)
        where.append(f"{self._config.sqlite_fts_table} MATCH {match_placeholder}")

        content_alias = "ct"
        if filters and self._config.sqlite_content_table:
            where.extend(_filter_clauses(self._hub, params, content_alias, self._config, filters))

        limit_placeholder = _add_param(self._hub, params, limit)
        weights_csv = (
            ", ".join(str(weight) for weight in self._config.sqlite_weights)
            if self._config.sqlite_weights
            else ""
        )
        bm25_expr = (
            f"bm25({self._config.sqlite_fts_table}, {weights_csv})"
            if weights_csv
            else f"bm25({self._config.sqlite_fts_table})"
        )

        if self._config.sqlite_content_table:
            sql = f"""
                SELECT {content_alias}.{self._config.sqlite_id_column} AS id,
                       {bm25_expr} AS score
                  FROM {self._config.sqlite_fts_table} fts
                  JOIN {self._config.sqlite_content_table} {content_alias}
                    ON {content_alias}.rowid = fts.rowid
                 WHERE {" AND ".join(where)}
                 ORDER BY score ASC, id ASC
                 LIMIT {limit_placeholder}
            """
        else:
            sql = f"""
                SELECT fts.rowid AS id, {bm25_expr} AS score
                  FROM {self._config.sqlite_fts_table} fts
                 WHERE {" AND ".join(where)}
                 ORDER BY score ASC, id ASC
                 LIMIT {limit_placeholder}
            """

        try:
            rows = fetch_all(self._hub, sql, params)
        except Exception as exc:
            logger.debug("FTS5 search failed on %s: %s", self._config.sqlite_fts_table, exc)
            return []

        raw_scores = [float(row_value(row, "score")) for row in rows]
        normalized = _normalize_fts5_scores(raw_scores)
        return [
            SearchHit(id=str(row_value(row, "id")), score=score)
            for row, score in zip(rows, normalized, strict=False)
        ]

    def get_stats(self) -> dict[str, Any]:
        try:
            row = fetch_one(
                self._hub,
                f"SELECT count(*) AS cnt FROM {self._config.sqlite_fts_table}",
                [],
            )
            count = int(row_value(row, "cnt")) if row else 0
        except Exception:
            count = 0
        return {
            "backend_type": "fts5",
            "fts_table": self._config.sqlite_fts_table,
            "content_table": self._config.sqlite_content_table,
            "document_count": count,
            "fitted": True,
        }

    def clear(self) -> None:
        if self._config.sqlite_content_table is None:
            execute_statement(
                self._hub,
                f"INSERT INTO {self._config.sqlite_fts_table}"
                f"({self._config.sqlite_fts_table}) VALUES ('delete-all')",
                [],
            )
            return
        execute_statement(self._hub, f"DELETE FROM {self._config.sqlite_fts_table}", [])


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
        bm25_query = _sanitize_pg_search_query(query)
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
        normalized = _normalize_positive_scores(raw_scores)
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

    async def fit_async(self, _items: list[tuple[str, str]]) -> None:
        return None

    async def search_async(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        return [(hit.id, hit.score) for hit in self._backend.search(query, top_k)]

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        return [(hit.id, hit.score) for hit in self._backend.search(query, top_k)]

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


def fetch_all(hub: Any, sql: str, params: Sequence[Any]) -> list[Any]:
    """Fetch rows using either legacy direct DB methods or HubDatabase transactions."""
    if hasattr(hub, "fetchall"):
        return list(hub.fetchall(sql, tuple(params)))
    with hub.transaction() as txn:
        return list(txn.execute(sql, params).fetchall())


def fetch_one(hub: Any, sql: str, params: Sequence[Any]) -> Any | None:
    """Fetch one row using either legacy direct DB methods or HubDatabase transactions."""
    if hasattr(hub, "fetchone"):
        return hub.fetchone(sql, tuple(params))
    with hub.transaction() as txn:
        return txn.execute(sql, params).fetchone()


def execute_statement(hub: Any, sql: str, params: Sequence[Any]) -> None:
    """Execute a statement through either database surface."""
    if hasattr(hub, "execute"):
        hub.execute(sql, tuple(params))
        return
    with hub.transaction() as txn:
        txn.execute(sql, params)


def row_value(row: Any, key: str) -> Any:
    """Read a column value from sqlite rows, dict rows, or tuple-like rows."""
    if isinstance(row, Mapping):
        return row[key]
    return row[key]


def uses_direct_placeholders(hub: Any) -> bool:
    """Return True when SQL is executed directly against sqlite3-style methods."""
    return hasattr(hub, "fetchall") or hasattr(hub, "execute") or hasattr(hub, "fetchone")


def placeholder(hub: Any, index: int) -> str:
    """Return the placeholder token for the active execution surface."""
    if _dialect(hub) == "sqlite" and uses_direct_placeholders(hub):
        return "?"
    return f"${index}"


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


def _dialect(hub: Any) -> Literal["sqlite", "postgres"]:
    return "postgres" if getattr(hub, "dialect", "sqlite") == "postgres" else "sqlite"


def _sanitize_pg_search_query(query: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in (" ", "_", "-") else " " for ch in query)
    return " ".join(token for token in cleaned.split() if token)


def _normalize_fts5_scores(raw_scores: list[float]) -> list[float]:
    positive = [-score for score in raw_scores]
    return _normalize_positive_scores(positive)


def _normalize_positive_scores(raw_scores: list[float]) -> list[float]:
    if not raw_scores:
        return []
    max_score = max(raw_scores) if raw_scores else 1.0
    if max_score <= 0:
        return [0.0] * len(raw_scores)
    return [score / max_score for score in raw_scores]
