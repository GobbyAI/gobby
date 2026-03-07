"""Transcript blob tools for session transcript storage and restore.

MCP tools for:
- Restoring a session transcript to disk (restore_session_transcript)
- Checking transcript blob status (get_transcript_status)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.session_transcripts import LocalSessionTranscriptManager
    from gobby.storage.sessions import LocalSessionManager

logger = logging.getLogger(__name__)


def register_transcript_tools(
    registry: InternalToolRegistry,
    transcript_manager: LocalSessionTranscriptManager,
    session_manager: LocalSessionManager,
) -> None:
    """Register transcript blob tools with a registry."""

    @registry.tool(
        name="restore_session_transcript",
        description=(
            "Restore a session transcript to disk for CLI resume. "
            "Decompresses the stored blob and writes it to the original path. "
            "Use when the CLI has purged the transcript file but you want to resume."
        ),
    )
    def restore_session_transcript(
        session_id: str,
        target_path: str | None = None,
    ) -> dict[str, Any]:
        """Restore a session transcript blob to the filesystem.

        Args:
            session_id: Session reference (#N, UUID, or prefix)
            target_path: Optional override path. If None, restores to original jsonl_path.

        Returns:
            Dict with status, path, and size.
        """
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        try:
            resolved_id = session_manager.resolve_session_reference(session_id, project_id)
        except Exception:
            return {"error": f"Could not resolve session: {session_id}"}

        path = transcript_manager.restore_to_disk(resolved_id, target_path)
        if path is None:
            return {
                "error": "No transcript blob stored for this session",
                "session_id": resolved_id,
            }

        size = os.path.getsize(path)
        return {
            "status": "restored",
            "session_id": resolved_id,
            "path": path,
            "size": size,
        }

    @registry.tool(
        name="get_transcript_status",
        description=(
            "Check if a transcript blob is stored for a session and get compression stats."
        ),
    )
    def get_transcript_status(session_id: str) -> dict[str, Any]:
        """Get transcript blob status for a session.

        Args:
            session_id: Session reference (#N, UUID, or prefix)

        Returns:
            Dict with exists flag and size stats if present.
        """
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        try:
            resolved_id = session_manager.resolve_session_reference(session_id, project_id)
        except Exception:
            return {"error": f"Could not resolve session: {session_id}"}

        stats = transcript_manager.get_stats(resolved_id)
        if stats is None:
            return {"exists": False, "session_id": resolved_id}

        stats["session_id"] = resolved_id
        return stats
