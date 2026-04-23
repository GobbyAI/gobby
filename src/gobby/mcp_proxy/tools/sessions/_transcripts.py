"""Transcript archive tools for session transcript backup and restore.

MCP tools for:
- Restoring a session transcript to disk (restore_session_transcript)
- Checking transcript archive status (get_transcript_status)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


def register_transcript_tools(
    registry: InternalToolRegistry,
    session_manager: SessionManager,
) -> None:
    """Register transcript archive tools with a registry."""

    @registry.tool(
        name="restore_session_transcript",
        description=(
            "Restore a session transcript from the gzip archive to disk for CLI resume. "
            "Use when the CLI has purged the transcript file but you want to resume."
        ),
    )
    def restore_session_transcript(
        session_id: str,
        target_path: str | None = None,
    ) -> dict[str, Any]:
        """Restore a session transcript from archive to the filesystem.

        Args:
            session_id: Session reference (#N, UUID, or prefix)
            target_path: Optional override path. If None, restores to original transcript_path.

        Returns:
            Dict with status, path, and size.
        """
        from gobby.sessions.transcript_archive import restore_transcript
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        try:
            resolved_id = session_manager.resolve_session_reference(session_id, project_id)
        except Exception:
            return {"error": f"Could not resolve session: {session_id}"}

        session = session_manager.get(resolved_id)
        if not session or not session.external_id:
            return {"error": "Session not found or missing external_id", "session_id": resolved_id}

        restore_path = target_path or session.transcript_path
        if not restore_path:
            return {"error": "No transcript_path for session", "session_id": resolved_id}

        restored = restore_transcript(session.external_id, restore_path)
        if not restored:
            return {
                "error": "No archive found or original file still exists",
                "session_id": resolved_id,
            }

        size = os.path.getsize(restore_path)
        return {
            "status": "restored",
            "session_id": resolved_id,
            "path": restore_path,
            "size": size,
        }

    @registry.tool(
        name="get_transcript_status",
        description="Check if a transcript archive exists for a session and get file stats.",
    )
    def get_transcript_status(session_id: str) -> dict[str, Any]:
        """Get transcript archive status for a session.

        Args:
            session_id: Session reference (#N, UUID, or prefix)

        Returns:
            Dict with exists flag and size stats if present.
        """
        from gobby.sessions.transcript_archive import get_archive_dir
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        try:
            resolved_id = session_manager.resolve_session_reference(session_id, project_id)
        except Exception:
            return {"error": f"Could not resolve session: {session_id}"}

        session = session_manager.get(resolved_id)
        if not session or not session.external_id:
            return {"exists": False, "session_id": resolved_id}

        archive_path = get_archive_dir() / f"{session.external_id}.jsonl.gz"
        if not archive_path.is_file():
            return {"exists": False, "session_id": resolved_id}

        stat = archive_path.stat()
        return {
            "exists": True,
            "session_id": resolved_id,
            "compressed_size": stat.st_size,
            "archive_path": str(archive_path),
        }
