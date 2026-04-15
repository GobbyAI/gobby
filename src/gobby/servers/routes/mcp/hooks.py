"""
Hooks management routes for Gobby HTTP server.

Provides hook execution endpoint for CLI adapters.
Extracted from base.py as part of Strangler Fig decomposition.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from gobby.telemetry.instruments import inc_counter

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Map hook types to hookEventName for additionalContext
# Only these hook types support hookSpecificOutput in Claude Code
HOOK_EVENT_NAME_MAP: dict[str, str] = {
    "pre-tool-use": "PreToolUse",
    "post-tool-use": "PostToolUse",
    "post-tool-use-failure": "PostToolUse",
    "user-prompt-submit": "UserPromptSubmit",
}


def _graceful_error_response(hook_type: str, error_msg: str) -> dict[str, Any]:
    """
    Create a graceful degradation response for hook errors.

    Instead of returning HTTP 500 (which causes Claude Code to show a confusing
    "hook failed" warning), return a successful response that:
    1. Allows the tool to proceed (continue=True)
    2. Explains the error via additionalContext (so agents understand what happened)

    This prevents agents from being confused by non-fatal hook errors.
    """
    response: dict[str, Any] = {
        "continue": True,
        "decision": "approve",
    }

    # Add helpful context for supported hook types
    hook_event_name = HOOK_EVENT_NAME_MAP.get(hook_type)
    if hook_event_name:
        response["hookSpecificOutput"] = {
            "hookEventName": hook_event_name,
            "additionalContext": (
                f"Gobby hook error (non-fatal): {error_msg}. Tool execution will proceed normally."
            ),
        }

    return response


MAX_PENDING_PER_SESSION = 3


async def _maybe_hold_open(
    request: Request,
    session_id: str,
    hook_type: str,
    payload: dict[str, Any],
    source: str,
) -> dict[str, Any] | None:
    """Hold HTTP response open for web chat sessions needing user approval.

    Returns a response dict if the request was held open and resolved, or
    ``None`` if the session is not a web chat session (so the caller should
    fall through to the normal adapter response path).
    """
    from gobby.storage.sessions import LocalSessionManager

    db = request.app.state.server.services.database
    if not db:
        return None
    session_store = LocalSessionManager(db)
    db_session = await asyncio.to_thread(session_store.get, session_id)

    if not db_session:
        return None

    if getattr(db_session, "session_type", "terminal") != "web_chat":
        return None

    # Guard: PendingInteractionManager may not be wired yet
    manager = getattr(request.app.state, "pending_interaction_manager", None)
    if manager is None:
        return None

    if hook_type == "PreToolUse":
        tool_name = payload.get("input_data", {}).get("tool_name", "")

        # Rate-limit pending interactions per session
        pending_count = await manager.count_pending(db_session.id)
        if pending_count >= MAX_PENDING_PER_SESSION:
            return {"decision": "deny", "reason": "too_many_pending"}

        interaction_id = await manager.create(
            session_id=db_session.id,
            kind="tool",
            provider=source,
            payload={
                "tool_name": tool_name,
                "arguments": payload.get("input_data", {}).get("arguments", {}),
            },
            tool_name=tool_name,
        )
        result_data = await manager.wait(interaction_id)
        return {"decision": result_data.get("decision", "deny")}

    if hook_type == "AskUserQuestion":
        question = payload.get("input_data", {}).get("question", "")

        interaction_id = await manager.create(
            session_id=db_session.id,
            kind="ask_user",
            provider=source,
            payload={"question": question},
        )
        result_data = await manager.wait(interaction_id)
        response = result_data.get("response", {})
        return {"additionalContext": response.get("answers", {})}

    return None


def create_hooks_router(server: "HTTPServer") -> APIRouter:
    """
    Create hooks router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with hooks endpoints
    """
    router = APIRouter(prefix="/api/hooks", tags=["hooks"])

    @router.post("/execute")
    async def execute_hook(request: Request) -> dict[str, Any]:
        """
        Execute CLI hook via adapter pattern.

        Request body:
            {
                "hook_type": "session-start",
                "input_data": {...},
                "source": "claude"
            }

        Returns:
            Hook execution result with status
        """
        start_time = time.perf_counter()
        inc_counter("hooks_total")
        hook_type: str | None = None  # Track for error handling

        try:
            # Parse request
            payload = await request.json()
            hook_type = payload.get("hook_type")
            source = payload.get("source")

            if not hook_type:
                raise HTTPException(status_code=400, detail="hook_type required")

            if not source:
                raise HTTPException(status_code=400, detail="source required")

            # Project context is set by ProjectContextMiddleware from
            # X-Gobby-Project-Id / X-Gobby-Session-Id headers.

            # Get HookManager from app.state
            if not hasattr(request.app.state, "hook_manager"):
                raise HTTPException(status_code=503, detail="HookManager not initialized")

            hook_manager = request.app.state.hook_manager

            # Select adapter based on source
            from gobby.adapters.base import BaseAdapter
            from gobby.adapters.claude_code import ClaudeCodeAdapter
            from gobby.adapters.codex_impl.adapter import CodexHooksAdapter
            from gobby.adapters.gemini import GeminiAdapter
            from gobby.adapters.qwen import QwenAdapter

            if source == "claude":
                adapter: BaseAdapter = ClaudeCodeAdapter(hook_manager=hook_manager)
            elif source == "gemini":
                adapter = GeminiAdapter(hook_manager=hook_manager)
            elif source == "qwen":
                adapter = QwenAdapter(hook_manager=hook_manager)
            elif source == "codex":
                # Always use CodexHooksAdapter for HTTP hook requests from
                # hook_dispatcher.py.  app.state.codex_adapter is the
                # WebSocket-oriented CodexAdapter whose translate_to_hook_event
                # expects JSON-RPC format ("method"/"params"), not the
                # hooks.json format ("hook_type"/"input_data") that the
                # dispatcher sends.  Using the wrong adapter silently drops
                # every hook — no terminal_context, no rule enforcement, no
                # stop gates.
                adapter = CodexHooksAdapter(hook_manager=hook_manager)
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported source: {source}. Supported: claude, gemini, qwen, codex",
                )

            # Execute hook via adapter
            try:
                result = await asyncio.to_thread(adapter.handle_native, payload, hook_manager)

                # After existing hook processing, check for web chat hold-open.
                # Terminal sessions pass straight through; only web_chat sessions
                # create pending interactions that hold the HTTP response open
                # until the user approves/denies in the browser.
                session_header = request.headers.get("X-Gobby-Session-Id", "")
                if session_header and hook_type in ("PreToolUse", "AskUserQuestion"):
                    hold_open_result = await _maybe_hold_open(
                        request, session_header, hook_type, payload, source
                    )
                    if hold_open_result is not None:
                        return hold_open_result

                response_time_ms = (time.perf_counter() - start_time) * 1000
                inc_counter("hooks_succeeded_total")

                logger.debug(
                    f"Hook executed: {hook_type}",
                    extra={
                        "hook_type": hook_type,
                        "continue": result.get("continue"),
                        "response_time_ms": response_time_ms,
                    },
                )

                return result

            except ValueError as e:
                # Invalid request - still return graceful response
                inc_counter("hooks_failed_total")
                logger.warning(
                    f"Invalid hook request: {hook_type}",
                    extra={"hook_type": hook_type, "error": str(e)},
                )
                return _graceful_error_response(hook_type, str(e))

            except Exception as e:
                # Hook execution error - return graceful response so tool proceeds
                # This prevents confusing "hook failed" warnings in Claude Code
                inc_counter("hooks_failed_total")
                logger.error(
                    f"Hook execution failed: {hook_type}",
                    exc_info=True,
                    extra={"hook_type": hook_type},
                )
                return _graceful_error_response(hook_type, str(e))

        except HTTPException:
            # Re-raise 400 errors (bad request) - these are client errors
            raise
        except Exception as e:
            # Outer exception - return graceful response to prevent CLI warning
            inc_counter("hooks_failed_total")
            logger.error("Hook endpoint error", exc_info=True)
            if hook_type:
                return _graceful_error_response(hook_type, str(e))
            # Fallback: return basic success to prevent CLI hook failure
            return {"continue": True, "decision": "approve"}

    return router
