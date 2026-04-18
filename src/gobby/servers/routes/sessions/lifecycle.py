"""Session lifecycle routes.

Handles lookup, status updates, expiry, and renaming.
"""

import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from gobby.sessions.terminal_kill import kill_terminal_session

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from gobby.servers.http import HTTPServer
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


def _get_session_stats(db: "DatabaseProtocol", session: Any) -> dict[str, int]:
    """Get activity stats for a session (tasks closed, memories, commits).

    Args:
        db: Database connection
        session: Session object with id, created_at, updated_at, transcript_path

    Returns:
        Dict with tasks_closed, memories_created, commit_count
    """
    stats: dict[str, int] = {}

    # Tasks closed in this session
    try:
        row = db.fetchone(
            "SELECT COUNT(*) FROM session_tasks WHERE session_id = ? AND action = 'closed'",
            (session.id,),
        )
        stats["tasks_closed"] = row[0] if row else 0
    except Exception:
        stats["tasks_closed"] = 0

    # Memories created by this session
    try:
        row = db.fetchone(
            "SELECT COUNT(*) FROM memories WHERE source_session_id = ?",
            (session.id,),
        )
        stats["memories_created"] = row[0] if row else 0
    except Exception:
        stats["memories_created"] = 0

    # Commits made during session timeframe
    from gobby.servers.routes.sessions.core import _get_commit_count

    stats["commit_count"] = _get_commit_count(db, session)

    # Skills injected in this session
    try:
        row = db.fetchone(
            "SELECT COUNT(DISTINCT skill_name) FROM session_skills WHERE session_id = ?",
            (session.id,),
        )
        stats["skills_used"] = row[0] if row else 0
    except Exception:
        stats["skills_used"] = 0

    return stats


def register_lifecycle_routes(
    router: APIRouter,
    server: "HTTPServer",
    get_session_manager: "Callable[[], Any]",
    broadcast_session: "Callable[..., Awaitable[None]]",
) -> None:
    """Register session lifecycle routes on the router."""

    @router.post("/bulk-move")
    async def bulk_move_sessions(request: Request) -> dict[str, Any]:
        """Move sessions between projects in bulk."""
        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            body = await request.json()
            session_ids = body.get("session_ids", [])
            target_project_id = body.get("target_project_id")

            if not session_ids or not target_project_id:
                raise HTTPException(
                    status_code=400,
                    detail="Required: session_ids (list) and target_project_id",
                )

            moved = 0
            errors = []
            with server.session_manager.db.transaction():
                for sid in session_ids:
                    try:
                        session = server.session_manager.get(sid)
                        if session is None:
                            errors.append(f"Session {sid} not found")
                            continue
                        server.session_manager.db.execute(
                            "UPDATE sessions SET project_id = ?, updated_at = datetime('now') WHERE id = ?",
                            (target_project_id, sid),
                        )
                        moved += 1
                    except Exception as e:
                        errors.append(f"Failed to move {sid}: {e}")

            return {
                "status": "success",
                "moved": moved,
                "errors": errors,
                "total": len(session_ids),
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Bulk move sessions error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/{session_id}")
    async def sessions_get(session_id: str) -> dict[str, Any]:
        """
        Get session by ID from local storage.

        Args:
            session_id: Session ID (UUID)

        Returns:
            Session data
        """
        start_time = time.perf_counter()

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            session = server.session_manager.get(session_id)

            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            session_data = session.to_dict()

            # Enrich with activity stats
            try:
                stats = _get_session_stats(server.session_manager.db, session)
                session_data.update(stats)
            except Exception as e:
                logger.warning(f"Failed to fetch session stats: {e}")

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "session": session_data,
                "response_time_ms": response_time_ms,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Sessions get error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/find_current")
    async def find_current_session(request: Request) -> dict[str, Any]:
        """
        Find current active session by composite key.

        Uses composite key: external_id, machine_id, source, project_id
        Accepts either project_id directly or cwd (which is resolved to project_id).
        """
        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            body = await request.json()
            external_id = body.get("external_id")
            machine_id = body.get("machine_id")
            source = body.get("source")
            project_id = body.get("project_id")
            cwd = body.get("cwd")

            if not external_id or not machine_id or not source:
                raise HTTPException(
                    status_code=400,
                    detail="Required fields: external_id, machine_id, source",
                )

            # Resolve project_id from cwd if not provided
            if not project_id and cwd:
                project_id = server.resolve_project_id(None, cwd)

            if not project_id:
                raise HTTPException(
                    status_code=400,
                    detail="Required: project_id or cwd (to resolve project)",
                )

            session = server.session_manager.find_by_external_id(
                external_id, machine_id, project_id, source
            )

            if session is None:
                return {"session": None}

            return {"session": session.to_dict()}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Find current session error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/find_parent")
    async def find_parent_session(request: Request) -> dict[str, Any]:
        """
        Find parent session for handoff.

        Looks for most recent session in same project with handoff_ready status.
        Accepts either project_id directly or cwd (which is resolved to project_id).
        """
        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            body = await request.json()
            machine_id = body.get("machine_id")
            source = body.get("source")
            project_id = body.get("project_id")
            cwd = body.get("cwd")

            if not source:
                raise HTTPException(status_code=400, detail="Required field: source")

            if not machine_id:
                from gobby.utils.machine_id import get_machine_id

                machine_id = get_machine_id()

            if not machine_id:
                logger.warning(
                    "Failed to determine machine_id for session discovery, using fallback"
                )
                machine_id = "unknown-machine"

            # Resolve project_id from cwd if not provided
            if not project_id:
                if not cwd:
                    raise HTTPException(
                        status_code=400,
                        detail="Required field: project_id or cwd",
                    )
                project_id = server.resolve_project_id(None, cwd)

            session = server.session_manager.find_parent(
                machine_id=machine_id,
                source=source,
                project_id=project_id,
            )

            if session is None:
                return {"session": None}

            return {"session": session.to_dict()}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Find parent session error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/update_status")
    async def update_session_status(request: Request) -> dict[str, Any]:
        """
        Update session status.
        """
        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            body = await request.json()
            session_id = body.get("session_id")
            status = body.get("status")

            if not session_id or not status:
                raise HTTPException(status_code=400, detail="Required fields: session_id, status")

            session = server.session_manager.update_status(session_id, status)

            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            await broadcast_session("session_updated", session_id)

            return {"session": session.to_dict()}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Update session status error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{session_id}/expire")
    async def expire_session(session_id: str) -> dict[str, Any]:
        """Expire a session, killing any associated terminal/tmux pane."""
        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            session = server.session_manager.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            if session.status == "expired":
                return {"status": "already_expired", "session_id": session_id}

            # Kill tmux pane / terminal process if present
            terminal_killed = False
            if session.terminal_context:
                terminal_killed = await kill_terminal_session(session.terminal_context, session_id)

            server.session_manager.update_status(session_id, "expired")
            await broadcast_session("session_expired", session_id)

            return {
                "status": "expired",
                "session_id": session_id,
                "terminal_killed": terminal_killed,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Expire session error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{session_id}/rename")
    async def rename_session(session_id: str, request: Request) -> dict[str, Any]:
        """
        Rename a session by setting a new title.

        Args:
            session_id: Session ID
            request: Request with JSON body {"title": "..."}

        Returns:
            Updated title
        """

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            body = await request.json()
            title = (body.get("title") or "").strip()
            if not title:
                raise HTTPException(status_code=400, detail="Title must not be empty")
            if len(title) > 200:
                raise HTTPException(status_code=400, detail="Title must be 200 characters or fewer")

            session = server.session_manager.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            result = server.session_manager.update_title(
                session_id,
                title,
                title_source="manual",
            )
            if result is None:
                raise HTTPException(status_code=404, detail="Session not found")

            return {"status": "success", "title": title}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Rename session error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
