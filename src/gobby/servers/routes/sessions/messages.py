"""Message and transcript routes for sessions.

Handles message listing, transcript status/download, and transcript restoration.
"""

import logging
import os
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query

from gobby.sessions.transcript_archive import get_archive_dir, restore_transcript

if TYPE_CHECKING:
    from collections.abc import Callable

    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def _get_session_record(session_manager: Any, session_id: str) -> Any | None:
    """Fetch a session from either storage or service-layer managers."""
    if hasattr(session_manager, "get"):
        return session_manager.get(session_id)
    if hasattr(session_manager, "get_session"):
        return session_manager.get_session(session_id)
    return None


def _session_attr(session: Any, name: str) -> Any:
    """Read an attribute from an object or dict-shaped session."""
    if isinstance(session, dict):
        return session.get(name)
    return getattr(session, name, None)


def register_message_routes(
    router: APIRouter,
    server: "HTTPServer",
    get_session_manager: "Callable[[], Any]",
) -> None:
    """Register message and transcript routes on the router."""

    @router.get("/{session_id}/messages")
    async def sessions_get_messages(
        session_id: str,
        limit: int = 100,
        offset: int = 0,
        role: str | None = None,
        format: str = Query("rendered", pattern="^(rendered|legacy)$"),
    ) -> dict[str, Any]:
        """
        Get messages for a session.

        Args:
            session_id: Session ID
            limit: Max messages to return (default 100)
            offset: Pagination offset
            role: Filter by role (user, assistant, tool)
            format: Response format - 'rendered' (default) or 'legacy' (flat rows)

        Returns:
            List of messages and total count key
        """
        start_time = time.perf_counter()

        try:
            if format == "legacy":
                if server.transcript_reader is None:
                    raise HTTPException(status_code=503, detail="Transcript reader not available")

                messages = await server.transcript_reader.get_messages(
                    session_id=session_id, limit=limit, offset=offset, role=role
                )
                count = await server.transcript_reader.count_messages(session_id)
            else:
                if server.transcript_reader is None:
                    raise HTTPException(status_code=503, detail="Transcript reader not available")

                # Note: role filter not yet supported in rendered format (groups turns)
                # If role filter is needed, legacy format must be used.
                rendered = await server.transcript_reader.get_rendered_messages(
                    session_id=session_id,
                    limit=limit,
                    offset=offset,
                )
                messages = [m.to_dict() for m in rendered]
                count = await server.transcript_reader.count_messages(session_id)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "messages": messages,
                "total_count": count,
                "response_time_ms": response_time_ms,
                "format": format,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Get messages error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # --- Transcript Archive Endpoints ---

    @router.get("/{session_id}/transcript/status")
    async def transcript_status(session_id: str) -> dict[str, Any]:
        """Check if a transcript archive exists for this session."""
        try:
            if server.transcript_reader is not None and hasattr(
                server.transcript_reader, "get_transcript_status"
            ):
                status = await server.transcript_reader.get_transcript_status(session_id)
                return dict(status)

            sm = get_session_manager()
            session = _get_session_record(sm, session_id)
            if not session:
                return {
                    "session_id": session_id,
                    "live_exists": False,
                    "archive_exists": False,
                    "availability": "missing",
                    "content_state": "missing",
                    "session_source": None,
                    "detected_source": None,
                    "source_mismatch": False,
                    "raw_record_count": 0,
                    "parsed_message_count": 0,
                }

            transcript_path = _session_attr(session, "transcript_path")
            external_id = _session_attr(session, "external_id")
            live_exists = bool(transcript_path and os.path.isfile(transcript_path))

            archive_dir = get_archive_dir()
            archive_path = archive_dir / f"{external_id}.jsonl.gz" if external_id else None
            archive_exists = bool(archive_path and archive_path.is_file())

            result: dict[str, Any] = {
                "session_id": session_id,
                "live_exists": live_exists,
                "archive_exists": archive_exists,
                "availability": "live"
                if live_exists
                else "archive_only"
                if archive_exists
                else "missing",
                "content_state": "empty" if (live_exists or archive_exists) else "missing",
                "session_source": _session_attr(session, "source"),
                "detected_source": None,
                "source_mismatch": False,
                "raw_record_count": 0,
                "parsed_message_count": 0,
            }
            if archive_exists and archive_path is not None:
                result["compressed_size"] = archive_path.stat().st_size
                result["archive_path"] = str(archive_path)
            return result
        except Exception as e:
            logger.error(f"Error getting transcript status: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/{session_id}/transcript")
    async def get_transcript(session_id: str) -> Any:
        """Download raw transcript content from filesystem."""
        try:
            import gzip

            from fastapi.responses import Response

            sm = get_session_manager()
            session = sm.get_session(session_id)

            # Try original JSONL path first
            if session and session.transcript_path and os.path.isfile(session.transcript_path):
                with open(session.transcript_path, "rb") as f:
                    raw = f.read()
                return Response(
                    content=raw,
                    media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="{session_id}.jsonl"'},
                )

            # Fall back to gzip archive
            if session and session.external_id:
                archive_path = get_archive_dir() / f"{session.external_id}.jsonl.gz"
                if archive_path.is_file():
                    with gzip.open(archive_path, "rb") as f:
                        raw = f.read()
                    return Response(
                        content=raw,
                        media_type="application/x-ndjson",
                        headers={
                            "Content-Disposition": f'attachment; filename="{session_id}.jsonl"'
                        },
                    )

            raise HTTPException(status_code=404, detail="No transcript found")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting transcript: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{session_id}/restore-transcript")
    async def restore_transcript_endpoint(session_id: str) -> dict[str, Any]:
        """Restore a transcript from archive to disk for CLI resume."""
        try:
            sm = get_session_manager()
            session = sm.get_session(session_id)
            if not session or not session.external_id or not session.transcript_path:
                raise HTTPException(
                    status_code=404,
                    detail="Session not found or missing external_id/transcript_path",
                )
            restored = restore_transcript(session.external_id, session.transcript_path)
            if not restored:
                raise HTTPException(
                    status_code=404,
                    detail="No transcript archive found or original still exists",
                )
            size = os.path.getsize(session.transcript_path)
            return {
                "status": "restored",
                "session_id": session_id,
                "path": session.transcript_path,
                "size": size,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error restoring transcript: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
