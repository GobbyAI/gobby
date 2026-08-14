"""Keyword search backends for hub storage."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

SearchMode = Literal["keyword", "semantic"]
MemoryKeywordScope = Literal["global_only", "project_only", "project_and_global"]

logger = logging.getLogger(__name__)


class SearchQuerySyntaxError(ValueError):
    """Raised when pg_search cannot parse a user search query."""

    def __init__(self, query: str) -> None:
        self.query = query
        super().__init__(
            f"Search query could not be parsed: {query!r}. "
            "Simplify the query to plain words and retry."
        )


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
        allowed_ids: Collection[str] | None = None,
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
    # Static unconditional SQL predicate (no user input, no parameters) appended
    # to every search's WHERE.
    # The shared ``filters`` mapping only expresses column equality, so an ``IS NULL``
    # visibility gate cannot be expressed there. Memory keyword search uses this to keep
    # soft-hidden rows (``deleted_at IS NOT NULL``) out of recall.
    active_clause: str | None = None
    tie_break_columns: tuple[str, ...] = ("id",)


_TABLE_CONFIGS: dict[str, _TableConfig] = {
    "tasks": _TableConfig(
        table="tasks",
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
        filters={"project_id": "project_id", "is_global": "is_global"},
        active_clause="deleted_at IS NULL",
        # id is unique; extra ORDER BY columns are not in memories_search_bm25 and
        # disable ParadeDB Top-K (a 25-way Dream UNION ALL then misses its 30s budget).
        tie_break_columns=("id",),
    ),
    "skills": _TableConfig(
        table="skills",
        aliases=("skills_fts",),
        postgres_columns=("name", "description", "content"),
        filters={"project_id": "project_id", "enabled": "enabled"},
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
    "tool_result_chunks": _TableConfig(
        table="tool_result_chunks",
        postgres_columns=("content",),
        filters={"result_id": "result_id"},
        tie_break_columns=("ordinal", "id"),
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
        allowed_ids: Collection[str] | None = None,
    ) -> list[SearchHit]:
        statement = render_keyword_search_statement(
            self._hub,
            self._config,
            query,
            limit,
            filters=filters,
            allowed_ids=allowed_ids,
        )
        if statement is None:
            return []
        sql, params = statement

        try:
            rows = fetch_all(self._hub, sql, params)
        except Exception as exc:
            if is_pg_search_parse_error(exc):
                raise SearchQuerySyntaxError(query) from exc
            raise

        return map_keyword_search_rows(rows)

    def get_stats(self) -> dict[str, Any]:
        try:
            row = fetch_one(
                self._hub,
                f"SELECT count(*) AS cnt FROM {self._config.table}",  # nosec
                [],
            )
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


def render_keyword_search_statement(
    hub: Any,
    table: str | _TableConfig,
    query: str,
    limit: int,
    *,
    filters: Mapping[str, Any] | None = None,
    allowed_ids: Collection[str] | None = None,
) -> tuple[str, tuple[Any, ...]] | None:
    """Render the exact PostgreSQL statement used by sync and async consumers."""
    bm25_query = sanitize_pg_search_query(query)
    if not bm25_query or (allowed_ids is not None and not allowed_ids):
        return None

    config = table if isinstance(table, _TableConfig) else _table_config(table)
    params: list[Any] = []
    search_clauses = [
        f"{column} @@@ {_add_param(hub, params, bm25_query)}" for column in config.postgres_columns
    ]
    where = [f"({' OR '.join(search_clauses)})"]
    if filters:
        where.extend(_filter_clauses(hub, params, config.table, config, filters))
    if allowed_ids is not None:
        id_placeholders = [_add_param(hub, params, item_id) for item_id in allowed_ids]
        where.append(f"{config.table}.id IN ({', '.join(id_placeholders)})")
    if config.active_clause:
        where.append(config.active_clause)

    limit_placeholder = _add_param(hub, params, limit)
    order_by = ", ".join(["score DESC", *(f"{column} ASC" for column in config.tie_break_columns)])
    sql = (
        f"SELECT id, pdb.score(id) AS score FROM {config.table} "  # nosec
        f"WHERE {' AND '.join(where)} ORDER BY {order_by} LIMIT {limit_placeholder}"
    )
    return sql, tuple(params)


def map_keyword_search_rows(rows: Sequence[Any]) -> list[SearchHit]:
    """Map PostgreSQL keyword rows to normalized search hits."""
    raw_scores = [float(row_value(row, "score")) for row in rows]
    normalized = normalize_positive_scores(raw_scores)
    return [
        SearchHit(id=str(row_value(row, "id")), score=score)
        for row, score in zip(rows, normalized, strict=False)
    ]


class KeywordAsyncSearchBackend:
    """Async adapter for UnifiedSearcher keyword mode."""

    def __init__(self, hub: Any, table: str) -> None:
        self._hub = hub
        self._table = table
        self._backend = pick_search_backend(hub, table)
        self._fitted_items: list[tuple[str, str]] | None = None
        self._fitted_ids: tuple[str, ...] | None = None
        self._needs_refit = False

    async def fit_async(self, items: list[tuple[str, str]]) -> None:
        fit = getattr(self._backend, "fit", None)
        if callable(fit):
            fit(items)
        self._fitted_items = items.copy()
        self._fitted_ids = tuple(item_id for item_id, _content in items)
        self._needs_refit = False
        return None

    async def search_async(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        return await asyncio.to_thread(self.search, query, top_k)

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        fitted_items = self._fitted_items
        fitted_id_values = self._fitted_ids
        if fitted_items is None or fitted_id_values is None:
            return []
        if not fitted_id_values:
            return []
        fitted_ids = set(fitted_id_values)
        try:
            return [
                (hit.id, hit.score)
                for hit in self._backend.search(
                    query,
                    top_k,
                    allowed_ids=fitted_id_values,
                )
                if hit.id in fitted_ids
            ]
        except Exception:
            logger.debug("Keyword backend search failed; using fitted item fallback", exc_info=True)
            return _search_fitted_items(query, fitted_items, top_k)

    def needs_refit(self) -> bool:
        return self._needs_refit

    def mark_update(self) -> None:
        self._needs_refit = True

    def get_stats(self) -> dict[str, Any]:
        return {
            "backend_type": "pg_search_bm25",
            "table": self._table,
            "document_count": len(self._fitted_ids or ()),
            "fitted": self._fitted_ids is not None,
        }

    def clear(self) -> None:
        clear = getattr(self._backend, "clear", None)
        if callable(clear):
            clear()
        self._fitted_items = None
        self._fitted_ids = None
        self._needs_refit = False


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
    return sanitize_pg_search_query(value.casefold()).split()


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
    memory_scope = filters.get("memory_scope") if config.table == "memories" else None
    if memory_scope is not None:
        project_id = filters.get("project_id")
        if memory_scope == "global_only":
            clauses.append(f"{alias}.is_global IS TRUE")
        elif memory_scope in {"project_only", "project_and_global"}:
            if not isinstance(project_id, str) or not project_id:
                raise ValueError(f"{memory_scope} keyword scope requires project_id")
            project_placeholder = _add_param(hub, params, project_id)
            if memory_scope == "project_only":
                clauses.append(
                    f"{alias}.project_id = {project_placeholder} AND {alias}.is_global IS FALSE"
                )
            else:
                clauses.append(
                    f"({alias}.project_id = {project_placeholder} OR {alias}.is_global IS TRUE)"
                )
        else:
            raise ValueError(f"unsupported memory keyword scope: {memory_scope!r}")

    for filter_name, value in filters.items():
        if memory_scope is not None and filter_name in {
            "memory_scope",
            "project_id",
            "include_global",
            "is_global",
        }:
            continue
        if config.table == "memories" and filter_name == "include_global":
            continue
        if filter_name not in columns:
            raise ValueError(
                f"unsupported filter {filter_name!r} for keyword search table {config.table!r}"
            )
        if value is None:
            continue
        placeholder_token = _add_param(hub, params, value)
        column = columns[filter_name]
        if config.table == "memories" and filter_name == "project_id":
            include_global = bool(filters.get("include_global", True))
            if include_global:
                clauses.append(
                    f"({alias}.{column} = {placeholder_token} OR {alias}.is_global IS TRUE)"
                )
            else:
                clauses.append(f"{alias}.{column} = {placeholder_token}")
        else:
            clauses.append(f"{alias}.{column} = {placeholder_token}")
    return clauses


def _table_config(table: str) -> _TableConfig:
    try:
        return _TABLE_CONFIGS[table]
    except KeyError as exc:
        raise ValueError(f"unsupported keyword search table: {table}") from exc


MAX_PG_SEARCH_QUERY_CHARS = 1_000
"""Maximum caller-controlled query length accepted by offload search surfaces."""


def sanitize_pg_search_query(query: str) -> str:
    """Sanitize user input for pg_search's BM25 query DSL."""
    cleaned = "".join(ch if ch.isalnum() or ch in (" ", "_") else " " for ch in query)
    tokens = (token for token in cleaned.split() if token.strip("_"))
    return " ".join(
        token.lower() if token.upper() in {"AND", "OR", "NOT"} else token for token in tokens
    )


def is_pg_search_parse_error(error: BaseException) -> bool:
    """Return whether an exception is a known pg_search query parser failure."""
    message = str(error).lower()
    return "could not parse query string" in message


def normalize_positive_scores(raw_scores: list[float]) -> list[float]:
    """Normalize positive scores to the 0..1 range while preserving rank order."""
    if not raw_scores:
        return []
    max_score = max(raw_scores) if raw_scores else 1.0
    if max_score <= 0:
        return [0.0] * len(raw_scores)
    return [score / max_score for score in raw_scores]
