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

from gobby.utils.metrics import get_metrics_collector

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


def create_hooks_router(server: "HTTPServer") -> APIRouter:
    """
    Create hooks router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with hooks endpoints
    """
    router = APIRouter(prefix="/api/hooks", tags=["hooks"])
    metrics = get_metrics_collector()

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
        metrics.inc_counter("http_requests_total")
        metrics.inc_counter("hooks_total")
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

            # Get HookManager from app.state
            if not hasattr(request.app.state, "hook_manager"):
                raise HTTPException(status_code=503, detail="HookManager not initialized")

            hook_manager = request.app.state.hook_manager

            # Select adapter based on source
            from gobby.adapters.base import BaseAdapter
            from gobby.adapters.claude_code import ClaudeCodeAdapter
            from gobby.adapters.codex_impl.adapter import CodexNotifyAdapter
            from gobby.adapters.copilot import CopilotAdapter
            from gobby.adapters.cursor import CursorAdapter
            from gobby.adapters.gemini import GeminiAdapter
            from gobby.adapters.windsurf import WindsurfAdapter
            from gobby.hooks.events import SessionSource

            if source == "claude":
                adapter: BaseAdapter = ClaudeCodeAdapter(hook_manager=hook_manager)
            elif source == "antigravity":
                adapter = ClaudeCodeAdapter(hook_manager=hook_manager)  # Same format as Claude
            elif source == "claude_sdk":
                adapter = ClaudeCodeAdapter(hook_manager=hook_manager)
                adapter.source = SessionSource.CLAUDE_SDK
            elif source == "claude_sdk_web_chat":
                adapter = ClaudeCodeAdapter(hook_manager=hook_manager)
                adapter.source = SessionSource.CLAUDE_SDK_WEB_CHAT
            elif source == "cursor":
                adapter = CursorAdapter(hook_manager=hook_manager)
            elif source == "windsurf":
                adapter = WindsurfAdapter(hook_manager=hook_manager)
            elif source == "copilot":
                adapter = CopilotAdapter(hook_manager=hook_manager)
            elif source == "gemini":
                adapter = GeminiAdapter(hook_manager=hook_manager)
            elif source == "codex":
                # Use bidirectional adapter when app-server is connected
                codex_adapter = getattr(request.app.state, "codex_adapter", None)
                if codex_adapter is not None:
                    adapter = codex_adapter
                else:
                    adapter = CodexNotifyAdapter(hook_manager=hook_manager)
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported source: {source}. Supported: claude, antigravity, claude_sdk, claude_sdk_web_chat, gemini, codex, cursor, windsurf, copilot",
                )

            # Execute hook via adapter
            try:
                result = await asyncio.to_thread(adapter.handle_native, payload, hook_manager)

                response_time_ms = (time.perf_counter() - start_time) * 1000
                metrics.inc_counter("hooks_succeeded_total")

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
                metrics.inc_counter("hooks_failed_total")
                logger.warning(
                    f"Invalid hook request: {hook_type}",
                    extra={"hook_type": hook_type, "error": str(e)},
                )
                return _graceful_error_response(hook_type, str(e))

            except Exception as e:
                # Hook execution error - return graceful response so tool proceeds
                # This prevents confusing "hook failed" warnings in Claude Code
                metrics.inc_counter("hooks_failed_total")
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
            metrics.inc_counter("hooks_failed_total")
            logger.error("Hook endpoint error", exc_info=True)
            if hook_type:
                return _graceful_error_response(hook_type, str(e))
            # Fallback: return basic success to prevent CLI hook failure
            return {"continue": True, "decision": "approve"}

    return router
