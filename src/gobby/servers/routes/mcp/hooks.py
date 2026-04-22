"""
Hooks management routes for Gobby HTTP server.

Provides hook execution endpoint for CLI adapters.
Extracted from base.py as part of Strangler Fig decomposition.
"""

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from gobby.adapters.claude_contract import (
    build_graceful_error_hook_response,
    get_claude_contract,
)
from gobby.servers.tool_approvals import (
    approval_key_for_tool,
    get_global_approval_rules,
    is_tool_auto_allowed,
    load_project_approval_rules,
    normalize_approved_tool_keys,
)
from gobby.storage.config_store import ConfigStore
from gobby.telemetry.instruments import inc_counter

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

HOLD_OPEN_HOOK_TYPE_MAP: dict[str, str] = {
    "PreToolUse": "PreToolUse",
    "pre-tool-use": "PreToolUse",
    "BeforeTool": "PreToolUse",
    "AskUserQuestion": "AskUserQuestion",
}

SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION = 1


def _graceful_error_response(hook_type: str, error_msg: str) -> dict[str, Any]:
    """
    Create a graceful degradation response for hook errors.

    Instead of returning HTTP 500 (which causes Claude Code to show a confusing
    "hook failed" warning), return a successful response that:
    1. Allows the tool to proceed (continue=True)
    2. Explains the error via additionalContext (so agents understand what happened)

    This prevents agents from being confused by non-fatal hook errors.
    """
    from gobby.adapters.claude_code import ClaudeCodeAdapter

    adapter = ClaudeCodeAdapter()
    response = adapter.translate_from_hook_response(
        build_graceful_error_hook_response(error_msg),
        hook_type=hook_type,
    )
    if isinstance(response, dict):
        return response

    fallback: dict[str, Any] = {"continue": True}
    contract = get_claude_contract(hook_type)
    if contract and contract.allows_additional_context:
        fallback["hookSpecificOutput"] = {
            "hookEventName": contract.hook_event_name,
            "additionalContext": (
                f"Gobby hook error (non-fatal): {error_msg}. Tool execution will proceed normally."
            ),
        }
    return fallback


MAX_PENDING_PER_SESSION = 3


def _normalize_hook_request(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize legacy flat hook payloads and schema-versioned envelopes.

    The discriminator is explicit: if ``schema_version`` is present, treat the
    request as an envelope. If it is absent, treat the request as the legacy
    flat shape. Do not heuristically infer envelope mode from other fields.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    # Explicit discriminator: schema_version present => envelope. Without it,
    # keep the request on the legacy flat path even if extra envelope-like
    # fields are present.
    if "schema_version" in payload:
        schema_version = payload.get("schema_version")
        if schema_version != SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Unsupported schema_version: "
                    f"{schema_version}. Supported: {SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION}"
                ),
            )
        metadata = {
            "request_shape": "envelope",
            "schema_version": schema_version,
            "critical": bool(payload.get("critical", False)),
            "enqueued_at": payload.get("enqueued_at"),
        }
    else:
        metadata = {
            "request_shape": "flat",
            "schema_version": None,
            "critical": None,
            "enqueued_at": None,
        }

    normalized_payload = {
        "hook_type": payload.get("hook_type"),
        "input_data": payload.get("input_data") or {},
        "source": payload.get("source"),
    }
    return normalized_payload, metadata


def _hook_log_extra(
    hook_type: str | None,
    metadata: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    """Build structured log extras for hook ingress."""
    combined = {
        "hook_type": hook_type,
        "request_shape": metadata.get("request_shape"),
        "schema_version": metadata.get("schema_version"),
        "critical": metadata.get("critical"),
        "enqueued_at": metadata.get("enqueued_at"),
    }
    combined.update(extra)
    return combined


def _normalize_hold_open_hook_type(hook_type: str | None) -> str | None:
    """Normalize provider-specific hook names for web-chat hold-open gating."""
    if not hook_type:
        return None
    return HOLD_OPEN_HOOK_TYPE_MAP.get(hook_type)


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
    from gobby.storage.sessions import SessionManager

    db = request.app.state.server.services.database
    if not db:
        return None
    session_store = SessionManager(db)
    db_session = await asyncio.to_thread(session_store.get, session_id)
    if not db_session:
        try:
            resolved_session_id = await asyncio.to_thread(
                session_store.resolve_session_reference, session_id
            )
        except Exception:
            resolved_session_id = None
        if resolved_session_id:
            db_session = await asyncio.to_thread(session_store.get, resolved_session_id)
    if not db_session:
        db_session = await asyncio.to_thread(
            session_store.find_active_by_external_id, session_id, source
        )

    if not db_session:
        return None

    if getattr(db_session, "session_type", "terminal") != "web_chat":
        return None

    project_path: str | None = None
    if getattr(db_session, "project_id", None):
        try:
            from gobby.storage.projects import LocalProjectManager

            project = LocalProjectManager(db).get(db_session.project_id)
            if project and project.repo_path:
                project_path = project.repo_path
        except Exception:
            logger.debug("Failed to resolve project_path for approval check", exc_info=True)

    # Guard: PendingInteractionManager may not be wired yet
    manager = getattr(request.app.state, "pending_interaction_manager", None)
    if manager is None:
        return None

    async def _broadcast_pending_tool(
        interaction_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        ws_server = (
            request.app.state.server.services.websocket_server
            or request.app.state.server.websocket_server
        )
        if not ws_server:
            return

        message = json.dumps(
            {
                "type": "tool_status",
                "conversation_id": db_session.id,
                "message_id": f"pending-interaction-{interaction_id}",
                "tool_call_id": interaction_id,
                "status": "pending_approval",
                "tool_name": tool_name,
                "arguments": arguments,
            }
        )
        for ws, meta in list(ws_server.clients.items()):
            cid = meta.get("conversation_id") if meta else None
            if cid is not None and cid != db_session.id:
                continue
            try:
                await ws.send(message)
            except Exception:
                logger.debug("Failed to broadcast pending tool interaction", exc_info=True)

    if hook_type == "PreToolUse":
        input_data = payload.get("input_data", {}) or {}
        tool_name = input_data.get("tool_name", "")
        arguments = input_data.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}

        approved_tools_json = getattr(db_session, "approved_tools_json", None)
        try:
            raw_session_rules = json.loads(approved_tools_json) if approved_tools_json else []
        except (TypeError, json.JSONDecodeError):
            raw_session_rules = []
        session_rules = normalize_approved_tool_keys(raw_session_rules)
        project_rules = load_project_approval_rules(project_path)
        global_rules = get_global_approval_rules(ConfigStore(db))
        if tool_name and is_tool_auto_allowed(
            tool_name,
            arguments,
            session_rules=session_rules,
            project_rules=project_rules,
            global_rules=global_rules,
        ):
            return {"decision": "approve"}

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
                "arguments": arguments,
            },
            tool_name=tool_name,
        )
        await _broadcast_pending_tool(interaction_id, tool_name, arguments)
        result_data = await manager.wait(interaction_id)
        decision = result_data.get("decision", "deny")
        if decision == "approve_always" and tool_name:
            key = approval_key_for_tool(tool_name, arguments)
            updated_rules = set(session_rules)
            updated_rules.add(key)
            await asyncio.to_thread(
                session_store.update_approved_tools, db_session.id, updated_rules
            )
            return {"decision": "approve"}
        if decision == "approve":
            return {"decision": "approve"}
        return {"decision": "deny"}

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
        request_metadata: dict[str, Any] = {
            "request_shape": "unknown",
            "schema_version": None,
            "critical": None,
            "enqueued_at": None,
        }

        try:
            # Parse request
            payload, request_metadata = _normalize_hook_request(await request.json())
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
                # Gobby-managed hook commands. app.state.codex_adapter is the
                # WebSocket-oriented CodexAdapter whose translate_to_hook_event
                # expects JSON-RPC format ("method"/"params"), not the
                # hooks.json format ("hook_type"/"input_data") that these
                # hook commands send. Using the wrong adapter silently drops
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
                normalized_hold_open_type = _normalize_hold_open_hook_type(hook_type)
                if session_header and normalized_hold_open_type:
                    hold_open_result = await _maybe_hold_open(
                        request, session_header, normalized_hold_open_type, payload, source
                    )
                    if hold_open_result is not None:
                        return hold_open_result

                response_time_ms = (time.perf_counter() - start_time) * 1000
                inc_counter("hooks_succeeded_total")

                logger.debug(
                    f"Hook executed: {hook_type}",
                    extra=_hook_log_extra(
                        hook_type,
                        request_metadata,
                        continue_=result.get("continue"),
                        response_time_ms=response_time_ms,
                    ),
                )

                return result

            except ValueError as e:
                # Invalid request - still return graceful response
                inc_counter("hooks_failed_total")
                logger.warning(
                    f"Invalid hook request: {hook_type}",
                    extra=_hook_log_extra(hook_type, request_metadata, error=str(e)),
                )
                return _graceful_error_response(hook_type, str(e))

            except Exception as e:
                # Hook execution error - return graceful response so tool proceeds
                # This prevents confusing "hook failed" warnings in Claude Code
                inc_counter("hooks_failed_total")
                logger.error(
                    f"Hook execution failed: {hook_type}",
                    exc_info=True,
                    extra=_hook_log_extra(hook_type, request_metadata),
                )
                return _graceful_error_response(hook_type, str(e))

        except HTTPException:
            # Re-raise 400 errors (bad request) - these are client errors
            raise
        except Exception as e:
            # Outer exception - return graceful response to prevent CLI warning
            inc_counter("hooks_failed_total")
            logger.error(
                "Hook endpoint error",
                exc_info=True,
                extra=_hook_log_extra(hook_type, request_metadata),
            )
            if hook_type:
                return _graceful_error_response(hook_type, str(e))
            # Fallback: return basic success to prevent CLI hook failure
            return {"continue": True, "decision": "approve"}

    return router
