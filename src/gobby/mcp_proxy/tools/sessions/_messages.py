"""Message retrieval and search tools for session management.

This module contains MCP tools for:
- Getting messages for a session (get_session_messages)
- Searching messages using FTS (search_messages, DB-only)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.sessions.transcript_reader import TranscriptReader
    from gobby.storage.sessions import SessionManager


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

            # Truncate content if not full_content
            if not full_content:
                for msg in messages:
                    if "content" in msg and msg["content"] and isinstance(msg["content"], str):
                        if len(msg["content"]) > 500:
                            msg["content"] = msg["content"][:500] + "... (truncated)"

                    if "tool_calls" in msg and msg["tool_calls"]:
                        for tc in msg["tool_calls"]:
                            if (
                                "input" in tc
                                and isinstance(tc["input"], str)
                                and len(tc["input"]) > 200
                            ):
                                tc["input"] = tc["input"][:200] + "... (truncated)"

                    if "tool_result" in msg and msg["tool_result"]:
                        tr = msg["tool_result"]
                        if (
                            "content" in tr
                            and isinstance(tr["content"], str)
                            and len(tr["content"]) > 200
                        ):
                            tr["content"] = tr["content"][:200] + "... (truncated)"

                    # Also truncate content_blocks if present (from renderer)
                    if "content_blocks" in msg and msg["content_blocks"]:
                        for block in msg["content_blocks"]:
                            if block.get("type") in ["text", "thinking"]:
                                if (
                                    isinstance(block.get("content"), str)
                                    and len(block["content"]) > 500
                                ):
                                    block["content"] = block["content"][:500] + "... (truncated)"

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
        name="search_messages",
        description="[DEPRECATED] Search messages using text matching. This tool is deprecated and will be removed in a future release. Accepts #N, N, UUID, or prefix for session_id.",
    )
    async def search_messages(
        query: str,
        session_id: str | None = None,
        limit: int = 20,
        full_content: bool = False,
    ) -> dict[str, Any]:
        """
        Search messages.

        Args:
            query: Search query
            session_id: Optional session filter - supports #N, N (seq_num), UUID, or prefix
            limit: Max results
            full_content: If True, returns full content. If False (default), truncates large content.
        """
        return {
            "success": False,
            "error": "search_messages is no longer available (session_messages table removed)",
        }
