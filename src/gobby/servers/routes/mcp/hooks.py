"""
Hooks management routes for Gobby HTTP server.

Provides hook execution endpoint for CLI adapters.
Extracted from base.py as part of Strangler Fig decomposition.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final

from fastapi import APIRouter, HTTPException, Request
from starlette.requests import ClientDisconnect

from gobby.adapters.agy_contract import (
    agy_execution_num,
    strip_unbudgeted_force_continue,
)
from gobby.config.hooks import HookTimeoutConfig
from gobby.hooks.adapter_execution import HOOK_ADAPTER_MAX_WORKERS as _HOOK_ADAPTER_MAX_WORKERS
from gobby.hooks.adapter_execution import (
    AdapterHookTimeout,
    register_adapter_timeout_finalization,
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
    envelope_terminal_response,
    finalize_envelope_processed,
    mark_envelope_processed,
    read_envelope_marker,
    release_envelope_processing_claim,
)
from gobby.hooks.health_gate import DaemonNotReadyError
from gobby.hooks.inbox import consume_pending_delivery_receipts
from gobby.hooks.phase_timing import (
    HookPhaseTimings,
    SlowHookSummaryReporter,
    SlowHookWindowSummary,
    hook_phase_timing_scope,
    observe_hook_phase_timings,
    timed_to_thread,
)
from gobby.hooks.receipt_effects import STAGED_EFFECTS_FIELD
from gobby.hooks.receipt_redelivery import (
    attach_delivery_receipt,
    receipt_guarded_response,
    receipt_session_id,
)
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    envelope_has_hook_response_capability,
)
from gobby.hooks.startup_claim_preflight import (
    StartupClaimLease,
    StartupClaimPreflightTimeout,
    invalidate_agy_startup_claim,
    preflight_agy_startup_claim_bounded,
    preflight_timeout_seconds,
    rollback_agy_startup_claim,
    strip_private_startup_claim_fields,
)
from gobby.servers.responses import JSONResponse
from gobby.servers.routes.mcp import hook_hold_open
from gobby.servers.routes.mcp.hook_responses import (
    _graceful_error_response,
    _hook_exception_response,
    _hook_timeout_response,
    _is_fail_safe_hook,
    _normalize_hold_open_hook_type,
    _result_encodes_denial,
)
from gobby.telemetry.instruments import inc_counter

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


HOOK_ADAPTER_MAX_WORKERS = _HOOK_ADAPTER_MAX_WORKERS
SUPPORTED_HOOK_SOURCES: Final = ("claude", "grok", "qwen", "codex", "droid", "agy")


def _log_slow_hook_summary(summary: SlowHookWindowSummary) -> None:
    logger.warning(
        "Slow hook summary: count=%d suppressed=%d max_seconds=%.1f "
        "window_seconds=%.0f by_phase=%s",
        summary.count,
        summary.suppressed,
        summary.max_seconds,
        summary.window_seconds,
        summary.by_phase,
    )


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


def create_hooks_router(server: "HTTPServer") -> APIRouter:
    """
    Create hooks router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with hooks endpoints
    """
    slow_hook_reporter = SlowHookSummaryReporter(emit=_log_slow_hook_summary)

    @asynccontextmanager
    async def drain_slow_hook_summary(_app: Any) -> AsyncIterator[None]:
        try:
            yield
        finally:
            slow_hook_reporter.close()

    # FastAPI nests this inside the app lifespan, so the drain runs while logging is live.
    router = APIRouter(prefix="/api/hooks", tags=["hooks"], lifespan=drain_slow_hook_summary)

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
        phase_timings = HookPhaseTimings()
        inc_counter("hooks_total")
        hook_type: str | None = None  # Track for error handling
        source: str | None = None  # Track for error handling
        adapter: Any | None = None
        claim_lease: StartupClaimLease | None = None
        owner_token: str | None = None
        lease_renewal: asyncio.Task[None] | None = None
        # An adapter worker that outlived its timeout still owns the lease;
        # its done-callback finalizes or releases it and the renewal loop
        # stops on its own once that CAS lands.
        lease_outlives_request = False
        request_metadata: dict[str, Any] = {
            "request_shape": "unknown",
            "schema_version": None,
            "critical": None,
            "enqueued_at": None,
        }
        envelope_id = request.headers.get(ENVELOPE_ID_HEADER, "").strip()
        payload: dict[str, Any] = {}
        platform_session_id = ""

        def _mark_processed_and_return(response: dict[str, Any]) -> Any:
            staged_payload = response.get(STAGED_EFFECTS_FIELD)
            response = strip_private_startup_claim_fields(response)
            receipt_db = getattr(getattr(server, "services", None), "database", None)
            if claim_lease is not None:
                staged = dict(staged_payload) if isinstance(staged_payload, dict) else {}
                staged["startup_context"] = {
                    "generation": claim_lease.generation,
                    "owner_token": claim_lease.owner_token,
                    "session_id": claim_lease.session_id,
                }
                staged_payload = staged
            if envelope_id:
                execution_num = None
                if response.get("terminationBehavior") == "force_continue":
                    execution_num = agy_execution_num(payload)
                response = attach_delivery_receipt(
                    response,
                    db=receipt_db,
                    envelope_id=envelope_id,
                    session_id=receipt_session_id(
                        claim_lease=claim_lease,
                        payload=payload,
                        platform_session_id=platform_session_id,
                        envelope_id=envelope_id,
                    ),
                    staged_payload=(staged_payload if isinstance(staged_payload, dict) else None),
                    force_continue_execution_num=execution_num,
                    skip_empty_receipt=hook_type in ("PreToolUse", "pre-tool-use", "BeforeTool"),
                )
            else:
                response = strip_unbudgeted_force_continue(response)
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
                return receipt_guarded_response(response, db=receipt_db)
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
            return receipt_guarded_response(response, db=receipt_db)

        async def mark_processed_and_return(response: dict[str, Any]) -> Any:
            # Receipt and envelope persistence block on the database and inbox
            # files, so they stay off the HTTP loop (#22708).
            with phase_timings.measure("persistence_broadcast"):
                return await timed_hop("persistence_receipt", _mark_processed_and_return, response)

        async def timed_hop[T](
            phase: str, function: Callable[..., T], /, *args: Any, **kwargs: Any
        ) -> T:
            # Route hops run outside the adapter worker's timing scope; without
            # their own, their wall time lands unattributed in `response` (#23063).
            with hook_phase_timing_scope(phase_timings):
                return await timed_to_thread(phase, function, *args, **kwargs)

        try:
            # Parse request
            try:
                with phase_timings.measure("request_body"):
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
            enqueued_at = request_metadata.get("enqueued_at")
            if isinstance(enqueued_at, str) and enqueued_at:
                payload["_enqueued_at"] = enqueued_at
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

            # Land in-flight delivery-receipt acks before this response's
            # carry-forward can presume the previous delivery lost and bump
            # the generation those acks would CAS against.
            try:
                with phase_timings.measure("persistence_broadcast"):
                    await timed_hop(
                        "persistence_consume_receipts",
                        consume_pending_delivery_receipts,
                        request.app,
                    )
            except Exception:
                logger.warning(
                    "Pending delivery-receipt sweep failed; the periodic drain remains",
                    exc_info=True,
                )

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

            if envelope_id:
                owner_token = await timed_hop(
                    "envelope_claim", claim_envelope_processing, envelope_id
                )
            if envelope_id and not owner_token:
                stored_response = await timed_hop(
                    "envelope_claim", envelope_terminal_response, envelope_id
                )
                if stored_response is not None:
                    logger.info("Replaying processed hook envelope %s result", envelope_id)
                    return stored_response
                marker = await timed_hop("envelope_claim", read_envelope_marker, envelope_id)
                if marker is None and (
                    owner_token := await timed_hop(
                        "envelope_claim", claim_envelope_processing, envelope_id
                    )
                ):
                    logger.info("Reclaimed expired hook envelope marker %s", envelope_id)
                elif not isinstance(marker, dict) or not isinstance(marker.get("status"), str):
                    reason = "duplicate envelope marker malformed"
                    logger.debug("Hook envelope %s duplicate: %s", envelope_id, reason)
                    return JSONResponse(
                        status_code=409,
                        content={"status": "malformed_marker", "reason": reason},
                    )
                elif await timed_hop(
                    "envelope_claim", clear_stale_envelope_processing_marker, envelope_id
                ) and (
                    owner_token := await timed_hop(
                        "envelope_claim", claim_envelope_processing, envelope_id
                    )
                ):
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

            if envelope_id and owner_token:
                lease_renewal = start_envelope_lease_renewal(envelope_id, owner_token)

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

            config = server.config
            hook_timeout = (
                config.hooks.adapter_timeout
                if config is not None
                else HookTimeoutConfig().adapter_timeout
            )
            try:
                claim_lease = await preflight_agy_startup_claim_bounded(
                    payload,
                    hook_manager,
                    timeout_seconds=preflight_timeout_seconds(hook_timeout),
                )
            except StartupClaimPreflightTimeout as exc:
                inc_counter("hooks_failed_total")
                released = bool(
                    envelope_id
                    and owner_token
                    and await asyncio.to_thread(
                        release_envelope_processing_claim, envelope_id, owner_token=owner_token
                    )
                )
                logger.warning(
                    "Retrying hook after startup-claim preflight timeout",
                    extra=_hook_log_extra(
                        hook_type,
                        request_metadata,
                        source=source,
                        envelope_id=envelope_id,
                        processing_claim_released=released,
                        retry_kind="preflight_timeout",
                        error=str(exc),
                    ),
                )
                return JSONResponse(
                    status_code=503,
                    content={"status": "retry", "retry_kind": "preflight_timeout"},
                )

            # Execute hook via adapter
            try:
                result = await _run_adapter_hook(
                    adapter,
                    payload,
                    hook_manager,
                    timeout_seconds=hook_timeout,
                    phase_timings=phase_timings,
                )

                # Rule and adapter denials are final. Never let web-chat approval,
                # auto-approval, or browser interaction overwrite them.
                if _result_encodes_denial(result):
                    return await mark_processed_and_return(result)

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
                        return await mark_processed_and_return(hold_open_result)

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

                return await mark_processed_and_return(result)

            except AgentRunIngressRetryableError as exc:
                inc_counter("hooks_failed_total")
                if claim_lease is not None:
                    await asyncio.to_thread(rollback_agy_startup_claim, hook_manager, claim_lease)
                released = bool(
                    envelope_id
                    and owner_token
                    and await asyncio.to_thread(
                        release_envelope_processing_claim, envelope_id, owner_token=owner_token
                    )
                )
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
                    await asyncio.to_thread(rollback_agy_startup_claim, hook_manager, claim_lease)
                released = bool(
                    envelope_id
                    and owner_token
                    and await asyncio.to_thread(
                        release_envelope_processing_claim, envelope_id, owner_token=owner_token
                    )
                )
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
                    await asyncio.to_thread(rollback_agy_startup_claim, hook_manager, claim_lease)
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
                return await mark_processed_and_return(
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
                    await asyncio.to_thread(invalidate_agy_startup_claim, hook_manager, claim_lease)
                timeout_seconds = hook_timeout
                timeout_log_extra = {
                    "source": source,
                    "exception_type": type(exc).__name__,
                    "evaluation_event": getattr(exc, "event_type", hook_type),
                    "evaluation_session_id": getattr(exc, "session_id", None),
                    "evaluation_timeout_seconds": getattr(exc, "timeout_seconds", None),
                    "adapter_admission_wait_seconds": getattr(exc, "admission_wait_seconds", None),
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
                        lease_outlives_request = True
                        try:
                            await register_adapter_timeout_finalization(
                                executor_future,
                                envelope_id=envelope_id,
                                owner_token=owner_token,
                                hook_type=hook_type if isinstance(hook_type, str) else None,
                            )
                        except Exception:
                            lease_outlives_request = False
                            raise
                if envelope_has_hook_response_capability(
                    request_metadata.get("response_capability")
                ):
                    released = False
                    if not live_worker:
                        released = bool(
                            envelope_id
                            and owner_token
                            and await asyncio.to_thread(
                                release_envelope_processing_claim,
                                envelope_id,
                                owner_token=owner_token,
                            )
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
                    return await mark_processed_and_return(
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
                return await mark_processed_and_return(
                    _hook_timeout_response(adapter, hook_type, source, timeout_seconds)
                )

            except HTTPException:
                raise
            except Exception as e:
                # Hook execution error - return graceful response so tool proceeds
                # This prevents confusing "hook failed" warnings in Claude Code
                inc_counter("hooks_failed_total")
                if claim_lease is not None:
                    await asyncio.to_thread(rollback_agy_startup_claim, hook_manager, claim_lease)
                logger.exception(
                    "Hook execution failed: %s",
                    hook_type,
                    extra=_hook_log_extra(hook_type, request_metadata),
                )
                return await mark_processed_and_return(
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
                return await mark_processed_and_return(
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
        finally:
            # The lease dies with this execution, including a client
            # disconnect or a cancelled replay. Releasing is a CAS on the live
            # lease this request owns, so a finalized marker is untouched.
            with phase_timings.measure("persistence_broadcast"):
                if not lease_outlives_request:
                    if lease_renewal is not None:
                        lease_renewal.cancel()
                    if envelope_id and owner_token:
                        await timed_hop(
                            "persistence_release_claim",
                            release_envelope_processing_claim,
                            envelope_id,
                            owner_token=owner_token,
                        )
            total_seconds = time.perf_counter() - start_time
            dominant_phase, dominant_seconds, phase_durations = observe_hook_phase_timings(
                phase_timings,
                total_seconds=total_seconds,
                hook_type=hook_type,
                source=source,
            )
            if slow_hook_reporter.observe(
                total_seconds=total_seconds, dominant_phase=dominant_phase
            ):
                logger.warning(
                    "Slow hook execution dominated by %s",
                    dominant_phase,
                    extra={
                        "hook_type": hook_type,
                        "source": source,
                        "total_seconds": total_seconds,
                        "dominant_phase": dominant_phase,
                        "dominant_phase_seconds": dominant_seconds,
                        "session_id": phase_timings.session_id,
                        "rule_evaluation_breakdown_seconds": phase_timings.breakdown(),
                        "hub_query_latency_ms": phase_timings.query_latency_summary_ms(),
                        "hook_phase_durations_seconds": phase_durations,
                    },
                )

    return router
