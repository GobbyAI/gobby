"""Core session CRUD routes.

Handles registration, listing, lookup, status updates, expiry, and renaming.
"""

import inspect
import json
import logging
import subprocess  # nosec B404 # subprocess needed for git commit counting
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.requests import ClientDisconnect

from gobby.agents.sandbox import (
    web_chat_policy_mismatch_message,
    web_chat_sandbox_config,
    web_chat_sandbox_policy_hash,
)
from gobby.servers.models import SessionRegisterRequest, WebChatSessionRequest
from gobby.servers.routes.sessions.statusline_activity import (
    STATUSLINE_GAP_WARNING_THRESHOLD_MS,
    last_session_activity,
    prune_trackers,
    record_statusline_seen,
)
from gobby.storage.token_events import TokenEventStore, build_session_usage_payload
from gobby.telemetry.instruments import inc_counter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from gobby.servers.http import HTTPServer
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


def _get_commit_count(db: "DatabaseProtocol", session: Any) -> int:
    """Count git commits made during a session's timeframe.

    Args:
        db: Database connection for project lookup
        session: Session object with created_at, updated_at, project_id

    Returns:
        Number of commits, or 0 if git is unavailable
    """
    # Resolve cwd from project repo_path (transcript_path parent is not a git repo)
    cwd = None
    if session.project_id:
        try:
            row = db.fetchone(
                "SELECT repo_path FROM projects WHERE id = ?",
                (session.project_id,),
            )
            if row and row["repo_path"]:
                cwd = row["repo_path"]
        except Exception as e:
            logger.debug(f"Failed to resolve repo_path for session {session.id}: {e}")

    if not cwd:
        return 0

    # Parse timestamps
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

    # Include timezone offset so git doesn't assume local time
    if since_time.tzinfo is not None:
        since_str = since_time.strftime("%Y-%m-%dT%H:%M:%S%z")
    else:
        since_str = since_time.strftime("%Y-%m-%dT%H:%M:%S")
    if until_time.tzinfo is not None:
        until_str = until_time.strftime("%Y-%m-%dT%H:%M:%S%z")
    else:
        until_str = until_time.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        cmd = [
            "git",
            "rev-list",
            "--count",
            f"--since={since_str}",
            f"--until={until_str}",
            "HEAD",
        ]
        result = subprocess.run(  # nosec B603 # cmd built from hardcoded git arguments
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass

    return 0


async def _compute_resumability(
    server: "HTTPServer",
    sessions: list[Any],
    current_session_id: str | None,
) -> dict[str, tuple[bool, str | None]]:
    """Compute resumability for each session.

    Returns a dict mapping session_id -> (is_resumable, blocked_reason).
    """
    result: dict[str, tuple[bool, str | None]] = {}

    # Batch-load active agent runs and pipeline executions
    active_agent_session_ids: set[str] = set()
    active_pipeline_session_ids: set[str] = set()

    if server.session_manager:
        db = server.session_manager.db
        try:
            rows = db.fetchall(
                "SELECT DISTINCT parent_session_id FROM agent_runs "
                "WHERE status IN ('pending', 'running') AND parent_session_id IS NOT NULL"
            )
            active_agent_session_ids = {r["parent_session_id"] for r in rows}
        except Exception as e:
            logger.debug(f"Failed to fetch active agent session ids: {e}")

        try:
            rows = db.fetchall(
                "SELECT DISTINCT session_id FROM pipeline_executions "
                "WHERE status IN ('pending', 'running', 'waiting_approval') AND session_id IS NOT NULL"
            )
            active_pipeline_session_ids = {r["session_id"] for r in rows}
        except Exception as e:
            logger.debug(f"Failed to fetch active pipeline session ids: {e}")

    # Active web chat session IDs
    ws_server = server.services.websocket_server
    active_chat_db_ids: set[str] = set()
    runtime_manager = server.services.web_chat_runtime_manager
    if ws_server:
        chat_sessions = getattr(ws_server, "_chat_sessions", {})
        for cs in chat_sessions.values():
            db_sid = getattr(cs, "db_session_id", None)
            if db_sid:
                active_chat_db_ids.add(db_sid)

    for session in sessions:
        sid = session.id

        # Exclude caller's own session
        if current_session_id and sid == current_session_id:
            result[sid] = (False, "current session")
            continue

        if sid in active_agent_session_ids:
            result[sid] = (False, "has active agent")
            continue

        if sid in active_pipeline_session_ids:
            result[sid] = (False, "has active pipeline")
            continue

        if sid in active_chat_db_ids:
            result[sid] = (False, "active in web chat")
            continue

        if runtime_manager and getattr(session, "session_type", None) == "web_chat":
            mismatch_reason = runtime_manager.policy_mismatch_reason(session)
            if mismatch_reason:
                result[sid] = (False, web_chat_policy_mismatch_message())
                continue

        result[sid] = (True, None)

    return result


def register_core_routes(
    router: APIRouter,
    server: "HTTPServer",
    get_session_manager: "Callable[[], Any]",
    broadcast_session: "Callable[..., Awaitable[None]]",
) -> None:
    """Register core session CRUD routes on the router."""

    @router.post("/web-chat")
    async def create_web_chat_session(body: WebChatSessionRequest) -> dict[str, Any]:
        """Create a durable web-chat session row owned by the server."""
        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            provider = body.provider or "claude"
            if provider not in {"claude", "gemini", "qwen", "codex", "droid"}:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid provider. Must be one of: claude, gemini, qwen, codex, droid",
                )

            project_id = server.resolve_project_id(body.project_id, body.cwd)

            from gobby.utils.machine_id import get_machine_id

            machine_id = get_machine_id() or "unknown-machine"
            model = body.model if isinstance(body.model, str) and body.model else None
            from gobby.llm.local_detection import is_local_agent_definition

            is_local = is_local_agent_definition(provider, model)
            chat_mode = (
                body.chat_mode if isinstance(body.chat_mode, str) and body.chat_mode else None
            )
            runtime_manager = server.services.web_chat_runtime_manager
            if runtime_manager is not None:
                sandbox_enabled = runtime_manager.sandbox_config.enabled
                sandbox_policy_hash = runtime_manager.sandbox_policy_hash
            else:
                sandbox_enabled = web_chat_sandbox_config(server.services.config).enabled
                sandbox_policy_hash = web_chat_sandbox_policy_hash(server.services.config)

            session = server.session_manager.create_web_chat_session(
                machine_id=machine_id,
                project_id=project_id,
                source=provider,
                title=body.title,
                model=model,
                is_local=is_local,
                chat_mode=chat_mode,
                sandbox_enabled=sandbox_enabled,
                sandbox_policy_hash=sandbox_policy_hash,
            )

            inc_counter("session_registrations_total")

            return {
                "status": "created",
                "session": session.to_dict(),
            }

        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error creating web chat session: {e}", exc_info=True)
            raise HTTPException(
                status_code=500, detail="Internal server error while creating web chat session"
            ) from e

    @router.post("/register")
    async def register_session(request_data: SessionRegisterRequest) -> dict[str, Any]:
        """
        Register session metadata in local storage.

        Args:
            request_data: Session registration parameters

        Returns:
            Registration confirmation with session ID
        """
        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            # Get machine_id from request or generate
            machine_id = request_data.machine_id
            if not machine_id:
                from gobby.utils.machine_id import get_machine_id

                machine_id = get_machine_id()

            if not machine_id:
                # Should unlikely happen if get_machine_id works, but type safe
                machine_id = "unknown-machine"

            # Extract git branch if project path exists but git_branch not provided
            git_branch = request_data.git_branch
            if request_data.project_path and not git_branch:
                from gobby.utils.git import get_git_metadata

                git_metadata = get_git_metadata(request_data.project_path)
                if git_metadata.get("git_branch"):
                    git_branch = git_metadata.get("git_branch")

            # Resolve project_id from cwd if not provided
            project_id = server.resolve_project_id(request_data.project_id, request_data.cwd)

            # Register session in local storage
            session = server.session_manager.register(
                external_id=request_data.external_id,
                machine_id=machine_id,
                source=request_data.source or "Claude Code",
                project_id=project_id,
                transcript_path=request_data.transcript_path,
                title=request_data.title,
                git_branch=git_branch,
                parent_session_id=request_data.parent_session_id,
                sandbox_enabled=request_data.sandbox_enabled,
            )

            inc_counter("session_registrations_total")

            return {
                "status": "registered",
                "external_id": request_data.external_id,
                "id": session.id,
                "machine_id": machine_id,
            }

        except HTTPException:
            raise

        except ValueError as e:
            # ValueError from _resolve_project_id when project not initialized

            raise HTTPException(status_code=400, detail=str(e)) from e

        except Exception as e:
            logger.error(f"Error registering session: {e}", exc_info=True)
            raise HTTPException(
                status_code=500, detail="Internal server error while registering session"
            ) from e

    @router.get("/usage")
    async def get_usage_breakdown(
        days: int = Query(1, ge=1, le=365, description="Number of days to look back"),
        project_id: str | None = Query(None, description="Filter by project ID"),
    ) -> dict[str, Any]:
        """Get token usage breakdown by source and model.

        Returns aggregated usage statistics including per-model and
        per-source (CLI adapter) breakdowns.
        """
        from gobby.sessions.token_tracker import SessionTokenTracker

        sm = get_session_manager()
        tracker = SessionTokenTracker(db=sm.db)
        return tracker.get_usage_summary(days=days, project_id=project_id)

    @router.post("/statusline")
    async def statusline_update(request: Request) -> dict[str, Any]:
        """Receive usage data from the Claude Code statusline handler.

        This is the primary path for token usage tracking.
        """
        try:
            body = await request.json()
        except ClientDisconnect:
            return {"status": "ok", "warning": "client_disconnected"}
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON") from None

        external_id = body.get("session_id")
        if not external_id:
            raise HTTPException(status_code=400, detail="Missing session_id") from None

        sm = get_session_manager()
        session = sm.find_active_by_external_id(external_id, source="claude")
        if not session:
            # Session may not be registered yet (first ~1s of updates)
            return {"status": "ok", "warning": "session_not_found"}

        now = datetime.now(UTC)
        prune_trackers(now)
        previous = record_statusline_seen(session.id, now)
        if previous is not None:
            gap_ms = int((now - previous).total_seconds() * 1000)
            if gap_ms >= STATUSLINE_GAP_WARNING_THRESHOLD_MS:
                activity_ts = last_session_activity(session.id)
                if activity_ts is not None and activity_ts > previous:
                    logger.warning(
                        "statusline_usage_gap session_id=%s gap_ms=%s threshold_ms=%s "
                        "last_activity_ms_ago=%s",
                        session.id,
                        gap_ms,
                        STATUSLINE_GAP_WARNING_THRESHOLD_MS,
                        int((now - activity_ts).total_seconds() * 1000),
                    )
                    inc_counter(
                        "statusline_usage_gap_warnings_total",
                        attributes={"source": "claude"},
                    )
                else:
                    logger.debug(
                        "statusline_usage_gap_quiet session_id=%s gap_ms=%s threshold_ms=%s",
                        session.id,
                        gap_ms,
                        STATUSLINE_GAP_WARNING_THRESHOLD_MS,
                    )

        sm.update_usage(
            session_id=session.id,
            input_tokens=body.get("input_tokens", 0),
            output_tokens=body.get("output_tokens", 0),
            cache_creation_tokens=body.get("cache_creation_tokens", 0),
            cache_read_tokens=body.get("cache_read_tokens", 0),
            context_window=body.get("context_window_size"),
            model=body.get("model_id"),
        )
        inc_counter("statusline_posts_succeeded_total", attributes={"source": "claude"})

        ws_server = server.services.websocket_server
        if ws_server is not None:
            broadcast_result = ws_server.broadcast_session_usage_updated(
                build_session_usage_payload(
                    session_id=session.id,
                    project_id=session.project_id,
                    model=body.get("model_id") if isinstance(body.get("model_id"), str) else None,
                    context_window=(
                        body.get("context_window_size")
                        if isinstance(body.get("context_window_size"), int)
                        else None
                    ),
                    totals={
                        "input_tokens": int(body.get("input_tokens", 0) or 0),
                        "output_tokens": int(body.get("output_tokens", 0) or 0),
                        "cache_creation_tokens": int(body.get("cache_creation_tokens", 0) or 0),
                        "cache_read_tokens": int(body.get("cache_read_tokens", 0) or 0),
                    },
                    updated_at=now,
                )
            )
            if inspect.isawaitable(broadcast_result):
                await broadcast_result

        return {"status": "ok"}

    @router.get("/{session_id}/token-events")
    async def get_session_token_events(
        session_id: str,
        limit: int = Query(500, ge=1, le=2000),
        since: str | None = Query(None),
    ) -> dict[str, Any]:
        """Return recent token events for a session."""
        sm = get_session_manager()
        session = sm.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        store = TokenEventStore(sm.db)
        events = store.list_session_events(session_id, limit=limit, since=since)
        return {
            "session_id": session_id,
            "events": events,
            "count": len(events),
        }

    @router.get("")
    async def list_sessions(
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        exclude_subagents: bool = Query(
            False, description="Exclude subagent sessions (agent_depth > 0)"
        ),
        include_resumability: bool = Query(
            False,
            description="Add is_resumable/resume_blocked_reason fields and filter non-resumable",
        ),
        current_session_id: str | None = Query(
            None, description="Caller's own session ID (excluded from resumable list)"
        ),
        cursor_updated_at: str | None = Query(
            None,
            description="Compound-cursor timestamp from a prior page's next_cursor",
        ),
        cursor_id: str | None = Query(
            None,
            description="Compound-cursor session id from a prior page's next_cursor",
        ),
    ) -> dict[str, Any]:
        """
        List sessions with optional filtering and message counts.

        Args:
            project_id: Filter by project ID
            status: Filter by status (active, archived, etc)
            source: Filter by source (Claude Code, Gemini, etc)
            limit: Max results (default 100)
            exclude_subagents: If true, only return top-level sessions
            include_resumability: If true, enrich with resumability and filter non-resumable
            current_session_id: Caller's session to exclude from resumable results
            cursor_updated_at: Pass next_cursor.updated_at from a prior page to fetch the next.
            cursor_id: Pass next_cursor.id from a prior page; both cursor params must be set
                together. Cursor pagination is disabled when include_resumability=true (the
                over-fetch semantics make cursor positioning unreliable); next_cursor is
                always null in that mode.

        Returns:
            Dict with `sessions`, `count`, `next_cursor` (or null), and `response_time_ms`.
        """
        start_time = time.perf_counter()

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            # Over-fetch when resumability filtering is requested, since
            # non-resumable sessions will be removed post-query
            fetch_limit = limit * 3 if include_resumability else limit
            sessions = server.session_manager.list(
                project_id=project_id,
                status=status,
                source=source,
                limit=fetch_limit,
                exclude_subagents=exclude_subagents,
                cursor_updated_at=cursor_updated_at,
                cursor_id=cursor_id,
            )

            # Build resumability info if requested
            resumability: dict[str, tuple[bool, str | None]] = {}
            if include_resumability:
                resumability = await _compute_resumability(server, sessions, current_session_id)

            # Enrich sessions with counts
            session_list = []
            for session in sessions:
                # If resumability requested, skip non-resumable sessions
                if include_resumability:
                    is_resumable, blocked_reason = resumability.get(session.id, (False, None))
                    if not is_resumable:
                        continue

                session_data = session.to_dict()
                if include_resumability:
                    session_data["is_resumable"] = is_resumable
                    session_data["resume_blocked_reason"] = blocked_reason
                session_list.append(session_data)
                if include_resumability and len(session_list) >= limit:
                    break

            response_time_ms = (time.perf_counter() - start_time) * 1000

            next_cursor: dict[str, str] | None = None
            if not include_resumability and len(session_list) >= limit and session_list:
                last = session_list[-1]
                next_cursor = {
                    "updated_at": str(last["updated_at"]),
                    "id": str(last["id"]),
                }

            return {
                "sessions": session_list,
                "count": len(session_list),
                "next_cursor": next_cursor,
                "response_time_ms": response_time_ms,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error listing sessions: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    # Remaining routes (bulk-move, get, find_current, find_parent,
    # update_status, expire, rename) are in lifecycle.py
