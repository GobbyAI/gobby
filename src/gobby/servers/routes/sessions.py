"""
Session routes for Gobby HTTP server.

Provides session registration, listing, lookup, and update endpoints.
"""

import asyncio
import logging
import re
import subprocess  # nosec B404 - subprocess needed for git commit counting
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request

from gobby.servers.models import SessionRegisterRequest
from gobby.utils.metrics import get_metrics_collector

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


def _get_session_stats(db: "DatabaseProtocol", session: Any) -> dict[str, int]:
    """Get activity stats for a session (tasks closed, memories, commits).

    Args:
        db: Database connection
        session: Session object with id, created_at, updated_at, jsonl_path

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


def _get_commit_count(db: "DatabaseProtocol", session: Any) -> int:
    """Count git commits made during a session's timeframe.

    Args:
        db: Database connection for project lookup
        session: Session object with created_at, updated_at, project_id

    Returns:
        Number of commits, or 0 if git is unavailable
    """
    # Resolve cwd from project repo_path (jsonl_path parent is not a git repo)
    cwd = None
    if session.project_id:
        try:
            row = db.fetchone(
                "SELECT repo_path FROM projects WHERE id = ?",
                (session.project_id,),
            )
            if row and row[0]:
                cwd = row[0]
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
        result = subprocess.run(  # nosec B603 - cmd built from hardcoded git arguments
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


def _sanitize_title(raw: str) -> str:
    """Strip markdown, emoji, normalize whitespace from LLM title."""
    title = raw.strip().strip('"').strip("'").split("\n")[0]
    title = re.sub(r"[#*_~`\[\]()]", "", title)
    title = re.sub(
        "[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff"
        "\U0001f1e0-\U0001f1ff\U00002702-\U000027b0\U0000fe00-\U0000fe0f"
        "\U0000200d\U000024c2-\U0001f251\U0001f900-\U0001f9ff"
        "\U0001fa00-\U0001fa6f\U0001fa70-\U0001faff]+",
        "",
        title,
    )
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > 100:
        title = title[:97] + "..."
    return title or "Untitled Session"


def create_sessions_router(server: "HTTPServer") -> APIRouter:
    """
    Create sessions router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with session endpoints
    """
    router = APIRouter(prefix="/api/sessions", tags=["sessions"])
    metrics = get_metrics_collector()

    async def _broadcast_session(event: str, session_id: str, **kwargs: Any) -> None:
        """Broadcast a session event via WebSocket if available."""
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_session_event(event, session_id, **kwargs)
            except Exception as e:
                logger.warning(
                    f"Failed to broadcast session event '{event}' for session {session_id}: {e}"
                )

    @router.post("/register")
    async def register_session(request_data: SessionRegisterRequest) -> dict[str, Any]:
        """
        Register session metadata in local storage.

        Args:
            request_data: Session registration parameters

        Returns:
            Registration confirmation with session ID
        """
        metrics.inc_counter("http_requests_total")
        metrics.inc_counter("session_registrations_total")

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
                jsonl_path=request_data.jsonl_path,
                title=request_data.title,
                git_branch=git_branch,
                parent_session_id=request_data.parent_session_id,
            )

            await _broadcast_session("session_created", session.id)

            return {
                "status": "registered",
                "external_id": request_data.external_id,
                "id": session.id,
                "machine_id": machine_id,
            }

        except HTTPException:
            metrics.inc_counter("http_requests_errors_total")
            raise

        except ValueError as e:
            # ValueError from _resolve_project_id when project not initialized
            metrics.inc_counter("http_requests_errors_total")
            raise HTTPException(status_code=400, detail=str(e)) from e

        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error registering session: {e}", exc_info=True)
            raise HTTPException(
                status_code=500, detail="Internal server error while registering session"
            ) from e

    @router.get("")
    async def list_sessions(
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        """
        List sessions with optional filtering and message counts.

        Args:
            project_id: Filter by project ID
            status: Filter by status (active, archived, etc)
            source: Filter by source (Claude Code, Gemini, etc)
            limit: Max results (default 100)

        Returns:
            List of session objects with message counts
        """
        metrics.inc_counter("http_requests_total")
        start_time = time.perf_counter()

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            sessions = server.session_manager.list(
                project_id=project_id,
                status=status,
                source=source,
                limit=limit,
            )

            # Fetch message counts if message manager is available
            message_counts = {}
            if server.message_manager:
                try:
                    message_counts = await server.message_manager.get_all_counts()
                except Exception as e:
                    logger.warning(f"Failed to fetch message counts: {e}")

            # Enrich sessions with counts
            session_list = []
            for session in sessions:
                session_data = session.to_dict()
                session_data["message_count"] = message_counts.get(session.id, 0)
                session_list.append(session_data)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "sessions": session_list,
                "count": len(session_list),
                "response_time_ms": response_time_ms,
            }

        except HTTPException:
            metrics.inc_counter("http_requests_errors_total")
            raise
        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error listing sessions: {e}", exc_info=True)
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
        metrics.inc_counter("http_requests_total")

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            session = server.session_manager.get(session_id)

            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            session_data = session.to_dict()

            # Enrich with message count (same as list endpoint)
            if server.message_manager:
                try:
                    counts = await server.message_manager.get_all_counts()
                    session_data["message_count"] = counts.get(session.id, 0)
                except Exception as e:
                    logger.warning(f"Failed to fetch message count: {e}")
                    session_data["message_count"] = 0

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

    @router.get("/{session_id}/messages")
    async def sessions_get_messages(
        session_id: str,
        limit: int = 100,
        offset: int = 0,
        role: str | None = None,
    ) -> dict[str, Any]:
        """
        Get messages for a session.

        Args:
            session_id: Session ID
            limit: Max messages to return (default 100)
            offset: Pagination offset
            role: Filter by role (user, assistant, tool)

        Returns:
            List of messages and total count key
        """
        start_time = time.perf_counter()
        metrics.inc_counter("http_requests_total")

        try:
            if server.message_manager is None:
                raise HTTPException(status_code=503, detail="Message manager not available")

            messages = await server.message_manager.get_messages(
                session_id=session_id, limit=limit, offset=offset, role=role
            )

            count = await server.message_manager.count_messages(session_id)
            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "messages": messages,
                "total_count": count,
                "response_time_ms": response_time_ms,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Get messages error: {e}", exc_info=True)
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

            await _broadcast_session("session_updated", session_id)

            return {"session": session.to_dict()}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Update session status error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/update_summary")
    async def update_session_summary(request: Request) -> dict[str, Any]:
        """
        Update session summary path.
        """
        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")

            body = await request.json()
            session_id = body.get("session_id")
            summary_path = body.get("summary_path")

            if not session_id or not summary_path:
                raise HTTPException(
                    status_code=400, detail="Required fields: session_id, summary_path"
                )

            session = server.session_manager.update_summary(session_id, summary_path)

            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            await _broadcast_session("session_updated", session_id)

            return {"session": session.to_dict()}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Update session summary error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{session_id}/synthesize-title")
    async def synthesize_session_title(session_id: str) -> dict[str, Any]:
        """
        Synthesize a title for a session from its recent messages.

        Uses LLM to generate a short 3-5 word title based on conversation content.

        Args:
            session_id: Session ID

        Returns:
            Synthesized title
        """
        start_time = time.perf_counter()
        metrics.inc_counter("http_requests_total")

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")
            if server.llm_service is None:
                raise HTTPException(status_code=503, detail="LLM service not available")
            if server.message_manager is None:
                raise HTTPException(status_code=503, detail="Message manager not available")

            session = server.session_manager.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            # Read recent messages from DB
            messages = await server.message_manager.get_messages(
                session_id=session_id, limit=20, offset=0
            )
            if not messages:
                raise HTTPException(status_code=422, detail="No messages to synthesize title from")

            # Build a concise transcript for the LLM
            transcript_lines = []
            for msg in messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if content and role in ("user", "assistant"):
                    # Truncate long messages
                    if len(content) > 300:
                        content = content[:300] + "..."
                    transcript_lines.append(f"{role}: {content}")

            if not transcript_lines:
                raise HTTPException(status_code=422, detail="No user/assistant messages found")

            transcript = "\n".join(transcript_lines)
            llm_prompt = (
                "Create a short title (3-5 words) for this chat session based on "
                "the conversation. Output ONLY the title, no quotes or explanation.\n\n"
                f"Conversation:\n{transcript}"
            )

            # Load system prompt from prompts system
            system_prompt: str | None = None
            try:
                from gobby.prompts.loader import PromptLoader

                loader = PromptLoader(db=getattr(server, "db", None))
                system_prompt = loader.load("sessions/synthesize_title").content
            except Exception:
                system_prompt = (
                    "You generate short titles for chat sessions. "
                    "Output ONLY 3-5 words. No quotes, no explanation, no punctuation."
                )

            title_config = server.config.session_title if server.config else None
            if title_config:
                try:
                    provider, model, _ = server.llm_service.get_provider_for_feature(title_config)
                except (ValueError, Exception):
                    provider = server.llm_service.get_default_provider()
                    model = "haiku"
            else:
                provider = server.llm_service.get_default_provider()
                model = "haiku"
            title = await asyncio.wait_for(
                provider.generate_text(
                    llm_prompt,
                    system_prompt=system_prompt,
                    model=model,
                    max_tokens=30,
                ),
                timeout=10,
            )
            title = _sanitize_title(title)

            result = server.session_manager.update_title(session_id, title)
            if result is None:
                raise HTTPException(status_code=404, detail="Session not found")

            await _broadcast_session("session_updated", session_id)

            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "status": "success",
                "title": title,
                "response_time_ms": response_time_ms,
            }

        except HTTPException:
            metrics.inc_counter("http_requests_errors_total")
            raise
        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Synthesize title error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to synthesize title") from e

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
        metrics.inc_counter("http_requests_total")

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

            result = server.session_manager.update_title(session_id, title)
            if result is None:
                raise HTTPException(status_code=404, detail="Session not found")

            await _broadcast_session("session_updated", session_id)

            return {"status": "success", "title": title}

        except HTTPException:
            metrics.inc_counter("http_requests_errors_total")
            raise
        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Rename session error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{session_id}/generate-summary")
    async def generate_session_summary(session_id: str) -> dict[str, Any]:
        """
        Generate an AI summary for a session on demand.

        Uses the LLM service to analyze the session transcript and produce
        a markdown summary. Stores the result on the session record.

        Args:
            session_id: Session ID

        Returns:
            Generated summary markdown and metadata
        """
        start_time = time.perf_counter()
        metrics.inc_counter("http_requests_total")

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")
            if server.llm_service is None:
                raise HTTPException(status_code=503, detail="LLM service not available")

            session = server.session_manager.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            from gobby.sessions.transcripts import get_parser
            from gobby.workflows.summary_actions import generate_summary

            transcript_processor = get_parser(session.source or "claude")

            result = await generate_summary(
                session_manager=server.session_manager,
                session_id=session_id,
                llm_service=server.llm_service,
                transcript_processor=transcript_processor,
            )

            if result and result.get("error"):
                raise HTTPException(status_code=422, detail=result["error"])

            # Refetch session to get updated summary_markdown
            updated_session = server.session_manager.get(session_id)

            await _broadcast_session("session_updated", session_id)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "summary_markdown": updated_session.summary_markdown if updated_session else None,
                "result": result,
                "response_time_ms": response_time_ms,
            }

        except HTTPException:
            raise
        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Generate summary error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/{session_id}/stop")
    async def stop_session(session_id: str, request: Request) -> dict[str, Any]:
        """
        Signal a session to stop gracefully.

        Allows external systems to request a graceful stop of an autonomous session.
        The session will check for this signal and stop at the next opportunity.

        Args:
            session_id: Session ID to stop
            request: Request body with optional reason and source

        Returns:
            Stop signal confirmation
        """
        metrics.inc_counter("http_requests_total")

        try:
            # Get HookManager from app state
            if not hasattr(request.app.state, "hook_manager"):
                raise HTTPException(status_code=503, detail="Hook manager not available")

            hook_manager = request.app.state.hook_manager
            if not hasattr(hook_manager, "_stop_registry") or not hook_manager._stop_registry:
                raise HTTPException(status_code=503, detail="Stop registry not available")

            stop_registry = hook_manager._stop_registry

            # Parse optional body parameters
            body: dict[str, Any] = {}
            try:
                body = await request.json()
            except Exception as e:
                logger.debug("Empty body in stop_session request (expected): %s", e)

            reason = body.get("reason", "External stop request")
            source = body.get("source", "http_api")

            # Signal the stop
            signal = stop_registry.signal_stop(
                session_id=session_id,
                reason=reason,
                source=source,
            )

            logger.info(f"Stop signal sent to session {session_id}: {reason}")

            await _broadcast_session("session_stop_signaled", session_id)

            return {
                "status": "stop_signaled",
                "session_id": session_id,
                "signal_id": signal.signal_id,
                "reason": signal.reason,
                "source": signal.source,
                "signaled_at": signal.signaled_at.isoformat(),
            }

        except HTTPException:
            metrics.inc_counter("http_requests_errors_total")
            raise
        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error sending stop signal: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/{session_id}/stop")
    async def get_stop_signal(session_id: str, request: Request) -> dict[str, Any]:
        """
        Check if a session has a pending stop signal.

        Args:
            session_id: Session ID to check

        Returns:
            Stop signal status and details if present
        """
        metrics.inc_counter("http_requests_total")

        try:
            # Get HookManager from app state
            if not hasattr(request.app.state, "hook_manager"):
                raise HTTPException(status_code=503, detail="Hook manager not available")

            hook_manager = request.app.state.hook_manager
            if not hasattr(hook_manager, "_stop_registry") or not hook_manager._stop_registry:
                raise HTTPException(status_code=503, detail="Stop registry not available")

            stop_registry = hook_manager._stop_registry

            signal = stop_registry.get_signal(session_id)

            if signal is None:
                return {
                    "has_signal": False,
                    "session_id": session_id,
                }

            return {
                "has_signal": True,
                "session_id": session_id,
                "signal_id": signal.signal_id,
                "reason": signal.reason,
                "source": signal.source,
                "signaled_at": signal.signaled_at.isoformat(),
                "acknowledged": signal.acknowledged,
                "acknowledged_at": (
                    signal.acknowledged_at.isoformat() if signal.acknowledged_at else None
                ),
            }

        except HTTPException:
            metrics.inc_counter("http_requests_errors_total")
            raise
        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error checking stop signal: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/{session_id}/stop")
    async def clear_stop_signal(session_id: str, request: Request) -> dict[str, Any]:
        """
        Clear a stop signal for a session.

        Useful for resetting a session's stop state after handling.

        Args:
            session_id: Session ID to clear signal for

        Returns:
            Confirmation of signal cleared
        """
        metrics.inc_counter("http_requests_total")

        try:
            # Get HookManager from app state
            if not hasattr(request.app.state, "hook_manager"):
                raise HTTPException(status_code=503, detail="Hook manager not available")

            hook_manager = request.app.state.hook_manager
            if not hasattr(hook_manager, "_stop_registry") or not hook_manager._stop_registry:
                raise HTTPException(status_code=503, detail="Stop registry not available")

            stop_registry = hook_manager._stop_registry

            cleared = stop_registry.clear(session_id)

            if cleared:
                await _broadcast_session("session_stop_cleared", session_id)

            return {
                "status": "cleared" if cleared else "no_signal",
                "session_id": session_id,
                "was_present": cleared,
            }

        except HTTPException:
            metrics.inc_counter("http_requests_errors_total")
            raise
        except Exception as e:
            metrics.inc_counter("http_requests_errors_total")
            logger.error(f"Error clearing stop signal: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router
