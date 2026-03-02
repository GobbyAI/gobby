"""Commits and workflow tools for session management.

This module contains MCP tools for:
- Getting session commits (get_session_commits)
- Marking autonomous loop complete (mark_loop_complete)
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.storage.sessions import LocalSessionManager


def register_commits_tools(
    registry: InternalToolRegistry,
    session_manager: LocalSessionManager,
    db: Any | None = None,
) -> None:
    """
    Register commits and workflow tools with a registry.

    Args:
        registry: The InternalToolRegistry to register tools with
        session_manager: LocalSessionManager instance for session operations
        db: Database for dependency injection (optional)
    """

    def _resolve_session_id(ref: str) -> str:
        """Resolve session reference (#N, N, UUID, or prefix) to UUID."""
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        project_id = project_ctx.get("id") if project_ctx else None

        return session_manager.resolve_session_reference(ref, project_id)

    @registry.tool(
        name="get_session_commits",
        description="Get git commits made during a session timeframe. Accepts #N, N, UUID, or prefix for session_id.",
    )
    def get_session_commits(
        session_id: str,
        max_commits: int = 20,
    ) -> dict[str, Any]:
        """
        Get git commits made during a session's active timeframe.

        Uses session.created_at and session.updated_at to filter
        git log within that timeframe.

        Args:
            session_id: Session reference - supports #N, N (seq_num), UUID, or prefix
            max_commits: Maximum commits to return (default 20)

        Returns:
            Session ID, list of commits, and count
        """
        import subprocess  # nosec B404 - subprocess needed for git commands
        from datetime import datetime
        from pathlib import Path

        if session_manager is None:
            return {"success": False, "error": "Session manager not available"}

        # Resolve session reference (#N, N, UUID, or prefix)
        try:
            resolved_id = _resolve_session_id(session_id)
            session = session_manager.get(resolved_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not session:
            return {"success": False, "error": f"Session {session_id} not found"}

        # Get working directory from transcript path or project
        cwd = None
        if session.jsonl_path:
            cwd = str(Path(session.jsonl_path).parent)

        # Format timestamps for git --since/--until
        # Git expects ISO format or relative dates
        # Session timestamps may be ISO strings or datetime objects
        if isinstance(session.created_at, str):
            since_time = datetime.fromisoformat(session.created_at.replace("Z", "+00:00"))
        else:
            since_time = session.created_at

        if session.updated_at:
            if isinstance(session.updated_at, str):
                until_time = datetime.fromisoformat(session.updated_at.replace("Z", "+00:00"))
            else:
                until_time = session.updated_at
        else:
            until_time = datetime.now(UTC)

        # Format as ISO 8601 for git
        since_str = since_time.isoformat()
        until_str = until_time.isoformat()

        try:
            # Get commits within timeframe
            cmd = [
                "git",
                "log",
                f"--since={since_str}",
                f"--until={until_str}",
                f"-{max_commits}",
                "--format=%H|%s|%aI",  # hash|subject|author-date-iso
            ]

            result = subprocess.run(  # nosec B603 - cmd built from hardcoded git arguments
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                cwd=cwd,
            )

            if result.returncode != 0:
                return {
                    "success": False,
                    "session_id": session.id,
                    "error": "Git command failed",
                    "stderr": result.stderr.strip(),
                }

            commits = []
            for line in result.stdout.strip().split("\n"):
                if "|" in line:
                    parts = line.split("|", 2)
                    if len(parts) >= 2:
                        commit = {
                            "hash": parts[0],
                            "message": parts[1],
                        }
                        if len(parts) >= 3:
                            commit["timestamp"] = parts[2]
                        commits.append(commit)

            return {
                "success": True,
                "session_id": session.id,
                "commits": commits,
                "count": len(commits),
                "timeframe": {
                    "since": since_str,
                    "until": until_str,
                },
            }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "session_id": session.id,
                "error": "Git command timed out",
            }
        except FileNotFoundError:
            return {
                "success": False,
                "session_id": session.id,
                "error": "Git executable not found in PATH",
            }
        except Exception as e:
            return {"success": False, "session_id": session.id, "error": str(e)}

    @registry.tool(
        name="mark_loop_complete",
        description="""Mark the autonomous loop as complete, preventing session chaining. Accepts #N, N, UUID, or prefix for session_id.

Args:
    session_id: (REQUIRED) Your session ID. Accepts #N, N, UUID, or prefix. Get it from:
        1. Your injected context (look for 'Session Ref: #N' or 'session_id: xxx')
        2. Or call get_current_session(external_id, source) first""",
    )
    def mark_loop_complete(session_id: str) -> dict[str, Any]:
        """
        Mark the autonomous loop as complete for a session.

        This sets stop_reason='completed' in the workflow state, which
        signals the auto-loop workflow to NOT chain a new session
        when this session ends.

        Use this when:
        - A task is fully complete and no more work is needed
        - You want to exit the autonomous loop gracefully
        - The user has explicitly asked to stop

        Args:
            session_id: Session reference - supports #N, N (seq_num), UUID, or prefix (REQUIRED)

        Returns:
            Success status and session details
        """
        if not session_manager:
            return {"success": False, "error": "Session manager not available"}

        # Resolve session reference (#N, N, UUID, or prefix)
        try:
            resolved_id = _resolve_session_id(session_id)
            session = session_manager.get(resolved_id)
        except ValueError as e:
            return {"success": False, "error": str(e), "session_id": session_id}

        if not session:
            return {
                "success": False,
                "error": f"Session {session_id} not found",
                "session_id": session_id,
            }

        # Set stop_reason via session variables
        from gobby.workflows.state_manager import SessionVariableManager

        if db is None:
            from gobby.storage.database import LocalDatabase

            local_db = LocalDatabase()
            session_var_manager = SessionVariableManager(local_db)
        else:
            session_var_manager = SessionVariableManager(db)

        session_var_manager.set_variable(session.id, "stop_reason", "completed")

        return {
            "success": True,
            "session_id": session.id,
            "stop_reason": "completed",
            "message": "Autonomous loop marked complete - session will not chain",
        }
