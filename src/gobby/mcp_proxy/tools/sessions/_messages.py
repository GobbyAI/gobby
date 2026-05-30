"""Message retrieval and search tools for session management.

This module contains MCP tools for:
- Getting messages for a session (get_session_messages)
- Searching rendered transcript messages (search_session_messages)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.sessions.transcript_reader import TranscriptReader
    from gobby.storage.sessions import SessionManager

MAX_SEARCH_SESSIONS: int = 100


def register_message_tools(
    registry: InternalToolRegistry,
    message_manager: object | None = None,  # Deprecated, ignored
    session_manager: SessionManager | None = None,
    transcript_reader: TranscriptReader | None = None,
) -> None:
    """
    Register message retrieval and search tools with a registry.

    Args:
        registry: The InternalToolRegistry to register tools with
        message_manager: Deprecated, ignored
        session_manager: SessionManager for resolving session references
        transcript_reader: Optional TranscriptReader for JSONL + gzip fallback reads
    """

    from gobby.sessions.transcript_search import search_rendered_messages
    from gobby.utils.session_context import resolve_session_ref

    def _resolve_session_id(session_id: str) -> str:
        return resolve_session_ref(session_manager, session_id)

    @registry.tool(
        name="get_session_messages",
        description="Get messages for a session. Returns rendered messages with content blocks. Accepts #N, N, UUID, or prefix for session_id.",
    )
    # Entry point for get_session_messages tool
    async def get_session_messages(
        session_id: str,
        limit: int = 50,
        offset: int = 0,
        full_content: bool = False,
    ) -> dict[str, Any]:
        """
        Get messages for a session.

        Args:
            session_id: Session reference - supports #N, N (seq_num), UUID, or prefix
            limit: Max messages to return
            offset: Offset for pagination
            full_content: If True, returns full content. If False (default), truncates large content.
        """
        try:
            resolved_id = _resolve_session_id(session_id)

            # Use TranscriptReader (JSONL + gzip fallback)
            if transcript_reader:
                rendered_messages = await transcript_reader.get_rendered_messages(
                    session_id=resolved_id,
                    limit=limit,
                    offset=offset,
                )
                messages = [m.to_dict() for m in rendered_messages]
                session_total = await transcript_reader.count_messages(resolved_id)
            else:
                return {
                    "success": False,
                    "error": "Message retrieval not available (TranscriptReader not configured)",
                }

            if not full_content:
                for msg in messages:
                    _truncate_session_message(msg)

            return {
                "success": True,
                "messages": messages,
                "total_count": session_total,
                "returned_count": len(messages),
                "limit": limit,
                "offset": offset,
                "truncated": not full_content,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="search_session_messages",
        description="Search rendered transcript messages by substring. Accepts #N, N, UUID, or prefix for session_id.",
    )
    async def search_session_messages(
        query: str,
        session_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 20,
        full_content: bool = False,
    ) -> dict[str, Any]:
        """
        Search rendered transcript messages.

        Args:
            query: Search query
            session_id: Optional session filter - supports #N, N (seq_num), UUID, or prefix
            project_id: Optional project filter for multi-session search
            status: Optional session status filter for multi-session search
            source: Optional CLI source filter for multi-session search
            limit: Max results
            full_content: If True, returns full content. If False (default), truncates large content.
        """
        if transcript_reader is None:
            return {
                "success": False,
                "error": "Message search not available (TranscriptReader not configured)",
            }

        query = query.strip()
        if not query:
            return {"success": False, "error": "query must not be empty"}

        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return {"success": False, "error": "limit must be positive"}
        result_limit = limit

        try:
            if session_id:
                resolved_id = _resolve_session_id(session_id)
                rendered_messages = await transcript_reader.get_rendered_messages(
                    session_id=resolved_id,
                    limit=None,
                    offset=0,
                )
                session_results = search_rendered_messages(
                    session_id=resolved_id,
                    messages=rendered_messages,
                    query=query,
                    limit=result_limit,
                    full_content=full_content,
                )
                return _search_response(query, session_results, 1, result_limit, full_content)

            if session_manager is None:
                return {
                    "success": False,
                    "error": "Multi-session search requires SessionManager",
                }

            sessions = session_manager.list(
                project_id=project_id,
                status=status,
                source=source,
                limit=MAX_SEARCH_SESSIONS,
            )

            results: list[dict[str, Any]] = []
            searched_sessions = 0
            for session in sessions:
                if len(results) >= result_limit:
                    break
                searched_sessions += 1
                current_limit = result_limit - len(results)
                rendered_messages = await transcript_reader.get_rendered_messages(
                    session_id=session.id,
                    limit=None,
                    offset=0,
                )
                results.extend(
                    search_rendered_messages(
                        session_id=session.id,
                        messages=rendered_messages,
                        query=query,
                        limit=current_limit,
                        full_content=full_content,
                    )
                )

            return _search_response(query, results, searched_sessions, result_limit, full_content)
        except Exception as e:
            return {"success": False, "error": str(e)}


def _search_response(
    query: str,
    results: list[dict[str, Any]],
    searched_sessions: int,
    limit: int,
    full_content: bool,
) -> dict[str, Any]:
    """Build the search tool response."""
    return {
        "success": True,
        "query": query,
        "results": results,
        "returned_count": len(results),
        "searched_sessions": searched_sessions,
        "limit": limit,
        "truncated": not full_content,
    }


def _truncate_session_message(msg: dict[str, Any]) -> None:
    """Truncate verbose message fields for MCP responses."""
    content = msg.get("content")
    if isinstance(content, str) and len(content) > 500:
        msg["content"] = content[:500] + "... (truncated)"

    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_input = tool_call.get("input")
            if isinstance(tool_input, str) and len(tool_input) > 200:
                tool_call["input"] = tool_input[:200] + "... (truncated)"

    tool_result = msg.get("tool_result")
    if isinstance(tool_result, dict):
        result_content = tool_result.get("content")
        if isinstance(result_content, str) and len(result_content) > 200:
            tool_result["content"] = result_content[:200] + "... (truncated)"

    content_blocks = msg.get("content_blocks")
    if isinstance(content_blocks, list):
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in ["text", "thinking"]:
                continue
            block_content = block.get("content")
            if isinstance(block_content, str) and len(block_content) > 500:
                block["content"] = block_content[:500] + "... (truncated)"
