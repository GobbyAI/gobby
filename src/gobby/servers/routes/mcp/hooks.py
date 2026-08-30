"""
Hooks management routes for Gobby HTTP server.

Provides hook execution endpoint for CLI adapters.
Extracted from base.py as part of Strangler Fig decomposition.
"""

import logging
import time
from typing import TYPE_CHECKING, Any, Final, cast

from fastapi import APIRouter, HTTPException, Request
from starlette.requests import ClientDisconnect

from gobby.adapters.capabilities import ContextChannel, get_provider_capabilities
from gobby.adapters.claude_contract import get_claude_contract
from gobby.adapters.degradation import AdapterDegradationKind, record_adapter_degradation
from gobby.config.hooks import HookTimeoutConfig
from gobby.hooks.adapter_execution import HOOK_ADAPTER_MAX_WORKERS as _HOOK_ADAPTER_MAX_WORKERS
from gobby.hooks.adapter_execution import (
    AdapterHookTimeout,
    schedule_adapter_timeout_finalization,
    start_envelope_lease_renewal,
)
from gobby.hooks.adapter_execution import (
    run_adapter_hook as _run_adapter_hook,
)
from gobby.hooks.agent_run_ingress import AgentRunIngressRetryableError
from gobby.hooks.envelope_dedupe import (
    ENVELOPE_ID_HEADER,
    claim_envelope_processing,
    clear_stale_envelope_processing_marker,
    envelope_processing_owner_token,
    envelope_terminal_response,
    finalize_envelope_processed,
    mark_envelope_processed,
    read_envelope_marker,
    release_envelope_processing_claim,
)
from gobby.hooks.health_gate import DaemonNotReadyError
from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    envelope_has_hook_response_capability,
)
from gobby.hooks.startup_claim_preflight import (
    StartupClaimLease,
    invalidate_agy_startup_claim,
    preflight_agy_startup_claim,
    rollback_agy_startup_claim,
    strip_private_startup_claim_fields,
)
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.mcp import hook_hold_open
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

FAIL_SAFE_HOOK_TYPES = frozenset(hook_type.casefold() for hook_type in {"Stop", "stop"})
HOOK_ADAPTER_MAX_WORKERS = _HOOK_ADAPTER_MAX_WORKERS
SUPPORTED_HOOK_SOURCES: Final = ("claude", "grok", "qwen", "codex", "droid", "agy")


def _graceful_error_response(
    hook_type: str,
    error_msg: str,
    *,
    source: str | None = "claude",
) -> dict[str, Any]:
    """
    Create a graceful degradation response for hook errors.

    Instead of returning HTTP 500 (which causes Claude Code to show a confusing
    "hook failed" warning), return a successful response that:
    1. Allows the tool to proceed (continue=True)
    2. Explains the error via additionalContext (so agents understand what happened)

    This prevents agents from being confused by non-fatal hook errors.
    """
    provider = source or "claude"
    message = f"Gobby hook error (non-fatal): {error_msg}. Tool execution will proceed normally."
    record_adapter_degradation(
        provider=provider,
        hook_type=hook_type,
        kind=AdapterDegradationKind.GRACEFUL_ERROR,
        response_field="context",
        destination_channel="provider_capability",
    )

    try:
        capabilities = get_provider_capabilities(provider)
        context_channel = capabilities.context_channel_for(hook_type)
    except ValueError:
        provider = "claude"
        context_channel = get_provider_capabilities(provider).context_channel_for(hook_type)

    from gobby.hooks.events import HookResponse

    if context_channel is not ContextChannel.NONE:
        hook_response = HookResponse(decision="allow", context=message)
    else:
        hook_response = HookResponse(decision="allow", system_message=message)

    if provider == "droid":
        from gobby.adapters.droid import DroidAdapter

        result = DroidAdapter().translate_from_hook_response(hook_response, hook_type=hook_type)
        if isinstance(result, dict):
            return result

    if provider == "agy":
        from gobby.adapters.agy import AgyAdapter

        agy_response = AgyAdapter().translate_from_hook_response(
            hook_response,
            hook_type=hook_type,
        )
        if isinstance(agy_response, dict):
            return agy_response

    if provider == "codex":
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        codex_response = CodexHooksAdapter().translate_from_hook_response(
            hook_response,
            hook_type=hook_type,
        )
        if isinstance(codex_response, dict):
            return codex_response

    if provider == "grok":
        from gobby.adapters.grok import GrokAdapter

        grok_response = GrokAdapter().translate_from_hook_response(
            hook_response,
            hook_type=hook_type,
        )
        if isinstance(grok_response, dict):
            return grok_response

    if provider == "qwen":
        from gobby.adapters.qwen import QwenAdapter

        qwen_response = QwenAdapter().translate_from_hook_response(
            hook_response,
            hook_type=hook_type,
        )
        if isinstance(qwen_response, dict):
            return qwen_response

    from gobby.adapters.claude_code import ClaudeCodeAdapter

    claude_response = ClaudeCodeAdapter().translate_from_hook_response(
        hook_response,
        hook_type=hook_type,
    )
    if isinstance(claude_response, dict):
        return claude_response

    fallback: dict[str, Any] = {"continue": True}
    claude_contract = get_claude_contract(hook_type)
    if claude_contract and claude_contract.allows_additional_context:
        fallback["hookSpecificOutput"] = {
            "hookEventName": claude_contract.hook_event_name,
            "additionalContext": message,
        }
    return fallback


def _normalize_hook_request(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize the current schema-versioned hook envelope."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

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
        "response_capability": payload.get("response_capability"),
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


def _is_fail_safe_hook(hook_type: str | None, metadata: dict[str, Any]) -> bool:
    """Return whether hook failures must block for safety."""
    normalized_hook_type = hook_type.casefold() if hook_type is not None else None
    return normalized_hook_type in FAIL_SAFE_HOOK_TYPES or metadata.get("critical") is True


def _hook_block_response(
    adapter: Any | None,
    hook_type: str,
    source: str | None,
    reason: str,
) -> dict[str, Any]:
    """Translate a fail-safe block, falling back to the shared route shape."""
    from gobby.hooks.events import HookResponse

    response = HookResponse(decision="block", reason=reason)
    if adapter is None:
        return {"continue": False, "decision": "block", "reason": reason}

    try:
        translated = adapter.translate_from_hook_response(response, hook_type=hook_type)
    except TypeError:
        translated = adapter.translate_from_hook_response(response)
    except Exception:
        logger.warning(
            "Failed to translate hook block response for %s/%s",
            source,
            hook_type,
            exc_info=True,
        )
        translated = {"continue": False, "decision": "block", "reason": reason}

    return cast(dict[str, Any], translated)


def _hook_timeout_response(
    adapter: Any,
    hook_type: str,
    source: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Build a provider-native timeout response without waiting on hook internals."""
    reason = (
        f"Gobby hook evaluation timed out after {timeout_seconds:g}s; "
        "blocking this critical hook for safety. Try again after the daemon recovers."
    )
    return _hook_block_response(adapter, hook_type, source, reason)


def _hook_exception_response(
    adapter: Any | None,
    hook_type: str,
    source: str | None,
    metadata: dict[str, Any],
    error: str,
) -> dict[str, Any]:
    """Fail closed for safety-critical hooks and degrade all other hook errors."""
    if not _is_fail_safe_hook(hook_type, metadata):
        return _graceful_error_response(hook_type, error, source=source)

    reason = (
        f"Gobby hook evaluation failed: {error}; blocking this critical hook for safety. "
        "Try again after the daemon recovers."
    )
    return _hook_block_response(adapter, hook_type, source, reason)


def _normalize_hold_open_hook_type(hook_type: str | None) -> str | None:
    """Normalize provider-specific hook names for web-chat hold-open gating."""
    if not hook_type:
        return None
    return HOLD_OPEN_HOOK_TYPE_MAP.get(hook_type)


def _result_encodes_denial(result: dict[str, Any]) -> bool:
    """Return whether an adapter result already denies the hook operation."""
    if result.get("continue") is False:
        return True

    decision = result.get("decision")
    if isinstance(decision, str) and decision.casefold() in {"block", "deny"}:
        return True

    permission_decision = result.get("permissionDecision")
    if isinstance(permission_decision, str) and permission_decision.casefold() == "deny":
        return True

    hook_output = result.get("hookSpecificOutput")
    if isinstance(hook_output, dict):
        permission_decision = hook_output.get("permissionDecision")
        if isinstance(permission_decision, str) and permission_decision.casefold() == "deny":
            return True

    return False


def _is_codex_root_context_miss(
    source: str | None,
    payload: dict[str, Any],
    error: ValueError,
) -> bool:
    if source != "codex" or "No .gobby/project.json found in /" not in str(error):
        return False
    input_data = payload.get("input_data")
    if not isinstance(input_data, dict):
        return False
    if payload.get("_platform_session_id") or input_data.get("project_id"):
        return False
    terminal_context = input_data.get("terminal_context")
    if isinstance(terminal_context, dict) and terminal_context.get("gobby_session_id"):
        return False

    from gobby.hooks.project_context import is_unusable_hook_cwd

    cwd = input_data.get("cwd")
    return isinstance(cwd, str) and is_unusable_hook_cwd(cwd)


def _receipt_session_id(
    *,
    claim_lease: StartupClaimLease | None,
    payload: dict[str, Any],
    platform_session_id: str,
    envelope_id: str,
) -> str:
    if claim_lease is not None and claim_lease.session_id:
        return claim_lease.session_id
    if platform_session_id:
        return platform_session_id
    input_data = payload.get("input_data")
    if isinstance(input_data, dict):
        for key in ("session_id", "conversationId", "conversation_id"):
            value = input_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return envelope_id


def _attach_delivery_receipt(
    response: dict[str, Any],
    *,
    db: Any,
    envelope_id: str,
    session_id: str,
    staged_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if db is None:
        return response
    try:
        from gobby.storage.hook_receipts import prepare_receipt

        receipt = prepare_receipt(
            db,
            session_id=session_id,
            envelope_id=envelope_id,
            staged_payload=staged_payload,
        )
    except Exception:
        logger.warning(
            "Failed to prepare hook delivery receipt for envelope %s",
            envelope_id,
            exc_info=True,
        )
        return response
    attached = dict(response)
    attached["_gobby_delivery_receipt"] = {
        "receipt_id": receipt.receipt_id,
        "original_envelope_id": receipt.original_envelope_id,
        "delivery_generation": receipt.delivery_generation,
    }
    return attached


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
    async def execute_hook(request: Request) -> Any:
        """
        Execute CLI hook via adapter pattern.

        Request body:
        {
            "schema_version": 1,
            "enqueued_at": "2026-04-16T12:00:00Z",
            "critical": false,
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
        source: str | None = None  # Track for error handling
        adapter: Any | None = None
        claim_lease: StartupClaimLease | None = None
        owner_token: str | None = None
        request_metadata: dict[str, Any] = {
            "request_shape": "unknown",
            "schema_version": None,
            "critical": None,
            "enqueued_at": None,
        }
        envelope_id = request.headers.get(ENVELOPE_ID_HEADER, "").strip()
        payload: dict[str, Any] = {}
        platform_session_id = ""

        def mark_processed_and_return(response: dict[str, Any]) -> dict[str, Any]:
            staged_payload = response.get(STAGED_EFFECTS_FIELD)
            response = strip_private_startup_claim_fields(response)
            if envelope_id:
                response = _attach_delivery_receipt(
                    response,
                    db=getattr(getattr(server, "services", None), "database", None),
                    envelope_id=envelope_id,
                    session_id=_receipt_session_id(
                        claim_lease=claim_lease,
                        payload=payload,
                        platform_session_id=platform_session_id,
                        envelope_id=envelope_id,
                    ),
                    staged_payload=(staged_payload if isinstance(staged_payload, dict) else None),
                )
            if envelope_id and owner_token:
                try:
                    finalize_envelope_processed(
                        envelope_id,
                        owner_token,
                        response=response,
                        hook_type=hook_type if isinstance(hook_type, str) else None,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to finalize hook envelope %s: %s",
                        envelope_id,
                        exc,
                    )
                return response
            if envelope_id:
                try:
                    mark_envelope_processed(
                        envelope_id,
                        response=response,
                        hook_type=hook_type if isinstance(hook_type, str) else None,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to mark hook envelope %s processed: %s",
                        envelope_id,
                        exc,
                    )
            return response

        try:
            # Parse request
            try:
                raw_payload = await request.json()
            except ClientDisconnect:
                logger.debug(
                    "Hook client disconnected before request body was read",
                    extra=_hook_log_extra(hook_type, request_metadata, error="client_disconnected"),
                )
                return {"continue": True, "decision": "approve"}

            payload, request_metadata = _normalize_hook_request(raw_payload)
            platform_session_id = request.headers.get("X-Gobby-Session-Id", "").strip()
            if platform_session_id:
                payload["_platform_session_id"] = platform_session_id
            if envelope_id:
                input_data = payload.get("input_data")
                if isinstance(input_data, dict):
                    input_data.setdefault("source_event_id", envelope_id)

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

            if source not in SUPPORTED_HOOK_SOURCES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported source: {source}. Supported: "
                        f"{', '.join(SUPPORTED_HOOK_SOURCES)}"
                    ),
                )

            if not envelope_has_hook_response_capability(
                request_metadata.get("response_capability")
            ):
                logger.warning(
                    "Rejecting hook below hook-response capability floor",
                    extra=_hook_log_extra(
                        hook_type,
                        request_metadata,
                        source=source,
                        protocol_diagnostic=(
                            "request-carried response_capability is below hook-response.v1"
                        ),
                    ),
                )
                return _graceful_error_response(
                    hook_type,
                    "hook-response capability below floor",
                    source=source,
                )

            if envelope_id and not claim_envelope_processing(envelope_id):
                stored_response = envelope_terminal_response(envelope_id)
                if stored_response is not None:
                    logger.info("Replaying processed hook envelope %s result", envelope_id)
                    return stored_response
                marker = read_envelope_marker(envelope_id)
                if marker is None and claim_envelope_processing(envelope_id):
                    logger.info("Reclaimed expired hook envelope marker %s", envelope_id)
                elif not isinstance(marker, dict) or not isinstance(marker.get("status"), str):
                    reason = "duplicate envelope marker malformed"
                    logger.debug("Hook envelope %s duplicate: %s", envelope_id, reason)
                    return JSONResponse(
                        status_code=409,
                        content={"status": "malformed_marker", "reason": reason},
                    )
                elif clear_stale_envelope_processing_marker(
                    envelope_id
                ) and claim_envelope_processing(envelope_id):
                    logger.info("Reclaimed stale hook envelope processing marker %s", envelope_id)
                else:
                    status = marker["status"]
                    reason = (
                        "duplicate envelope already processing"
                        if status == "processing"
                        else "duplicate envelope previously processed"
                    )
                    logger.debug("Hook envelope %s duplicate: %s", envelope_id, reason)
                    return JSONResponse(
                        status_code=409,
                        content={
                            "status": status,
                            "reason": reason,
                        },
                    )

            if envelope_id:
                owner_token = envelope_processing_owner_token(envelope_id)
                if owner_token:
                    start_envelope_lease_renewal(envelope_id, owner_token)

            # Select adapter based on source
            from gobby.adapters.agy import AgyAdapter
            from gobby.adapters.claude_code import ClaudeCodeAdapter
            from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
            from gobby.adapters.droid import DroidAdapter
            from gobby.adapters.grok import GrokAdapter
            from gobby.adapters.qwen import QwenAdapter

            if source == "claude":
                adapter = ClaudeCodeAdapter(hook_manager=hook_manager)
            elif source == "qwen":
                adapter = QwenAdapter(hook_manager=hook_manager)
            elif source == "grok":
                adapter = GrokAdapter(hook_manager=hook_manager)
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
            elif source == "droid":
                adapter = DroidAdapter(hook_manager=hook_manager)
            elif source == "agy":
                adapter = AgyAdapter(hook_manager=hook_manager)
            else:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported source: {source}. Supported: "
                        f"{', '.join(SUPPORTED_HOOK_SOURCES)}"
                    ),
                )

            # Execute hook via adapter
            claim_lease = preflight_agy_startup_claim(payload, hook_manager)
            try:
                config = server.config
                hook_timeout = (
                    config.hooks.adapter_timeout
                    if config is not None
                    else HookTimeoutConfig().adapter_timeout
                )
                result = await _run_adapter_hook(
                    adapter,
                    payload,
                    hook_manager,
                    timeout_seconds=hook_timeout,
                )

                # Rule and adapter denials are final. Never let web-chat approval,
                # auto-approval, or browser interaction overwrite them.
                if _result_encodes_denial(result):
                    return mark_processed_and_return(result)

                # After existing hook processing, check for web chat hold-open.
                # Terminal sessions pass straight through; only web_chat sessions
                # create pending interactions that hold the HTTP response open
                # until the user approves/denies in the browser.
                session_header = request.headers.get("X-Gobby-Session-Id", "")
                normalized_hold_open_type = _normalize_hold_open_hook_type(hook_type)
                if session_header and normalized_hold_open_type:
                    hold_open_result = await hook_hold_open._maybe_hold_open(
                        request,
                        session_header,
                        normalized_hold_open_type,
                        payload,
                        source,
                        server=server,
                    )
                    if hold_open_result is not None:
                        return mark_processed_and_return(hold_open_result)

                response_time_ms = (time.perf_counter() - start_time) * 1000
                inc_counter("hooks_succeeded_total")

                logger.debug(
                    "Hook executed: %s",
                    hook_type,
                    extra=_hook_log_extra(
                        hook_type,
                        request_metadata,
                        continue_=result.get("continue"),
                        response_time_ms=response_time_ms,
                    ),
                )

                return mark_processed_and_return(result)

            except AgentRunIngressRetryableError as exc:
                inc_counter("hooks_failed_total")
                if claim_lease is not None:
                    rollback_agy_startup_claim(hook_manager, claim_lease)
                released = bool(envelope_id and release_envelope_processing_claim(envelope_id))
                logger.warning(
                    "Retrying managed hook until durable run identity is available",
                    extra=_hook_log_extra(
                        hook_type,
                        request_metadata,
                        source=source,
                        session_id=exc.session_id,
                        run_id=exc.expected_run_id,
                        reason=exc.reason,
                        envelope_id=envelope_id,
                        processing_claim_released=released,
                    ),
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "retry",
                        "retry_kind": "ingress_backpressure",
                        "reason": "agent_run_identity_pending",
                    },
                )

            except DaemonNotReadyError as exc:
                inc_counter("hooks_failed_total")
                if claim_lease is not None:
                    rollback_agy_startup_claim(hook_manager, claim_lease)
                released = bool(envelope_id and release_envelope_processing_claim(envelope_id))
                logger.warning(
                    "Retrying hook after daemon-not-ready gate",
                    extra=_hook_log_extra(
                        hook_type,
                        request_metadata,
                        source=source,
                        daemon_status=exc.daemon_status,
                        reason=exc.reason,
                        envelope_id=envelope_id,
                        processing_claim_released=released,
                    ),
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "retry",
                        "retry_kind": "ingress_backpressure",
                        "reason": "daemon_not_ready",
                    },
                )

            except ValueError as e:
                # Invalid request - still return graceful response
                inc_counter("hooks_failed_total")
                if claim_lease is not None:
                    rollback_agy_startup_claim(hook_manager, claim_lease)
                if _is_codex_root_context_miss(source, payload, e):
                    logger.debug(
                        "Skipping Codex hook without project context: %s",
                        hook_type,
                        extra=_hook_log_extra(hook_type, request_metadata, error=str(e)),
                    )
                else:
                    logger.warning(
                        "Invalid hook request: %s",
                        hook_type,
                        extra=_hook_log_extra(hook_type, request_metadata, error=str(e)),
                    )
                return mark_processed_and_return(
                    _hook_exception_response(
                        adapter,
                        hook_type,
                        source,
                        request_metadata,
                        str(e),
                    )
                )

            except TimeoutError as exc:
                inc_counter("hooks_failed_total")
                if claim_lease is not None:
                    invalidate_agy_startup_claim(hook_manager, claim_lease)
                timeout_seconds = hook_timeout
                timeout_log_extra = {
                    "source": source,
                    "exception_type": type(exc).__name__,
                    "evaluation_event": getattr(exc, "event_type", hook_type),
                    "evaluation_session_id": getattr(exc, "session_id", None),
                    "evaluation_timeout_seconds": getattr(exc, "timeout_seconds", None),
                    "adapter_queue_duration_seconds": getattr(exc, "queue_duration_seconds", None),
                    "adapter_execution_duration_seconds": getattr(
                        exc, "execution_duration_seconds", None
                    ),
                }
                live_worker = False
                if isinstance(exc, AdapterHookTimeout):
                    executor_future = exc.executor_future
                    if (
                        executor_future is not None
                        and not executor_future.done()
                        and envelope_id
                        and owner_token
                    ):
                        live_worker = True
                        schedule_adapter_timeout_finalization(
                            executor_future,
                            envelope_id=envelope_id,
                            owner_token=owner_token,
                            hook_type=hook_type if isinstance(hook_type, str) else None,
                        )
                if envelope_has_hook_response_capability(
                    request_metadata.get("response_capability")
                ):
                    released = False
                    if not live_worker:
                        released = bool(
                            envelope_id and release_envelope_processing_claim(envelope_id)
                        )
                    logger.warning(
                        "Retrying hook after adapter timeout",
                        extra=_hook_log_extra(
                            hook_type,
                            request_metadata,
                            timeout_seconds=timeout_seconds,
                            envelope_id=envelope_id,
                            processing_claim_released=released,
                            retry_kind="adapter_timeout",
                            **timeout_log_extra,
                        ),
                    )
                    return JSONResponse(
                        status_code=503,
                        content={
                            "status": "retry",
                            "retry_kind": "adapter_timeout",
                        },
                    )
                if not _is_fail_safe_hook(hook_type, request_metadata):
                    logger.warning(
                        "Non-critical hook timed out: %s",
                        hook_type,
                        extra=_hook_log_extra(
                            hook_type,
                            request_metadata,
                            timeout_seconds=timeout_seconds,
                            **timeout_log_extra,
                        ),
                    )
                    return mark_processed_and_return(
                        _graceful_error_response(
                            hook_type,
                            f"hook evaluation timed out after {timeout_seconds:g}s",
                            source=source,
                        )
                    )

                logger.error(
                    "Critical hook timed out: %s",
                    hook_type,
                    extra=_hook_log_extra(
                        hook_type,
                        request_metadata,
                        timeout_seconds=timeout_seconds,
                        **timeout_log_extra,
                    ),
                )
                return mark_processed_and_return(
                    _hook_timeout_response(adapter, hook_type, source, timeout_seconds)
                )

            except HTTPException:
                raise
            except Exception as e:
                # Hook execution error - return graceful response so tool proceeds
                # This prevents confusing "hook failed" warnings in Claude Code
                inc_counter("hooks_failed_total")
                if claim_lease is not None:
                    rollback_agy_startup_claim(hook_manager, claim_lease)
                logger.exception(
                    "Hook execution failed: %s",
                    hook_type,
                    extra=_hook_log_extra(hook_type, request_metadata),
                )
                return mark_processed_and_return(
                    _hook_exception_response(
                        adapter,
                        hook_type,
                        source,
                        request_metadata,
                        str(e),
                    )
                )

        except HTTPException:
            # Re-raise 400 errors (bad request) - these are client errors
            raise
        except Exception as e:
            # Outer exception - return graceful response to prevent CLI warning
            inc_counter("hooks_failed_total")
            logger.exception(
                "Hook endpoint error",
                extra=_hook_log_extra(hook_type, request_metadata),
            )
            if hook_type:
                return mark_processed_and_return(
                    _hook_exception_response(
                        adapter,
                        hook_type,
                        source,
                        request_metadata,
                        str(e),
                    )
                )
            # Fallback: return basic success to prevent CLI hook failure
            return {"continue": True, "decision": "approve"}

    return router
