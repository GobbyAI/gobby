"""Message retrieval and search tools for session management.

This module contains MCP tools for:
- Getting messages for a session (get_session_messages)
- Searching rendered transcript messages (search_session_messages)
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from gobby.sessions.transcript_limits import RENDERED_LIMIT_MAX

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.sessions.transcript_reader import TranscriptReader
    from gobby.storage.sessions import SessionManager

MAX_SEARCH_SESSIONS: int = 100
# Rendered message groups one search call may scan across all sessions; keeps a
# miss well inside the MCP proxy timeout. Callers continue with ``next_cursor``.
SEARCH_GROUP_BUDGET: int = 2000


def register_message_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager | None = None,
    transcript_reader: TranscriptReader | None = None,
) -> None:
    """
    Register message retrieval and search tools with a registry.

    Args:
        registry: The InternalToolRegistry to register tools with
        session_manager: SessionManager for resolving session references
        transcript_reader: Optional TranscriptReader for JSONL + gzip fallback reads
    """

    from gobby.sessions.transcript_search import search_rendered_messages
    from gobby.utils.session_context import resolve_session_ref

    def _resolve_session_id(session_id: str) -> str:
        return resolve_session_ref(session_manager, session_id)

    @registry.tool(
        name="get_session_messages",
        read_only=True,
        description="Get messages for a session. Returns rendered messages with content blocks. Accepts <project>#N, local #N/N, UUID, or prefix for session_id.",
    )
    # Entry point for get_session_messages tool
    async def get_session_messages(
        session_id: str,
        limit: int = 50,
        offset: int = 0,
        full_content: bool = True,
    ) -> dict[str, Any]:
        """
        Get messages for a session.

        Args:
            session_id: Session reference - supports <project>#N, local #N/N, UUID, or prefix
            limit: Max rendered groups to return, capped at RENDERED_LIMIT_MAX; the
                response's limit is the effective, capped value.
            offset: Rendered-group offset; advance by the response's returned_count.
                total_count is the rendered-group total, so offset=total_count-limit
                returns the last page.
            full_content: Unused. Content is always returned in full.
        """
        try:
            resolved_id = _resolve_session_id(session_id)
            _ = full_content

            # Use TranscriptReader (windowed; JSONL + gzip fallback)
            if transcript_reader:
                clamped = min(max(int(limit), 1), RENDERED_LIMIT_MAX)
                result = await transcript_reader.get_rendered_window(
                    session_id=resolved_id,
                    limit=clamped,
                    offset=offset,
                    order="head",
                )
                messages = [m.to_dict() for m in result.groups]
            else:
                return {
                    "success": False,
                    "error": "Message retrieval not available (TranscriptReader not configured)",
                }

            return {
                "success": True,
                "messages": messages,
                "total_count": result.total_groups,
                "returned_count": result.returned_count,
                "limit": clamped,
                "offset": offset,
                "truncated": False,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="search_session_messages",
        read_only=True,
        description=(
            "Search rendered transcript messages by substring. Accepts <project>#N, local #N/N, "
            "UUID, or prefix for session_id. Each call scans a bounded number of messages; "
            "when truncated, pass next_cursor back as cursor to continue."
        ),
    )
    async def search_session_messages(
        query: str,
        session_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 20,
        full_content: bool = True,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """
        Search rendered transcript messages.

        Each call renders at most ``SEARCH_GROUP_BUDGET`` message groups across
        all searched sessions. When the scan stops early (limit reached or
        budget spent) the response is ``truncated`` and carries ``next_cursor``;
        pass it back as ``cursor`` with the same filters to continue.

        Args:
            query: Search query
            session_id: Optional session filter - supports <project>#N, local #N/N, UUID, or prefix
            project_id: Optional project filter for multi-session search
            status: Optional session status filter for multi-session search
            source: Optional CLI source filter for multi-session search
            limit: Max results
            full_content: Unused. Message bodies are always returned in full.
            cursor: Opaque ``next_cursor`` from a previous truncated response
        """
        if transcript_reader is None:
            return {
                "success": False,
                "error": "Message search not available (TranscriptReader not configured)",
            }

        query = query.strip()
        _ = full_content
        if not query:
            return {"success": False, "error": "query must not be empty"}

        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return {"success": False, "error": "limit must be positive"}
        result_limit = limit
        budget = SEARCH_GROUP_BUDGET

        async def _scan_session(
            sid: str, start: int, collected: list[dict[str, Any]]
        ) -> int | None:
            """Scan one session's rendered groups chronologically from ``start``.

            Pages one resolved snapshot via ``iter_rendered_windows`` (``head``
            order preserves search ordering) within the call's remaining group
            budget. Returns the group to resume from when the limit or budget
            stopped the scan, or None once the session is exhausted.
            """
            nonlocal budget
            group = start
            windows = transcript_reader.iter_rendered_windows(
                sid, order="head", start=start, max_groups=budget
            )
            async for window in windows:
                budget -= window.returned_count
                for message in window.groups:
                    group += 1
                    collected.extend(
                        search_rendered_messages(
                            session_id=sid,
                            messages=[message],
                            query=query,
                            limit=result_limit - len(collected),
                            full_content=full_content,
                        )
                    )
                    if len(collected) >= result_limit:
                        return group if group < window.total_groups else None
                if group >= window.total_groups:
                    return None
            return group if budget <= 0 else None

        try:
            resume: tuple[str, int] | None = None
            if cursor is not None:
                resume = _decode_cursor(cursor)

            if session_id:
                resolved_id = _resolve_session_id(session_id)
                start = 0
                if resume is not None:
                    if resume[0] != resolved_id:
                        return {"success": False, "error": "cursor belongs to another session"}
                    start = resume[1]
                session_results: list[dict[str, Any]] = []
                group = await _scan_session(resolved_id, start, session_results)
                next_cursor = None if group is None else _encode_cursor(resolved_id, group)
                return _search_response(query, session_results, 1, result_limit, next_cursor)

            if session_manager is None:
                return {
                    "success": False,
                    "error": "Multi-session search requires SessionManager",
                }

            # Recency order shifts with every write (the caller's own session
            # included), so pages walk sessions newest-created first and resume
            # from the cursor session's (created_at, id) key.
            from_created_at = None
            from_id = None
            if resume is not None:
                anchor = session_manager.get(resume[0])
                if anchor is None:
                    return {"success": False, "error": "cursor session no longer exists"}
                from_created_at, from_id = anchor.created_at, anchor.id
            ids = [
                session.id
                for session in session_manager.list_newest_created(
                    project_id=project_id,
                    status=status,
                    source=source,
                    limit=MAX_SEARCH_SESSIONS + 1,
                    from_created_at=from_created_at,
                    from_id=from_id,
                )
            ]
            start = resume[1] if resume is not None and ids[:1] == [resume[0]] else 0

            results: list[dict[str, Any]] = []
            searched_sessions = 0
            next_cursor = None
            for position, sid in enumerate(ids):
                if position == MAX_SEARCH_SESSIONS or len(results) >= result_limit or budget <= 0:
                    next_cursor = _encode_cursor(sid, 0)
                    break
                searched_sessions += 1
                group = await _scan_session(sid, start, results)
                start = 0
                if group is not None:
                    next_cursor = _encode_cursor(sid, group)
                    break

            return _search_response(query, results, searched_sessions, result_limit, next_cursor)
        except Exception as e:
            return {"success": False, "error": str(e)}


def _search_response(
    query: str,
    results: list[dict[str, Any]],
    searched_sessions: int,
    limit: int,
    next_cursor: str | None,
) -> dict[str, Any]:
    """Build the search tool response."""
    return {
        "success": True,
        "query": query,
        "results": results,
        "returned_count": len(results),
        "searched_sessions": searched_sessions,
        "limit": limit,
        "truncated": next_cursor is not None,
        "next_cursor": next_cursor,
    }


def _encode_cursor(session_id: str, group: int) -> str:
    """Encode a resume point as an opaque cursor."""
    raw = json.dumps({"session_id": session_id, "group": group}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, int]:
    """Decode a cursor from ``_encode_cursor``; raise ValueError when malformed."""
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except ValueError as e:
        raise ValueError("invalid cursor") from e
    session_id = data.get("session_id") if isinstance(data, dict) else None
    group = data.get("group") if isinstance(data, dict) else None
    if (
        not isinstance(session_id, str)
        or not isinstance(group, int)
        or isinstance(group, bool)
        or group < 0
    ):
        raise ValueError("invalid cursor")
    return session_id, group
