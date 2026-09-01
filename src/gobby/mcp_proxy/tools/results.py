"""Retrieval tools for oversized MCP results stored out of band."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from functools import partial
from typing import Any

from gobby.config.features import ToolResultOffloadConfig
from gobby.mcp_proxy.services.result_offload import (
    _WRAPPER_MUTATION_RESERVE,
    _fit_text_to_budget,
    _serialized_size,
)
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.search.keyword import (
    MAX_PG_SEARCH_QUERY_CHARS,
    SearchHit,
    pick_search_backend,
    sanitize_pg_search_query,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tool_results import ToolResultMeta, ToolResultSlice, ToolResultStore
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)

_MAX_SEARCH_LIMIT = 50
_MAX_SLICE_CHARS = 1_000_000 - _WRAPPER_MUTATION_RESERVE
_DEFAULT_SLICE_CHARS = 1_000
_RESULT_ID_SCHEMA: dict[str, object] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
}


def create_results_registry(
    db: HubDatabase,
    config_resolver: ToolResultOffloadConfig | Callable[[], ToolResultOffloadConfig],
    *,
    default_project_id: str | None = None,
    project_id_getter: Callable[[], str | None] | None = None,
) -> InternalToolRegistry:
    """Create the scoped tool-result retrieval registry."""

    registry = InternalToolRegistry(
        name="gobby-results",
        description="Retrieve oversized MCP tool results by result_id",
    )
    resolve_config = config_resolver if callable(config_resolver) else lambda: config_resolver
    store = ToolResultStore(db, resolve_config)
    search_backend = pick_search_backend(db, "tool_result_chunks")

    def current_project_id() -> str | None:
        if project_id_getter is not None:
            return project_id_getter()
        context = get_project_context()
        context_project_id = context.get("id") if context is not None else None
        if isinstance(context_project_id, str):
            return context_project_id
        return default_project_id

    def search_tool_result(
        result_id: str,
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        config = resolve_config()
        response_limit = config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
        canonical_id = _canonical_result_id(result_id)
        if canonical_id is None:
            return _not_found(config)
        validation_error = _validate_search_arguments(query=query, limit=limit)
        if validation_error is not None:
            return validation_error

        project_id = current_project_id()
        if project_id is None:
            return _not_found(config)

        try:
            meta = store.get_meta(canonical_id, project_id)
        except Exception:
            logger.exception("Failed to read tool result metadata")
            return _bounded_error("tool result search unavailable", response_limit)
        if meta is None:
            return _not_found(config)

        sanitized_query = sanitize_pg_search_query(query)
        if not sanitized_query:
            return _empty_search_result(meta)

        try:
            hits = search_backend.search(
                sanitized_query,
                limit,
                filters={"result_id": canonical_id},
            )
            matches = _hydrate_matches(db, result_id=canonical_id, hits=hits)
        except Exception:
            logger.exception("Failed to search stored tool result")
            return _bounded_error("tool result search unavailable", response_limit)

        return _fit_search_result(
            {
                "result_id": canonical_id,
                "total_chars": meta["total_chars"],
                "matches": matches,
            },
            response_limit,
        )

    registry.register(
        name="search_tool_result",
        description="Search chunks within one stored oversized tool result.",
        input_schema={
            "type": "object",
            "properties": {
                "result_id": dict(_RESULT_ID_SCHEMA),
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_PG_SEARCH_QUERY_CHARS,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_SEARCH_LIMIT,
                    "default": 5,
                },
            },
            "required": ["result_id", "query"],
        },
        output_schema={"type": "object"},
        func=search_tool_result,
    )

    def get_tool_result(
        result_id: str,
        offset: int = 0,
        limit: int = _DEFAULT_SLICE_CHARS,
    ) -> dict[str, Any]:
        config = resolve_config()
        response_limit = config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
        canonical_id = _canonical_result_id(result_id)
        if canonical_id is None:
            return _not_found(config)
        validation_error = _validate_slice_arguments(offset=offset, limit=limit)
        if validation_error is not None:
            return validation_error

        project_id = current_project_id()
        if project_id is None:
            return _not_found(config)

        try:
            page = store.get_slice(
                canonical_id,
                project_id,
                offset=offset,
                # The live envelope budget is dynamic, so a limit above it is
                # clamped rather than rejected; next_offset drives paging.
                limit=min(limit, response_limit),
            )
        except Exception:
            logger.exception("Failed to read stored tool result")
            return _bounded_error("tool result retrieval unavailable", response_limit)
        if page is None:
            return _not_found(config)
        return _fit_slice_result(page, response_limit)

    registry.register(
        name="get_tool_result",
        description=(
            "Read a bounded character slice from one stored oversized tool result. "
            "A limit above the live maximum is clamped to it; page with next_offset."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "result_id": dict(_RESULT_ID_SCHEMA),
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_SLICE_CHARS,
                    "default": _DEFAULT_SLICE_CHARS,
                },
            },
            "required": ["result_id"],
        },
        output_schema={"type": "object"},
        func=get_tool_result,
    )

    return registry


def _canonical_result_id(result_id: object) -> str | None:
    if not isinstance(result_id, str):
        return None
    try:
        return str(uuid.UUID(result_id))
    except (AttributeError, TypeError, ValueError):
        return None


def _not_found(config: ToolResultOffloadConfig) -> dict[str, Any]:
    return {
        "success": False,
        "error": (f"result_id not found or expired ({config.retention_days}-day retention)"),
    }


def _invalid_arguments(message: str) -> dict[str, Any]:
    return {"success": False, "error": f"invalid arguments: {message}"}


def _validate_search_arguments(*, query: object, limit: object) -> dict[str, Any] | None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_SEARCH_LIMIT:
        return _invalid_arguments(f"limit must be between 1 and {_MAX_SEARCH_LIMIT}")
    if not isinstance(query, str):
        return _invalid_arguments("query must be a string")
    if not query.strip():
        return _invalid_arguments("query must not be empty")
    if len(query) > MAX_PG_SEARCH_QUERY_CHARS:
        return _invalid_arguments(f"query must be at most {MAX_PG_SEARCH_QUERY_CHARS} characters")
    return None


def _validate_slice_arguments(*, offset: object, limit: object) -> dict[str, Any] | None:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return _invalid_arguments("offset must be non-negative")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return _invalid_arguments("limit must be positive")
    return None


def _empty_search_result(meta: ToolResultMeta) -> dict[str, Any]:
    return {
        "result_id": meta["result_id"],
        "total_chars": meta["total_chars"],
        "matches": [],
    }


def _hydrate_matches(
    db: HubDatabase,
    *,
    result_id: str,
    hits: list[SearchHit],
) -> list[dict[str, Any]]:
    if not hits:
        return []
    rows = db.fetchall(
        """SELECT id, ordinal, start_offset, end_offset, content
           FROM tool_result_chunks
           WHERE result_id = %s AND id = ANY(%s)""",
        (result_id, [hit.id for hit in hits]),
    )
    rows_by_id = {str(row["id"]): row for row in rows}
    matches: list[dict[str, Any]] = []
    for hit in hits:
        row = rows_by_id.get(hit.id)
        if row is None:
            continue
        matches.append(
            {
                "ordinal": int(row["ordinal"]),
                "start_offset": int(row["start_offset"]),
                "end_offset": int(row["end_offset"]),
                "score": hit.score,
                "content": str(row["content"]),
            }
        )
    return matches


def _fits_search_match_content(
    content: str,
    *,
    fitted: dict[str, Any],
    match: dict[str, Any],
    limit: int,
) -> bool:
    return (
        _serialized_size(
            {
                **fitted,
                "matches": [*fitted["matches"], {**match, "content": content}],
            }
        )
        <= limit
    )


def _fit_search_result(result: dict[str, Any], limit: int) -> dict[str, Any]:
    if _serialized_size(result) <= limit:
        return result

    fitted = {
        "result_id": result["result_id"],
        "total_chars": result["total_chars"],
        "matches": [],
    }
    for match in result["matches"]:
        candidate_matches = [*fitted["matches"], match]
        candidate = {**fitted, "matches": candidate_matches}
        if _serialized_size(candidate) <= limit:
            fitted = candidate
            continue

        content = str(match["content"])
        fitted_content = _fit_text_to_budget(
            content,
            partial(
                _fits_search_match_content,
                fitted=fitted,
                match=match,
                limit=limit,
            ),
        )
        if fitted_content:
            fitted = {
                **fitted,
                "matches": [*fitted["matches"], {**match, "content": fitted_content}],
            }
        break
    return fitted


def _fit_slice_result(result: ToolResultSlice, limit: int) -> dict[str, Any]:
    if _serialized_size(result) <= limit:
        return dict(result)

    content = str(result["content"])
    fitted_content = _fit_text_to_budget(
        content,
        lambda shortened: _serialized_size(
            {
                **result,
                "content": shortened,
                "next_offset": (
                    result["offset"] + len(shortened)
                    if result["offset"] + len(shortened) < result["stored_chars"]
                    else None
                ),
            }
        )
        <= limit,
    )
    if not fitted_content:
        return {**result, "content": "", "next_offset": result["offset"]}

    next_offset = result["offset"] + len(fitted_content)
    return {
        **result,
        "content": fitted_content,
        "next_offset": next_offset if next_offset < result["stored_chars"] else None,
    }


def _bounded_error(message: str, limit: int) -> dict[str, Any]:
    result = {"success": False, "error": message}
    if _serialized_size(result) <= limit:
        return result

    return {
        "success": False,
        "error": _fit_text_to_budget(
            message,
            lambda shortened: _serialized_size({"success": False, "error": shortened}) <= limit,
        ),
    }
