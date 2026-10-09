"""Dispatch staged terminal handoffs after their successful tool completion event."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from gobby.agents.terminal_delivery import (
    TerminalDeliveryAdmissionClosedError,
    shielded_terminal_delivery,
)
from gobby.app_context import get_app_context
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.handoff_dispatch_recovery import (
    confirm_reclaimed_compact,
    settle_landed_boundary,
)
from gobby.hooks.tool_outcomes import tool_outcome_from_data
from gobby.mcp_proxy.tools.sessions._terminal_clear import deliver_staged_clear_session
from gobby.mcp_proxy.tools.sessions._terminal_compaction import NO_TERMINAL_TARGET_ERROR_CODE
from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    deliver_staged_compact_handoff,
)
from gobby.sessions.clear_continuation import clear_failed_attempt
from gobby.sessions.compact_continuation import persist_pull_prompt_message
from gobby.sessions.handoff import (
    DISPATCH_OWNER,
    HANDOFF_DELIVERY_FAILURES_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    HANDOFF_UNAVAILABLE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ClaimedHandoffDelivery,
    claim_staged_handoff_delivery,
    restore_staged_handoff,
    staged_handoff_rejection,
)
from gobby.sessions.handoff_shutdown import HANDOFF_IN_FLIGHT_MINUTES
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now
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
    "Retry gobby-sessions:set_handoff after clearing any held provider command; "
    "recover the payload first if needed."
)
_COMPACT_UNCONFIRMED_GUIDANCE = (
    "The provider has not confirmed the compact boundary yet. Wait for it, then call "
    "gobby-sessions:get_handoff to recover this attempt. Do not submit /compact again "
    "unless the pane proves the command was never accepted and remains in the composer."
)
_COMPOSER_OCCUPIED_ERROR_CODE = "composer_occupied"
_COMPOSER_OCCUPIED_GUIDANCE = (
    "Terminal handoff delivery was withheld: the operator has an unsent draft in the "
    "composer. Nothing was interrupted. Tell the operator the handoff is waiting on "
    "their draft, then retry gobby-sessions:set_handoff once they have sent or "
    "cleared it."
)
# The recorded CLI no longer owns its pane: no terminal can take the command, so the
# attempt settles without counting toward abandonment and stays readable (#23095).
# Its retry gate stays armed, so the guidance names the retry that gate admits (#23495).
_NO_TERMINAL_TARGET_GUIDANCE = (
    "Terminal handoff delivery found no live terminal seat for this session; no keys "
    "were sent. A resumed seat retries gobby-sessions:set_handoff; the payload stays "
    "recoverable as recovery_guidance describes."
)
# Consecutive failures per session before terminal delivery is abandoned: a CLI
# that cannot take the command twice will not take it a ninth time (#22364).
_MAX_CONSECUTIVE_DELIVERY_FAILURES = 2
_ABANDONED_ERROR_CODE = "handoff_delivery_abandoned"
_COMPACT_FAILED_ATTENTION_REASON = "handoff_delivery_failed"
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
    return _schedule_claimed_delivery(
        claimed,
        session_manager=session_manager,
        agent_run_manager=agent_run_manager,
        event_loop=event_loop,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )


def schedule_staged_handoff_on_stop(
    event: HookEvent,
    *,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
    event_loop: asyncio.AbstractEventLoop | None,
    terminal_manager: Any | None = None,
    terminal_runtime_registry: Any | None = None,
) -> bool:
    """Recover a staged delivery when the provider omitted its AFTER_TOOL hook."""
    if event.event_type is not HookEventType.STOP or event.source not in _TERMINAL_SOURCES:
        return False
    session_id = event.metadata.get("_platform_session_id")
    if not isinstance(session_id, str) or not session_id:
        return False
    variables = SessionVariableManager(session_manager.db).get_variables(session_id)
    marker = variables.get(PENDING_HANDOFF_VARIABLE)
    if not isinstance(marker, Mapping):
        return False
    attempt_id = marker.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        _log_skipped_delivery(session_id, attempt_id, "staged marker has no attempt_id")
        return False
    if (
        marker.get("dispatch_started_at") is not None
        and marker.get("dispatch_owner") == DISPATCH_OWNER
    ):
        # AFTER_TOOL already dispatched this attempt in this daemon; nothing to recover.
        return False
    claimed = claim_staged_handoff_delivery(
        session_manager.db, session_id, attempt_id, recover_unarmed_gate=True
    )
    if claimed is None:
        current = SessionVariableManager(session_manager.db).get_variables(session_id)
        reason = staged_handoff_rejection(current, attempt_id) or "claim changed concurrently"
        _log_skipped_delivery(session_id, attempt_id, reason)
        return False
    return _schedule_claimed_delivery(
        claimed,
        session_manager=session_manager,
        agent_run_manager=agent_run_manager,
        event_loop=event_loop,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )


def _schedule_claimed_delivery(
    claimed: ClaimedHandoffDelivery,
    *,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
    event_loop: asyncio.AbstractEventLoop | None,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> bool:
    db = session_manager.db
    if event_loop is None or event_loop.is_closed():
        _compensate_delivery_failure(db, claimed, "daemon event loop is unavailable")
        return False
    confirm_since: datetime | None = None
    if claimed.reclaimed_dispatch_started_at is not None:
        settled = _settle_dead_dispatch(
            claimed,
            session_manager=session_manager,
            event_loop=event_loop,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=terminal_runtime_registry,
        )
        if isinstance(settled, bool):
            return settled
        confirm_since = settled

    operation = _settle_delivery(
        claimed,
        session_manager=session_manager,
        agent_run_manager=agent_run_manager,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
        confirm_since=confirm_since,
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


def _settle_dead_dispatch(
    claimed: ClaimedHandoffDelivery,
    *,
    session_manager: SessionManager,
    event_loop: asyncio.AbstractEventLoop,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> bool | datetime | None:
    """Settle a claim a dead process left.

    ``None`` means dispatch it again; a datetime means its compact is already
    running, so only confirm the boundary of the dispatch started then.
    """
    db = session_manager.db
    session = session_manager.get(claimed.session_id)
    try:
        started_at = datetime.fromisoformat(str(claimed.reclaimed_dispatch_started_at))
    except ValueError:
        started_at = None
    if session is None or started_at is None or started_at.tzinfo is None:
        _compensate_delivery_failure(db, claimed, "dead dispatch claim is unreadable")
        return False
    if settle_landed_boundary(
        db,
        claimed,
        session,
        started_at,
        event_loop=event_loop,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    ):
        return True
    if started_at > utc_now() - timedelta(minutes=HANDOFF_IN_FLIGHT_MINUTES):
        # PreCompact already moved the row: the compact is running, so typing it
        # again would compact twice.
        if not claimed.clear_session and getattr(session, "status", None) == "awaiting_handoff":
            return started_at
        return None
    _compensate_delivery_failure(
        db,
        claimed,
        f"dispatch died after claiming at {claimed.reclaimed_dispatch_started_at}",
        error_code=None if claimed.clear_session else "compact_unconfirmed",
    )
    return False


def resume_dead_handoff_dispatches(
    machine_id: str,
    *,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
    event_loop: asyncio.AbstractEventLoop,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> int:
    """Reclaim dispatches another daemon process claimed and died holding."""
    rows = session_manager.db.fetchall(
        """
        SELECT s.id, v.variables -> 'set_handoff_pending' ->> 'attempt_id' AS attempt_id
          FROM sessions s JOIN session_variables v ON v.session_id = s.id
         WHERE s.machine_id = %s
           AND s.status NOT IN ('expired', 'deleted')
           AND v.variables -> 'set_handoff_pending' ->> 'dispatch_started_at' IS NOT NULL
           AND (v.variables -> 'set_handoff_pending' ->> 'dispatch_owner') IS DISTINCT FROM %s
        """,
        (machine_id, DISPATCH_OWNER),
    )
    resumed = 0
    for row in rows:
        attempt_id = row["attempt_id"]
        claimed = claim_staged_handoff_delivery(session_manager.db, str(row["id"]), attempt_id)
        if claimed is None:
            _log_skipped_delivery(row["id"], attempt_id, "dead dispatch claim changed")
            continue
        resumed += _schedule_claimed_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=agent_run_manager,
            event_loop=event_loop,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=terminal_runtime_registry,
        )
    return resumed


async def _settle_delivery(
    claimed: ClaimedHandoffDelivery,
    *,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
    confirm_since: datetime | None = None,
) -> None:
    db = session_manager.db

    async def deliver() -> dict[str, Any]:
        if confirm_since is not None:
            session = session_manager.get(claimed.session_id)
            if session is None:
                return {"compacted": False, "reason": f"Session {claimed.session_id} not found"}
            return await confirm_reclaimed_compact(
                db,
                claimed,
                session,
                confirm_since,
                event_loop=asyncio.get_running_loop(),
                terminal_manager=terminal_manager,
                terminal_runtime_registry=terminal_runtime_registry,
            )
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

    async def settle() -> None:
        # Caller cancellation must wait for durable result settlement as well as
        # the physical writer; otherwise a failed handoff stays delivery_pending.
        try:
            result = await deliver()
        except Exception as exc:
            logger.warning(
                "Terminal handoff delivery crashed for session %s",
                claimed.session_id,
                exc_info=True,
            )
            failure = _compensate_delivery_failure(db, claimed, str(exc))
            await _wake_failed_attempt(db, claimed.session_id, failure)
            return

        if _delivery_succeeded(result, clear_session=claimed.clear_session):
            _clear_compact_failure_attention(db, claimed.session_id)
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
        error_code = result.get("error_code")
        failure = _compensate_delivery_failure(
            db,
            claimed,
            str(reason),
            error_code=str(error_code) if isinstance(error_code, str) else None,
        )
        await _wake_failed_attempt(db, claimed.session_id, failure)

    try:
        await shielded_terminal_delivery(
            f"handoff:{claimed.session_id}:{claimed.attempt_id}",
            settle,
            raise_if_closed=True,
        )
    except TerminalDeliveryAdmissionClosedError as exc:
        # The daemon is stopping, so a live wake would be lost; queue the prompt durably.
        failure = _compensate_delivery_failure(db, claimed, str(exc))
        await _queue_failed_attempt(db, claimed.session_id, failure)
    except Exception as exc:
        logger.warning(
            "Terminal handoff delivery scope failed for session %s",
            claimed.session_id,
            exc_info=True,
        )
        failure = _compensate_delivery_failure(db, claimed, str(exc))
        await _wake_failed_attempt(db, claimed.session_id, failure)


def _delivery_succeeded(result: Mapping[str, Any], *, clear_session: bool) -> bool:
    if clear_session:
        return result.get("success") is True or result.get("attempt_pending") is True
    return result.get("compacted") is True


def _consecutive_delivery_failures(db: HubDatabase, session_id: str) -> int:
    count = (
        SessionVariableManager(db)
        .get_variable_subset(session_id, (HANDOFF_DELIVERY_FAILURES_VARIABLE,))
        .get(HANDOFF_DELIVERY_FAILURES_VARIABLE)
    )
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _attention_manager(db: HubDatabase) -> AttentionStateManager:
    container = get_app_context()
    configured = getattr(container, "attention_manager", None)
    return (
        configured
        if isinstance(configured, AttentionStateManager) and configured.db is db
        else AttentionStateManager(db)
    )


def _compensate_delivery_failure(
    db: HubDatabase,
    claimed: ClaimedHandoffDelivery | StagedTerminalHandoff,
    reason: str,
    *,
    error_code: str | None = None,
) -> dict[str, Any] | None:
    """Settle a failed attempt; return its failure when a live seat should hear of it."""
    seatless = error_code == NO_TERMINAL_TARGET_ERROR_CODE
    failures = _consecutive_delivery_failures(db, claimed.session_id) + 1
    abandoned = not seatless and failures >= _MAX_CONSECUTIVE_DELIVERY_FAILURES
    guidance = _RETRY_GUIDANCE
    if error_code == _COMPOSER_OCCUPIED_ERROR_CODE:
        guidance = _COMPOSER_OCCUPIED_GUIDANCE
    elif error_code == "compact_unconfirmed":
        guidance = _COMPACT_UNCONFIRMED_GUIDANCE
    elif seatless:
        guidance = _NO_TERMINAL_TARGET_GUIDANCE
    recovery_guidance = (
        "Authored content is available through "
        f"gobby-sessions:get_handoff(failed_attempt_id={claimed.attempt_id!r}); "
        "this explicit read does not deliver it."
    )
    if error_code == "compact_unconfirmed":
        recovery_guidance = (
            "Once a compact boundary arrives, call "
            "gobby-sessions:get_handoff(failed_attempt_id="
            f"{claimed.attempt_id!r}, reconcile_late_compact=true) to deliver this attempt."
        )
    failure: dict[str, Any] = {
        "compacted": False,
        "delivery_failed": not abandoned,
        "delivery_pending": False,
        "delivery_abandoned": abandoned,
        "delivery_state": "failed_not_deliverable",
        "attempt_id": claimed.attempt_id,
        "clear_session": claimed.clear_session,
        "reason": reason,
        "retry_guidance": _ABANDON_GUIDANCE if abandoned else guidance,
        "recovery_guidance": recovery_guidance,
    }
    if abandoned:
        failure["error_code"] = _ABANDONED_ERROR_CODE
    elif error_code is not None:
        failure["error_code"] = error_code
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
        # A boundary receipt or a newer attempt won the race. Never replace its gate.
        logger.info(
            "Ignored stale terminal handoff failure for session %s attempt %s",
            claimed.session_id,
            claimed.attempt_id,
        )
        return None
    # A missing seat is not a delivery the CLI refused, so it neither nears
    # abandonment nor lifts require-handoff-at-context-limit for the next seat.
    if not seatless:
        updates: dict[str, Any] = {HANDOFF_DELIVERY_FAILURES_VARIABLE: failures}
        if abandoned:
            # Lifts require-handoff-at-context-limit; the epoch reset clears it again.
            updates[HANDOFF_UNAVAILABLE_VARIABLE] = True
        SessionVariableManager(db).merge_variables(claimed.session_id, updates)
    if error_code == "compact_failed":
        try:
            _attention_manager(db).transition(
                session_attention_entry_id(claimed.session_id),
                state="blocked",
                session_id=claimed.session_id,
                reason=_COMPACT_FAILED_ATTENTION_REASON,
                kind="actionable",
                fingerprint=f"compact-handoff:{claimed.attempt_id}",
                payload={"attempt_id": claimed.attempt_id, "message": reason},
            )
        except Exception:
            logger.warning(
                "Failed raising compact delivery attention for session %s",
                claimed.session_id,
                exc_info=True,
            )
    if abandoned:
        logger.warning(
            "Terminal handoff delivery abandoned for session %s after %d consecutive "
            "failures; attempt %s: %s",
            claimed.session_id,
            failures,
            claimed.attempt_id,
            reason,
        )
        return failure
    logger.warning(
        "Terminal handoff delivery failed for session %s attempt %s: %s",
        claimed.session_id,
        claimed.attempt_id,
        reason,
    )
    return None if seatless else failure


async def _wake_failed_attempt(
    db: HubDatabase, session_id: str, failure: Mapping[str, Any] | None
) -> None:
    """Tell the seat its attempt failed; an idle seat otherwise waits for a keystroke."""
    if failure is None:
        return
    dispatcher = getattr(get_app_context(), "wake_dispatcher", None)
    if dispatcher is None:
        await _queue_failed_attempt(db, session_id, failure)
        return
    attempt_id = str(failure["attempt_id"])
    await dispatcher.wake(
        session_id,
        _failed_attempt_prompt(failure),
        {
            "message_type": "handoff_delivery_failed",
            "completion_id": f"handoff-failed:{attempt_id}",
            "attempt_id": attempt_id,
            "error_code": failure.get("error_code"),
        },
        bypass_debounce=True,
    )


async def _queue_failed_attempt(
    db: HubDatabase, session_id: str, failure: Mapping[str, Any] | None
) -> None:
    """Persist the failed-attempt pull prompt for the seat's next turn, without a live wake."""
    if failure is None:
        return
    await asyncio.to_thread(
        persist_pull_prompt_message,
        db,
        session_id,
        _failed_attempt_prompt(failure),
        str(failure["attempt_id"]),
    )


def _failed_attempt_prompt(failure: Mapping[str, Any]) -> str:
    return (
        f"Gobby handoff delivery failed: {failure['reason']}. "
        f"{failure['retry_guidance']} {failure['recovery_guidance']}"
    )


def _clear_compact_failure_attention(db: HubDatabase, session_id: str) -> None:
    try:
        manager = _attention_manager(db)
        entry_id = session_attention_entry_id(session_id)
        current = manager.get(entry_id)
        if current is None or current.reason != _COMPACT_FAILED_ATTENTION_REASON:
            return
        manager.transition(
            entry_id,
            state=None,
            expected_attention_id=current.attention_id,
            expected_fingerprint=current.fingerprint,
        )
    except Exception:
        logger.warning(
            "Failed clearing compact delivery attention for session %s",
            session_id,
            exc_info=True,
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
