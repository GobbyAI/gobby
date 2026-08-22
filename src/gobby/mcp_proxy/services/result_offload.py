"""Bounded offloading for oversized MCP tool results."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from fnmatch import fnmatch
from functools import partial
from typing import Any, Literal, NamedTuple

from mcp.types import CallToolResult, TextContent

from gobby.config.features import ToolResultOffloadConfig
from gobby.hooks.tool_error_tracker import render_bounded_identity
from gobby.hooks.tool_outcomes import ToolOutcomeStatus, classify_raw_tool_result
from gobby.search.keyword import (
    MAX_PG_SEARCH_QUERY_CHARS,
    BM25SearchBackend,
    SearchHit,
    sanitize_pg_search_query,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tool_results import ToolResultStore

logger = logging.getLogger(__name__)

_WRAPPER_MUTATION_RESERVE = 512
_MANDATORY_EXEMPT_TOOLS = (
    "gobby-skills/get_skill",
    "gobby-skills/get_skill_file",
    "gobby-agents/get_inter_session_message",
    "gobby-results/*",
)
_MAX_STRUCTURE_KEYS = 20


class _SerializedResult(NamedTuple):
    text: str
    content_kind: Literal["json", "text"]
    structure: dict[str, Any]


class ToolResultOffloader:
    """Replace oversized successful results with bounded retrieval envelopes."""

    def __init__(
        self,
        store: ToolResultStore,
        db: HubDatabase,
        config: ToolResultOffloadConfig | Callable[[], ToolResultOffloadConfig],
        project_id_getter: Callable[[], str | None],
    ) -> None:
        self._store = store
        self._db = db
        self._config_resolver = config if callable(config) else lambda: config
        self._project_id_getter = project_id_getter
        self._search_backend = BM25SearchBackend(db, "tool_result_chunks")

    @property
    def _config(self) -> ToolResultOffloadConfig:
        return self._config_resolver()

    async def maybe_offload(
        self,
        *,
        server_name: str,
        tool_name: str,
        result: Any,
        session_id: str | None,
        intent: str | None,
        project_id: str | None = None,
    ) -> Any:
        """Return the original result or a bounded retrieval envelope."""
        return await asyncio.to_thread(
            self._maybe_offload_sync,
            server_name=server_name,
            tool_name=tool_name,
            result=result,
            session_id=session_id,
            intent=intent,
            project_id=project_id,
        )

    def _maybe_offload_sync(
        self,
        *,
        server_name: str,
        tool_name: str,
        result: Any,
        session_id: str | None,
        intent: str | None,
        project_id: str | None,
    ) -> Any:
        if not self._config.enabled:
            return result
        identity = f"{server_name}/{tool_name}"
        exemption_patterns = (*_MANDATORY_EXEMPT_TOOLS, *self._config.exempt_tools)
        if any(fnmatch(identity, pattern) for pattern in exemption_patterns):
            return result
        if classify_raw_tool_result(result).status is ToolOutcomeStatus.FAILED:
            return result
        if _has_non_text_content(result):
            return result

        try:
            serialized = _serialize_success_result(result)
        except Exception:
            logger.warning(
                "Tool result serialization failed; preserving original result",
                extra={"server_name": server_name, "tool_name": tool_name},
                exc_info=True,
            )
            return result

        if len(serialized.text) <= self._config.threshold_chars:
            return result

        total_chars = len(serialized.text)
        if total_chars > self._config.max_stored_chars:
            return self._too_large_envelope(
                server_name=server_name,
                tool_name=tool_name,
                serialized=serialized,
                total_chars=total_chars,
            )
        resolved_project_id = project_id
        try:
            if not resolved_project_id:
                resolved_project_id = self._project_id_getter()
            if not resolved_project_id:
                raise ValueError("project context is unavailable")
            result_id = self._store.save(
                project_id=resolved_project_id,
                session_id=session_id,
                server_name=server_name,
                tool_name=tool_name,
                content=serialized.text,
                content_kind=serialized.content_kind,
                total_chars=total_chars,
            )
        except Exception:
            logger.warning(
                "Tool result persistence failed; returning bounded inline fallback",
                extra={
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "project_id": resolved_project_id,
                },
                exc_info=True,
            )
            return self._build_envelope(
                server_name=server_name,
                tool_name=tool_name,
                serialized=serialized,
                total_chars=total_chars,
                stored_chars=0,
                result_id=None,
                matches=None,
            )

        matches: list[dict[str, Any]] | None = None
        sanitized_intent = _bounded_intent(intent)
        if sanitized_intent is not None:
            try:
                matches = self._search_matches(
                    result_id=result_id,
                    intent=sanitized_intent,
                )
            except Exception:
                logger.warning(
                    "Tool result intent search failed; returning persisted envelope",
                    extra={
                        "server_name": server_name,
                        "tool_name": tool_name,
                        "result_id": result_id,
                    },
                    exc_info=True,
                )

        return self._build_envelope(
            server_name=server_name,
            tool_name=tool_name,
            serialized=serialized,
            total_chars=total_chars,
            stored_chars=total_chars,
            result_id=result_id,
            matches=matches,
        )

    def _search_matches(self, *, result_id: str, intent: str) -> list[dict[str, Any]]:
        if self._config.intent_match_limit == 0:
            return []
        hits = self._search_backend.search(
            intent,
            self._config.intent_match_limit,
            filters={"result_id": result_id},
        )
        return [
            match
            for hit in hits
            if (match := self._load_match(result_id=result_id, hit=hit)) is not None
        ]

    def _load_match(self, *, result_id: str, hit: SearchHit) -> dict[str, Any] | None:
        row = self._db.fetchone(
            """SELECT ordinal, start_offset, end_offset, content
               FROM tool_result_chunks
               WHERE id = %s AND result_id = %s""",
            (hit.id, result_id),
        )
        if row is None:
            return None
        return {
            "ordinal": int(row["ordinal"]),
            "start_offset": int(row["start_offset"]),
            "end_offset": int(row["end_offset"]),
            "score": hit.score,
            "content": str(row["content"]),
        }

    def _too_large_envelope(
        self,
        *,
        server_name: str,
        tool_name: str,
        serialized: _SerializedResult,
        total_chars: int,
    ) -> dict[str, Any]:
        cap = self._config.max_stored_chars
        return {
            "offloaded": False,
            "stored": False,
            "reason": "too_large",
            "server_name": render_bounded_identity(server_name),
            "tool_name": render_bounded_identity(tool_name),
            "content_kind": serialized.content_kind,
            "total_chars": total_chars,
            "stored_chars": 0,
            "retrieval_available": False,
            "guidance": (
                f"Output is {total_chars} chars, over the {cap} char storage cap. "
                "The result was not stored; the tail is not retrievable from Gobby. "
                "Re-query the tool with a narrower request."
            ),
        }

    def _build_envelope(
        self,
        *,
        server_name: str,
        tool_name: str,
        serialized: _SerializedResult,
        total_chars: int,
        stored_chars: int,
        result_id: str | None,
        matches: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        retrieval_available = result_id is not None
        envelope: dict[str, Any] = {
            "offloaded": True,
            "server_name": render_bounded_identity(server_name),
            "tool_name": render_bounded_identity(tool_name),
            "content_kind": serialized.content_kind,
            "total_chars": total_chars,
            "stored_chars": stored_chars,
            "retrieval_available": retrieval_available,
            "guidance": self._guidance(
                result_id=result_id,
                total_chars=total_chars,
                stored_chars=stored_chars,
            ),
        }
        if result_id is not None:
            envelope["result_id"] = result_id
        if matches is not None:
            envelope["matches"] = []

        limit = self._config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
        envelope["structure"] = _fit_structure(envelope, serialized.structure, limit)
        envelope["preview"] = _fit_text_field(
            envelope,
            "preview",
            serialized.text[: self._config.preview_chars],
            limit,
        )
        if matches is not None:
            _fit_matches(envelope, matches, limit)

        if _serialized_size(envelope) > limit:
            logger.warning(
                "Tool result envelope exceeded its configured working budget; degrading envelope",
                extra={
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "result_id": result_id,
                    "working_budget": limit,
                },
            )
            degraded: dict[str, Any] = {
                "offloaded": True,
                "server_name": render_bounded_identity(server_name),
                "tool_name": render_bounded_identity(tool_name),
                "total_chars": total_chars,
                "stored_chars": stored_chars,
                "retrieval_available": retrieval_available,
            }
            if result_id is not None:
                degraded["result_id"] = result_id
            return degraded
        return envelope

    def _guidance(
        self,
        *,
        result_id: str | None,
        total_chars: int,
        stored_chars: int,
    ) -> str:
        threshold = self._config.threshold_chars
        retention = self._config.retention_days
        if result_id is None:
            return (
                f"Output exceeded {threshold} chars. Storage failed, so retrieval is unavailable; "
                "the omitted tail cannot be retrieved from Gobby. The source system still holds "
                "the original."
            )
        return (
            f"Output exceeded {threshold} chars and is retrievable for {retention} days. "
            "Search it: call_tool('gobby-results','search_tool_result',"
            f"{{'result_id':'{result_id}','query':'...'}}). "
            "Page raw content: get_tool_result. Tip: pass intent='<what you need>' on call_tool "
            "to get matched sections directly."
        )


def _has_non_text_content(result: object) -> bool:
    return isinstance(result, CallToolResult) and any(
        not isinstance(item, TextContent) for item in result.content
    )


def _serialize_success_result(result: object) -> _SerializedResult:
    payload: object = result
    if isinstance(result, CallToolResult):
        if result.structured_content is not None:
            payload = result.structured_content
        else:
            payload = "\n".join(
                item.text for item in result.content if isinstance(item, TextContent)
            )

    if isinstance(payload, str):
        return _SerializedResult(payload, "text", {"type": "text"})

    json_payload = dict(payload) if isinstance(payload, Mapping) else payload
    text = json.dumps(json_payload, indent=2, default=str)
    return _SerializedResult(text, "json", _summarize_structure(json_payload))


def _summarize_structure(payload: object) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        items = list(payload.items())
        keys = {
            render_bounded_identity(str(key)): _describe_value(value)
            for key, value in items[:_MAX_STRUCTURE_KEYS]
        }
        if len(items) > _MAX_STRUCTURE_KEYS:
            keys["…"] = f"{len(items) - _MAX_STRUCTURE_KEYS} more keys"
        return {"type": "object", "keys": keys}
    if isinstance(payload, (list, tuple)):
        return {"type": "list", "length": len(payload)}
    return {"type": type(payload).__name__}


def _describe_value(value: object) -> str:
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return f"list[{len(value)}]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _bounded_intent(intent: str | None) -> str | None:
    if intent is None:
        return None
    sanitized = sanitize_pg_search_query(intent[:MAX_PG_SEARCH_QUERY_CHARS])
    return sanitized or None


def _serialized_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _fit_text_to_budget(text: str, fits: Callable[[str], bool]) -> str:
    if fits(text):
        return text

    low = 0
    high = len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        if fits(text[:midpoint]):
            low = midpoint
        else:
            high = midpoint - 1
    return text[:low]


def _fits(envelope: dict[str, Any], key: str, value: object, limit: int) -> bool:
    candidate = dict(envelope)
    candidate[key] = value
    return _serialized_size(candidate) <= limit


def _fits_match_content(
    content: str,
    *,
    envelope: dict[str, Any],
    fitted: list[dict[str, Any]],
    candidate: dict[str, Any],
    limit: int,
) -> bool:
    return _fits(
        envelope,
        "matches",
        [*fitted, {**candidate, "content": content}],
        limit,
    )


def _fit_structure(
    envelope: dict[str, Any],
    structure: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    if _fits(envelope, "structure", structure, limit):
        return structure
    if structure.get("type") != "object":
        return {"type": structure.get("type", "unknown")}

    fitted: dict[str, Any] = {"type": "object", "keys": {}}
    source_keys = structure.get("keys")
    if not isinstance(source_keys, Mapping):
        return fitted
    for key, value in source_keys.items():
        candidate_keys = {**fitted["keys"], str(key): value}
        candidate = {"type": "object", "keys": candidate_keys}
        if not _fits(envelope, "structure", candidate, limit):
            break
        fitted = candidate
    return fitted


def _fit_text_field(
    envelope: dict[str, Any],
    key: str,
    text: str,
    limit: int,
) -> str:
    return _fit_text_to_budget(
        text,
        lambda candidate: _fits(envelope, key, candidate, limit),
    )


def _fit_matches(
    envelope: dict[str, Any],
    matches: list[dict[str, Any]],
    limit: int,
) -> None:
    fitted = envelope["matches"]
    if not isinstance(fitted, list):
        return
    for match in matches:
        metadata = {key: value for key, value in match.items() if key != "content"}
        candidate = {**metadata, "content": ""}
        if not _fits(envelope, "matches", [*fitted, candidate], limit):
            break
        original_content = str(match.get("content", ""))
        candidate["content"] = _fit_text_to_budget(
            original_content,
            partial(
                _fits_match_content,
                envelope=envelope,
                fitted=fitted,
                candidate=candidate,
                limit=limit,
            ),
        )
        fitted.append(candidate)


__all__ = ["ToolResultOffloader"]
