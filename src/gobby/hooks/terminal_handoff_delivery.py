"""Dispatch staged terminal handoffs after their successful tool completion event."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from gobby.agents.terminal_delivery import (
    TerminalDeliveryAdmissionClosedError,
    shielded_terminal_delivery,
)
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.tool_outcomes import tool_outcome_from_data
from gobby.mcp_proxy.tools.sessions._terminal_clear import deliver_staged_clear_session
from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    deliver_staged_compact_handoff,
)
from gobby.sessions.clear_continuation import clear_failed_attempt
from gobby.sessions.handoff import (
    HANDOFF_DELIVERY_FAILURES_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    HANDOFF_UNAVAILABLE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ClaimedHandoffDelivery,
    claim_staged_handoff_delivery,
    restore_staged_handoff,
    staged_handoff_rejection,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

logger = logging.getLogger(__name__)
_delivery_futures: set[Future[None]] = set()
_delivery_futures_lock = threading.Lock()

_TERMINAL_SOURCES = frozenset(
    {
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.GROK,
        SessionSource.QWEN,
        SessionSource.DROID,
    }
)
_RETRY_GUIDANCE = (
    "Terminal handoff delivery failed after set_handoff returned. "
    "Retry gobby-sessions:set_handoff before calling any other tool."
)
# Consecutive failures per session before terminal delivery is abandoned: a CLI
# that cannot take the command twice will not take it a ninth time (#22364).
_MAX_CONSECUTIVE_DELIVERY_FAILURES = 2
_ABANDONED_ERROR_CODE = "handoff_delivery_abandoned"
_ABANDON_GUIDANCE = (
    "Terminal handoff delivery failed twice in a row; delivery is abandoned for this "
    "session. Do not call set_handoff again. Continue the task with the remaining "
    "context and keep tool results small."
)


@dataclass(frozen=True, slots=True)
class StagedTerminalHandoff:
    session_id: str
    attempt_id: str
    clear_session: bool


def _is_successful_set_handoff_completion(event: HookEvent) -> bool:
    data = event.data or {}
    return (
        event.event_type is HookEventType.AFTER_TOOL
        and data.get("mcp_server") == "gobby-sessions"
        and data.get("mcp_tool") == "set_handoff"
        and tool_outcome_from_data(data).succeeded is True
    )


def _unwrap_tool_output(output: object) -> tuple[Mapping[str, Any] | None, str]:
    """Return the set_handoff result inside the call_tool envelope, or a rejection reason.

    The proxy strips the tool's top-level ``success`` key from the nested result,
    so only the envelope's ``success`` carries meaning here (#21713).
    """
    if not isinstance(output, Mapping):
        return None, f"tool_output is {type(output).__name__}, not the call_tool envelope"
    nested = output.get("result")
    if not isinstance(nested, Mapping):
        return output, ""
    if output.get("success") is not True:
        return None, "call_tool envelope success is not true"
    return nested, ""


def _validate_staged_result(
    event: HookEvent,
    payload: Mapping[str, Any],
) -> StagedTerminalHandoff | str:
    """Return the staged dispatch for a delivery-pending result, or the rejection reason."""
    session_id = payload.get("session_id")
    attempt_id = payload.get("attempt_id")
    clear_session = payload.get("clear_session")
    if not isinstance(session_id, str) or not session_id:
        return "result has no session_id"
    if not isinstance(attempt_id, str) or not attempt_id:
        return "result has no attempt_id"
    if not isinstance(clear_session, bool):
        return "result clear_session is not a bool"
    platform_session_id = event.metadata.get("_platform_session_id")
    if session_id != platform_session_id:
        return f"result session {session_id} is not the hook session {platform_session_id}"
    if event.source not in _TERMINAL_SOURCES:
        return f"session source {event.source.value!r} is not a terminal CLI"
    # set_handoff reports delivery_pending only for terminal sessions, so the
    # result is the session_type authority; AFTER_TOOL metadata carries none
    # (live-verified 2026-09-03: "session_type None is not terminal", #21713).
    return StagedTerminalHandoff(session_id, attempt_id, clear_session)


@dataclass(frozen=True, slots=True)
class SkippedTerminalHandoff:
    """A delivery-pending set_handoff completion the hook cannot dispatch."""

    session_id: str | None
    attempt_id: str | None
    clear_session: bool | None
    reason: str


def _log_skipped_delivery(session_id: object, attempt_id: object, reason: str) -> None:
    logger.warning(
        "Terminal handoff delivery skipped for session %s attempt %s: %s",
        session_id or "unknown",
        attempt_id or "unknown",
        reason,
    )


def classify_set_handoff_completion(
    event: HookEvent,
) -> StagedTerminalHandoff | SkippedTerminalHandoff | None:
    """Classify an AFTER_TOOL event as staged for delivery, skipped, or not a delivery."""
    if not _is_successful_set_handoff_completion(event):
        return None
    platform_session_id = event.metadata.get("_platform_session_id")
    payload, rejection = _unwrap_tool_output((event.data or {}).get("tool_output"))
    if payload is None:
        return SkippedTerminalHandoff(platform_session_id, None, None, rejection)
    if payload.get("handoff_staged") is not True or payload.get("delivery_pending") is not True:
        # Synchronous (web chat) and failed results stage nothing for terminal delivery.
        return None
    staged = _validate_staged_result(event, payload)
    if isinstance(staged, str):
        session_id = payload.get("session_id")
        attempt_id = payload.get("attempt_id")
        clear_session = payload.get("clear_session")
        return SkippedTerminalHandoff(
            session_id if isinstance(session_id, str) and session_id else platform_session_id,
            attempt_id if isinstance(attempt_id, str) and attempt_id else None,
            clear_session if isinstance(clear_session, bool) else None,
            staged,
        )
    return staged


def staged_handoff_from_event(event: HookEvent) -> StagedTerminalHandoff | None:
    """Validate a successful set_handoff completion whose result awaits terminal delivery.

    Every delivery-pending completion this rejects is logged at WARNING so a
    silent miss shows up in daemon.log.
    """
    outcome = classify_set_handoff_completion(event)
    if isinstance(outcome, SkippedTerminalHandoff):
        _log_skipped_delivery(outcome.session_id, outcome.attempt_id, outcome.reason)
        return None
    return outcome


def _settle_skipped_delivery(
    db: HubDatabase,
    event: HookEvent,
    skipped: SkippedTerminalHandoff,
) -> None:
    """Fail the staged attempt so the session is not wedged behind a delivery that never runs."""
    if (
        skipped.session_id is None
        or skipped.attempt_id is None
        or skipped.clear_session is None
        or skipped.session_id != event.metadata.get("_platform_session_id")
    ):
        _log_skipped_delivery(skipped.session_id, skipped.attempt_id, skipped.reason)
        return
    _compensate_delivery_failure(
        db,
        StagedTerminalHandoff(skipped.session_id, skipped.attempt_id, skipped.clear_session),
        skipped.reason,
    )


def _settle_unclaimed_delivery(db: HubDatabase, staged: StagedTerminalHandoff) -> None:
    try:
        variables = SessionVariableManager(db).get_variables(staged.session_id)
        reason = staged_handoff_rejection(variables, staged.attempt_id)
    except Exception as exc:
        _log_skipped_delivery(
            staged.session_id, staged.attempt_id, f"session variables unreadable: {exc}"
        )
        return
    reason = reason or "claim rejected although the staged marker now looks claimable"
    marker = variables.get(PENDING_HANDOFF_VARIABLE)
    attempt_is_idle = (
        isinstance(marker, Mapping)
        and marker.get("attempt_id") == staged.attempt_id
        and marker.get("dispatch_started_at") is None
    )
    if not attempt_is_idle:
        # Already dispatched, superseded, or consumed: nothing is left to fail.
        _log_skipped_delivery(staged.session_id, staged.attempt_id, reason)
        return
    _compensate_delivery_failure(db, staged, reason)


def schedule_terminal_handoff_delivery(
    event: HookEvent,
    *,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
    event_loop: asyncio.AbstractEventLoop | None,
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
) -> bool:
    """Atomically claim and schedule one post-result terminal handoff delivery."""
    outcome = classify_set_handoff_completion(event)
    if outcome is None:
        return False
    db = session_manager.db
    if isinstance(outcome, SkippedTerminalHandoff):
        _settle_skipped_delivery(db, event, outcome)
        return False
    staged = outcome
    claimed = claim_staged_handoff_delivery(db, staged.session_id, staged.attempt_id)
    if claimed is None:
        _settle_unclaimed_delivery(db, staged)
        return False
    if event_loop is None or event_loop.is_closed():
        _compensate_delivery_failure(db, claimed, "daemon event loop is unavailable")
        return False

    operation = _settle_delivery(
        claimed,
        session_manager=session_manager,
        agent_run_manager=agent_run_manager,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )
    try:
        future = asyncio.run_coroutine_threadsafe(operation, event_loop)
    except Exception as exc:
        operation.close()
        _compensate_delivery_failure(db, claimed, str(exc))
        return False
    with _delivery_futures_lock:
        _delivery_futures.add(future)
    future.add_done_callback(lambda done: _log_delivery_completion(done, claimed))
    logger.info(
        "Terminal handoff delivery scheduled for session %s attempt %s",
        claimed.session_id,
        claimed.attempt_id,
    )
    return True


async def _settle_delivery(
    claimed: ClaimedHandoffDelivery,
    *,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> None:
    db = session_manager.db

    async def deliver() -> dict[str, Any]:
        if claimed.clear_session:
            return await deliver_staged_clear_session(
                claimed.session_id,
                claimed.attempt_id,
                session_manager=session_manager,
                db=db,
                agent_run_manager=agent_run_manager,
                terminal_manager=terminal_manager,
                terminal_runtime_registry=terminal_runtime_registry,
            )
        return await deliver_staged_compact_handoff(
            claimed.session_id,
            claimed.attempt_id,
            claimed.handoff_record_id,
            session_manager=session_manager,
            db=db,
            agent_run_manager=agent_run_manager,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=terminal_runtime_registry,
        )

    try:
        result = await shielded_terminal_delivery(
            f"handoff:{claimed.session_id}:{claimed.attempt_id}",
            deliver,
            raise_if_closed=True,
        )
    except TerminalDeliveryAdmissionClosedError as exc:
        _compensate_delivery_failure(db, claimed, str(exc))
        return
    except Exception as exc:
        logger.warning(
            "Terminal handoff delivery crashed for session %s",
            claimed.session_id,
            exc_info=True,
        )
        _compensate_delivery_failure(db, claimed, str(exc))
        return

    if _delivery_succeeded(result, clear_session=claimed.clear_session):
        if result.get("attempt_pending") is True:
            SessionVariableManager(db).merge_variables(
                claimed.session_id,
                {
                    HANDOFF_DISPATCH_GATE_VARIABLE: {
                        "attempt_pending": True,
                        "attempt_id": claimed.attempt_id,
                        "clear_session": True,
                    }
                },
            )
        logger.info(
            "Terminal handoff delivered for session %s attempt %s (clear_session=%s cli=%s via=%s)",
            claimed.session_id,
            claimed.attempt_id,
            claimed.clear_session,
            result.get("cli"),
            result.get("via"),
        )
        return
    reason = result.get("reason") or result.get("error") or "terminal delivery failed"
    _compensate_delivery_failure(db, claimed, str(reason))


def _delivery_succeeded(result: Mapping[str, Any], *, clear_session: bool) -> bool:
    if clear_session:
        return result.get("success") is True or result.get("attempt_pending") is True
    return result.get("compacted") is True


def _consecutive_delivery_failures(db: HubDatabase, session_id: str) -> int:
    count = (
        SessionVariableManager(db).get_variables(session_id).get(HANDOFF_DELIVERY_FAILURES_VARIABLE)
    )
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _compensate_delivery_failure(
    db: HubDatabase,
    claimed: ClaimedHandoffDelivery | StagedTerminalHandoff,
    reason: str,
) -> None:
    failures = _consecutive_delivery_failures(db, claimed.session_id) + 1
    abandoned = failures >= _MAX_CONSECUTIVE_DELIVERY_FAILURES
    failure: dict[str, Any] = {
        "compacted": False,
        "delivery_failed": not abandoned,
        "delivery_pending": False,
        "delivery_abandoned": abandoned,
        "attempt_id": claimed.attempt_id,
        "clear_session": claimed.clear_session,
        "reason": reason,
        "retry_guidance": _ABANDON_GUIDANCE if abandoned else _RETRY_GUIDANCE,
    }
    if abandoned:
        failure["error_code"] = _ABANDONED_ERROR_CODE
    if claimed.clear_session:
        restored = clear_failed_attempt(
            db,
            claimed.session_id,
            attempt_id=claimed.attempt_id,
            marker_updates={HANDOFF_DISPATCH_GATE_VARIABLE: failure},
        )
    else:
        restored = restore_staged_handoff(
            db,
            claimed.session_id,
            claimed.attempt_id,
            failure_result=failure,
        )
    if not restored:
        SessionVariableManager(db).merge_variables(
            claimed.session_id,
            {HANDOFF_DISPATCH_GATE_VARIABLE: failure},
        )
    updates: dict[str, Any] = {HANDOFF_DELIVERY_FAILURES_VARIABLE: failures}
    if abandoned:
        # Lifts require-handoff-at-context-limit; the epoch reset clears it again.
        updates[HANDOFF_UNAVAILABLE_VARIABLE] = True
    SessionVariableManager(db).merge_variables(claimed.session_id, updates)
    if abandoned:
        logger.warning(
            "Terminal handoff delivery abandoned for session %s after %d consecutive "
            "failures; attempt %s: %s",
            claimed.session_id,
            failures,
            claimed.attempt_id,
            reason,
        )
        return
    logger.warning(
        "Terminal handoff delivery failed for session %s attempt %s: %s",
        claimed.session_id,
        claimed.attempt_id,
        reason,
    )


def _log_delivery_completion(
    future: Future[None],
    claimed: ClaimedHandoffDelivery,
) -> None:
    with _delivery_futures_lock:
        _delivery_futures.discard(future)
    try:
        future.result()
    except Exception:
        logger.exception(
            "Terminal handoff settlement escaped for session %s attempt %s",
            claimed.session_id,
            claimed.attempt_id,
        )
