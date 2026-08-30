"""Core session CRUD routes.

Handles registration, listing, lookup, status updates, expiry, and renaming.
"""

import asyncio
import logging
import subprocess  # nosec B404 # subprocess needed for git commit counting
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Query

from gobby.agents.sandbox import (
    web_chat_policy_mismatch_message,
    web_chat_sandbox_policy_hash,
)
from gobby.servers.models import (
    SessionRegisterRequest,
    WebChatSessionRequest,
)
from gobby.servers.routes.configuration_context import require_config_snapshot
from gobby.sessions.acp_lifecycle import attach_acp_block
from gobby.storage.machines import MachineNotRegisteredError
from gobby.storage.sessions._update_sentinel import UNSET
from gobby.storage.token_events import TokenEventStore
from gobby.telemetry.instruments import inc_counter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from gobby.servers.http import HTTPServer
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


async def _get_commit_count(db: "HubDatabase", session: Any) -> int:
    """Count git commits made during a session's timeframe.

    Args:
        db: Database connection for project lookup
        session: Session object with created_at, updated_at, project_id

    Returns:
        Number of commits, or 0 if git is unavailable
    """
    # Resolve cwd from the session-machine checkout (transcript_path parent is not a git repo)
    cwd = None
    if session.project_id:
        machine_id = getattr(session, "machine_id", None)
        if not machine_id:
            return 0
        try:
            from gobby.storage.project_checkouts import (
                CheckoutNotFoundError,
                MissingMachineContextError,
                require_root,
            )
            from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError

            cwd = await asyncio.to_thread(require_root, db, session.project_id, machine_id)
        except (
            CheckoutNotFoundError,
            MissingMachineContextError,
            MachineOwnershipMismatchError,
        ) as e:
            logger.debug("Failed to resolve checkout for session %s: %s", session.id, e)
        except Exception as e:
            logger.debug("Failed to resolve checkout for session %s: %s", session.id, e)

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
        result = await asyncio.to_thread(
            subprocess.run,  # nosec B603 # cmd built from hardcoded git arguments
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
            rows = await server.run_db(
                db.fetchall,
                "SELECT DISTINCT parent_session_id FROM agent_runs "
                "WHERE status IN ('pending', 'running') AND parent_session_id IS NOT NULL",
            )
            active_agent_session_ids = {r["parent_session_id"] for r in rows}
        except Exception as e:
            logger.debug("Failed to fetch active agent session ids: %s", e)

        try:
            rows = await server.run_db(
                db.fetchall,
                "SELECT DISTINCT session_id FROM pipeline_executions "
                "WHERE status IN ('pending', 'running', 'waiting_approval') AND session_id IS NOT NULL",
            )
            active_pipeline_session_ids = {r["session_id"] for r in rows}
        except Exception as e:
            logger.debug("Failed to fetch active pipeline session ids: %s", e)

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
            if provider not in {"claude", "grok", "qwen", "codex", "droid", "agy"}:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Invalid provider. Must be one of: claude, grok, qwen, codex, droid, agy"
                    ),
                )

            project_id = await server.run_db(server.resolve_project_id, body.project_id, body.cwd)
            from gobby.utils.machine_id import get_machine_id

            machine_id = get_machine_id()

            model = body.model if isinstance(body.model, str) and body.model else None
            from gobby.llm.local_detection import is_local_agent_definition

            is_local = is_local_agent_definition(provider, model)
            chat_mode = (
                body.chat_mode if isinstance(body.chat_mode, str) and body.chat_mode else None
            )
            runtime_manager = server.services.web_chat_runtime_manager
            if runtime_manager is not None:
                sandbox_policy_hash = runtime_manager.sandbox_policy_hash
            else:
                config = require_config_snapshot(server).active
                sandbox_policy_hash = web_chat_sandbox_policy_hash(config)

            session = await server.run_db(
                server.session_manager.create_web_chat_session,
                machine_id=machine_id,
                project_id=project_id,
                source=provider,
                title=body.title,
                model=model,
                is_local=is_local,
                chat_mode=chat_mode,
                sandbox_enabled=False,
                sandbox_policy_hash=sandbox_policy_hash,
            )

            inc_counter("session_registrations_total")

            return {
                "status": "created",
                "session": session.to_dict(),
            }

        except HTTPException:
            raise
        except MachineNotRegisteredError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("Error creating web chat session: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

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

            # Extract git branch if project path exists but git_branch not provided
            git_branch = request_data.git_branch
            if request_data.project_path and not git_branch:
                from gobby.utils.git import get_git_metadata

                git_metadata = await asyncio.to_thread(get_git_metadata, request_data.project_path)
                if git_metadata.get("git_branch"):
                    git_branch = git_metadata.get("git_branch")

            # Resolve project_id from cwd if not provided
            project_id = await server.run_db(
                server.resolve_project_id, request_data.project_id, request_data.cwd
            )
            from gobby.utils.machine_id import get_machine_id

            machine_id = get_machine_id()

            # Register session in local storage
            session = await server.run_db(
                server.session_manager.register,
                external_id=request_data.external_id,
                machine_id=machine_id,
                source=request_data.source or "Claude Code",
                project_id=project_id,
                transcript_path=request_data.transcript_path,
                title=request_data.title,
                title_source=(
                    "manual"
                    if isinstance(request_data.title, str) and request_data.title.strip()
                    else None
                ),
                git_branch=git_branch,
                parent_session_id=(
                    request_data.parent_session_id
                    if request_data.parent_session_id is not None
                    else UNSET
                ),
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

        except MachineNotRegisteredError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

        except ValueError as e:
            # ValueError from _resolve_project_id when project not initialized

            raise HTTPException(status_code=400, detail=str(e)) from e

        except Exception as e:
            logger.exception("Error registering session: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

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
        return cast(
            dict[str, Any],
            await server.run_db(tracker.get_usage_summary, days=days, project_id=project_id),
        )

    @router.get("/{session_id}/token-events")
    async def get_session_token_events(
        session_id: str,
        limit: int = Query(500, ge=1, le=2000),
        since: str | None = Query(None),
    ) -> dict[str, Any]:
        """Return recent token events for a session."""
        sm = get_session_manager()
        session = await server.run_db(sm.get, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        store = TokenEventStore(sm.db)
        events = await server.run_db(
            store.list_session_events, session_id, limit=limit, since=since
        )
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
        machine_id: str | None = Query(None, description="Filter by client machine id"),
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
        sources: list[str] | None = Query(
            None, description="Repeatable: filter by multiple sources (source IN ...)"
        ),
        status_in: list[str] | None = Query(
            None,
            description=(
                "Repeatable: filter by multiple statuses (status IN ...). "
                "Stacks on top of the exclude-deleted base predicate."
            ),
        ),
        mode: list[str] | None = Query(
            None,
            description=(
                "Repeatable: 'interactive' (agent_depth=0) or 'auto' (agent_depth>=1). "
                "Both/neither = no filter."
            ),
        ),
        model: list[str] | None = Query(
            None, description="Repeatable: filter by model (model IN ...)"
        ),
        session_seq_min: int | None = Query(
            None, description="Lower bound (inclusive) on sessions.seq_num"
        ),
        session_seq_max: int | None = Query(
            None, description="Upper bound (inclusive) on sessions.seq_num"
        ),
        task_ref_min: int | None = Query(
            None, description="Lower bound (inclusive) on linked task seq_num"
        ),
        task_ref_max: int | None = Query(
            None, description="Upper bound (inclusive) on linked task seq_num"
        ),
        task_ref_role: list[str] | None = Query(
            None,
            description=(
                "Repeatable subset of {claimed, created, closed} for task ref filter. "
                "Defaults to claimed when min/max is set without roles."
            ),
        ),
        created_after: str | None = Query(
            None, description="Inclusive lower bound on created_at (ISO timestamp)"
        ),
        created_before: str | None = Query(
            None, description="Exclusive upper bound on created_at (ISO timestamp)"
        ),
    ) -> dict[str, Any]:
        """
        List sessions with optional filtering and message counts.

        Args:
            project_id: Filter by project ID
            status: Filter by status (active, archived, etc)
            source: Filter by source (Claude Code, Qwen, etc)
            machine_id: Filter by client machine id
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
            sessions = await server.run_db(
                server.session_manager.list,
                project_id=project_id,
                status=status,
                source=source,
                machine_id=machine_id,
                limit=fetch_limit,
                exclude_subagents=exclude_subagents,
                cursor_updated_at=cursor_updated_at,
                cursor_id=cursor_id,
                sources=sources,
                statuses=status_in,
                modes=mode,
                models=model,
                session_seq_min=session_seq_min,
                session_seq_max=session_seq_max,
                task_ref_min=task_ref_min,
                task_ref_max=task_ref_max,
                task_ref_roles=task_ref_role,
                created_after=created_after,
                created_before=created_before,
            )

            # Build resumability info if requested
            resumability: dict[str, tuple[bool, str | None]] = {}
            if include_resumability:
                resumability = await _compute_resumability(server, sessions, current_session_id)

            # One bulk join against tasks for the whole page — populates
            # claimed_task_refs / created_task_refs / closed_task_refs on each
            # session before serialization. Empty lists when the session never
            # touched a task.
            task_refs_by_session = await server.run_db(
                server.session_manager.fetch_task_refs_by_session, [s.id for s in sessions]
            )
            for session in sessions:
                refs = task_refs_by_session.get(session.id)
                if refs is None:
                    continue
                session.claimed_task_refs = refs["claimed"]
                session.created_task_refs = refs["created"]
                session.closed_task_refs = refs["closed"]

            # Enrich sessions with counts
            acp_runtime_manager = getattr(server.services, "web_chat_runtime_manager", None)
            session_list = []
            for session in sessions:
                # If resumability requested, skip non-resumable sessions
                if include_resumability:
                    is_resumable, blocked_reason = resumability.get(session.id, (False, None))
                    if not is_resumable:
                        continue

                session_data = session.to_dict()
                # Computed ACP enrichment (not persisted): present only for ACP
                # web-chat rows, so the UI's Boolean(session.acp) detection works.
                attach_acp_block(session_data, session, acp_runtime_manager)
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
            logger.exception("Error listing sessions: %s", e)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    # Remaining routes (bulk-move, get, find_current,
    # update_status, expire, rename) are in lifecycle.py
