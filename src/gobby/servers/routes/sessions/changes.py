"""Session-scoped Changes panel routes.

Expose the viewed session's working-tree changes (resolved against its real
working directory and diff base) so the Changes activity panel is correct for
worktree/clone/resumed sessions and switches contents when the session
switches.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query

from gobby.servers.session_changes import (
    SessionWorkspace,
    compute_session_changes,
    compute_session_file_diff,
    is_safe_relative_path,
    resolve_session_workspace,
)
from gobby.storage.project_checkouts import CheckoutNotFoundError
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def register_changes_routes(router: APIRouter, server: HTTPServer) -> None:
    """Register session-scoped Changes routes on the router."""

    def _resolve(session_id: str) -> SessionWorkspace:
        try:
            workspace = resolve_session_workspace(
                session_manager=server.session_manager,
                task_manager=server.task_manager,
                session_id=session_id,
            )
        except CheckoutNotFoundError as exc:
            raise HTTPException(
                404,
                detail={
                    "error": type(exc).__name__,
                    "message": "No checkout for this session's project on this machine",
                },
            ) from exc
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(
                409, detail={"error": type(exc).__name__, "message": str(exc)}
            ) from exc
        if workspace is None:
            raise HTTPException(404, "Session working directory not found")
        return workspace

    @router.get("/{session_id}/changes")
    async def session_changes(session_id: str) -> dict[str, Any]:
        """Return the changed-file list for the session's working tree."""
        workspace = _resolve(session_id)
        try:
            files = await compute_session_changes(workspace)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as e:
            logger.warning(
                "Failed to compute changes for session %s: %s",
                session_id,
                type(e).__name__,
                exc_info=True,
            )
            raise HTTPException(500, "Failed to compute session changes") from e
        return {
            "files": [{"path": f.path, "status": f.status} for f in files],
            "isolation": workspace.isolation,
        }

    @router.get("/{session_id}/changes/diff")
    async def session_change_diff(
        session_id: str,
        path: str = Query(..., description="Relative file path"),
    ) -> dict[str, str]:
        """Return the unified diff for a single file in the session's working tree."""
        workspace = _resolve(session_id)
        if not is_safe_relative_path(workspace.working_dir, path):
            raise HTTPException(400, "Invalid path")
        try:
            diff = await compute_session_file_diff(workspace, path)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as e:
            logger.warning(
                "Failed to compute session file diff",
                extra={"session_id": session_id, "path": path},
                exc_info=True,
            )
            return {"diff": "", "path": path, "error": str(e)}
        return {"diff": diff, "path": path}
