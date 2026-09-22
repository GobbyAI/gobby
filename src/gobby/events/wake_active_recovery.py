"""Restart-horizon reconciliation for stale active terminal sessions."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import ActivityProbe, TerminalActivity
from gobby.events.wake_terminal_resolution import (
    LiveTerminalResolver,
    resolve_session_terminal_route,
)
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

RunDb = Callable[..., Awaitable[Any]]
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ACTIVE_SNAPSHOT_LIMIT = 10_000


def datetime_to_unix_ms(value: object) -> int | None:
    """Return integer Unix milliseconds, rejecting missing or naive timestamps."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        delta = value.astimezone(UTC) - _EPOCH
    except (OverflowError, ValueError):
        return None
    return ((delta.days * 86_400 + delta.seconds) * 1_000) + delta.microseconds // 1_000


def restart_activity_allows_recovery(activity: TerminalActivity) -> bool:
    """Only a known idle composer with no in-flight turn may advance."""
    return activity.turn_in_flight_fingerprint is None and activity.composer.state in {
        "empty",
        "draft",
    }


def session_precedes_restart_horizon(
    session: object,
    restart_horizon_ms: int | None,
) -> bool:
    """Check both lifecycle timestamps using one integer-millisecond horizon."""
    if not isinstance(restart_horizon_ms, int) or isinstance(restart_horizon_ms, bool):
        return False
    last_activity_ms = datetime_to_unix_ms(getattr(session, "last_activity", None))
    updated_at_ms = datetime_to_unix_ms(getattr(session, "updated_at", None))
    return (
        getattr(session, "status", None) == "active"
        and getattr(session, "session_type", None) == "terminal"
        and last_activity_ms is not None
        and updated_at_ms is not None
        and last_activity_ms <= restart_horizon_ms
        and updated_at_ms <= restart_horizon_ms
    )


async def _read_activity(
    activity_probe: ActivityProbe,
    session: Session,
    terminal: Any | None,
) -> TerminalActivity:
    try:
        return await activity_probe(session, terminal)
    except Exception:
        logger.debug("Restart activity probe failed for session %s", session.id, exc_info=True)
        return TerminalActivity(ComposerRead("unknown"))


async def reconcile_restart_stale_session(
    *,
    session_manager: SessionManager,
    observed: Session,
    terminal: Any | None,
    activity_probe: ActivityProbe,
    run_db: RunDb,
    restart_horizon_ms: int | None,
    excluded_session_ids: frozenset[str],
) -> Session | None:
    """Pause one restart-stale row after two activity reads and an exact CAS."""
    if observed.id in excluded_session_ids or not session_precedes_restart_horizon(
        observed,
        restart_horizon_ms,
    ):
        return None
    first = await _read_activity(activity_probe, observed, terminal)
    if not restart_activity_allows_recovery(first):
        return None

    second = await _read_activity(activity_probe, observed, terminal)
    if not restart_activity_allows_recovery(second):
        return None
    current = await run_db(session_manager.get, observed.id)
    if (
        current is None
        or current.id in excluded_session_ids
        or current.updated_at != observed.updated_at
        or not session_precedes_restart_horizon(current, restart_horizon_ms)
    ):
        return None
    return cast(
        "Session | None",
        await run_db(
            session_manager._pause_restart_stale_active,
            observed.id,
            observed_updated_at=observed.updated_at,
            restart_horizon_ms=restart_horizon_ms,
        ),
    )


async def reconcile_restart_stale_sessions(
    *,
    session_manager: SessionManager,
    terminal_manager: LiveTerminalResolver | None,
    activity_probe: ActivityProbe | None,
    run_db: RunDb,
    restart_horizon_ms: int | None,
    excluded_session_ids: frozenset[str],
    recovery_safe: bool,
) -> tuple[str, ...]:
    """Snapshot active candidates, reconcile safe rows, and return paused IDs."""
    if not recovery_safe or activity_probe is None:
        return ()

    def snapshot_candidates() -> list[Session]:
        return session_manager.list(
            status="active",
            machine_id=require_machine_id(),
            limit=_ACTIVE_SNAPSHOT_LIMIT,
        )

    candidates = await run_db(snapshot_candidates)
    paused: list[str] = []
    for observed in candidates:
        if observed.id in excluded_session_ids or not session_precedes_restart_horizon(
            observed,
            restart_horizon_ms,
        ):
            continue

        def read_route(current: Session = observed) -> Any | None:
            return resolve_session_terminal_route(current, terminal_manager).managed_terminal

        try:
            terminal = await run_db(read_route)
        except Exception:
            logger.warning(
                "Restart terminal lookup failed for session %s",
                observed.id,
                exc_info=True,
            )
            terminal = None
        updated = await reconcile_restart_stale_session(
            session_manager=session_manager,
            observed=observed,
            terminal=terminal,
            activity_probe=activity_probe,
            run_db=run_db,
            restart_horizon_ms=restart_horizon_ms,
            excluded_session_ids=excluded_session_ids,
        )
        if updated is not None:
            paused.append(updated.id)
    return tuple(paused)
