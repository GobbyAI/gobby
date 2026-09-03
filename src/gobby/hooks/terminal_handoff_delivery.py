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
from gobby.mcp_proxy.tools.sessions._terminal import deliver_staged_compact_handoff
from gobby.mcp_proxy.tools.sessions._terminal_clear import deliver_staged_clear_session
from gobby.sessions.clear_continuation import clear_failed_attempt
from gobby.sessions.handoff import (
    HANDOFF_DISPATCH_GATE_VARIABLE,
    ClaimedHandoffDelivery,
    claim_staged_handoff_delivery,
    restore_staged_handoff,
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


@dataclass(frozen=True, slots=True)
class StagedTerminalHandoff:
    session_id: str
    attempt_id: str
    clear_session: bool


def staged_handoff_from_event(event: HookEvent) -> StagedTerminalHandoff | None:
    """Validate a normalized successful set_handoff completion."""
    data = event.data or {}
    if (
        event.event_type is not HookEventType.AFTER_TOOL
        or event.source not in _TERMINAL_SOURCES
        or event.metadata.get("session_type") != "terminal"
        or data.get("mcp_server") != "gobby-sessions"
        or data.get("mcp_tool") != "set_handoff"
        or tool_outcome_from_data(data).succeeded is not True
    ):
        return None

    output = data.get("tool_output")
    if not isinstance(output, Mapping):
        return None
    payload: Mapping[str, Any] = output
    nested = output.get("result")
    if isinstance(nested, Mapping):
        if output.get("success") is not True:
            return None
        payload = nested
    session_id = payload.get("session_id")
    attempt_id = payload.get("attempt_id")
    clear_session = payload.get("clear_session")
    if (
        payload.get("success") is not True
        or payload.get("handoff_staged") is not True
        or payload.get("delivery_pending") is not True
        or not isinstance(session_id, str)
        or not session_id
        or session_id != event.metadata.get("_platform_session_id")
        or not isinstance(attempt_id, str)
        or not attempt_id
        or not isinstance(clear_session, bool)
    ):
        return None
    return StagedTerminalHandoff(session_id, attempt_id, clear_session)


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
    staged = staged_handoff_from_event(event)
    if staged is None:
        return False
    db = session_manager.db
    claimed = claim_staged_handoff_delivery(db, staged.session_id, staged.attempt_id)
    if claimed is None:
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
        return
    reason = result.get("reason") or result.get("error") or "terminal delivery failed"
    _compensate_delivery_failure(db, claimed, str(reason))


def _delivery_succeeded(result: Mapping[str, Any], *, clear_session: bool) -> bool:
    if clear_session:
        return result.get("success") is True or result.get("attempt_pending") is True
    return result.get("compacted") is True


def _compensate_delivery_failure(
    db: HubDatabase,
    claimed: ClaimedHandoffDelivery,
    reason: str,
) -> None:
    failure = {
        "compacted": False,
        "delivery_failed": True,
        "delivery_pending": False,
        "attempt_id": claimed.attempt_id,
        "clear_session": claimed.clear_session,
        "reason": reason,
        "retry_guidance": _RETRY_GUIDANCE,
    }
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
