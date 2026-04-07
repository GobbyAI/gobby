"""Analytics and summary routes for sessions.

Handles summary updates, title synthesis, summary generation, and stop signals.
"""

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


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


def register_analytics_routes(
    router: APIRouter,
    server: "HTTPServer",
    get_session_manager: "Callable[[], Any]",
    broadcast_session: "Callable[..., Awaitable[None]]",
) -> None:
    """Register analytics/summary routes on the router."""

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

            await broadcast_session("session_updated", session_id)

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

        try:
            if server.session_manager is None:
                raise HTTPException(status_code=503, detail="Session manager not available")
            if server.llm_service is None:
                raise HTTPException(status_code=503, detail="LLM service not available")
            if server.transcript_reader is None:
                raise HTTPException(status_code=503, detail="Transcript reader not available")

            session = server.session_manager.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")

            # Read recent messages from transcript (JSONL / archive)
            messages = await server.transcript_reader.get_messages(
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
                except Exception:
                    provider = server.llm_service.get_default_provider()
                    model = None
            else:
                provider = server.llm_service.get_default_provider()
                model = None
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

            await broadcast_session("session_updated", session_id)

            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "status": "success",
                "title": title,
                "response_time_ms": response_time_ms,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Synthesize title error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to synthesize title") from e

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

            transcript_processor = get_parser(session.source or "claude", session_id=session_id)

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

            await broadcast_session("session_updated", session_id)

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
                logger.debug(f"Empty body in stop_session request (expected): {e}")

            reason = body.get("reason", "External stop request")
            source = body.get("source", "http_api")

            # Signal the stop
            signal = stop_registry.signal_stop(
                session_id=session_id,
                reason=reason,
                source=source,
            )

            logger.info(f"Stop signal sent to session {session_id}: {reason}")

            await broadcast_session("session_stop_signaled", session_id)

            return {
                "status": "stop_signaled",
                "session_id": session_id,
                "reason": signal.reason,
                "source": signal.source,
                "signaled_at": signal.requested_at.isoformat(),
            }

        except HTTPException:
            raise
        except Exception as e:
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
                "reason": signal.reason,
                "source": signal.source,
                "signaled_at": signal.requested_at.isoformat(),
                "acknowledged": signal.acknowledged,
                "acknowledged_at": (
                    signal.acknowledged_at.isoformat() if signal.acknowledged_at else None
                ),
            }

        except HTTPException:
            raise
        except Exception as e:
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
                await broadcast_session("session_stop_cleared", session_id)

            return {
                "status": "cleared" if cleared else "no_signal",
                "session_id": session_id,
                "was_present": cleared,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error clearing stop signal: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
